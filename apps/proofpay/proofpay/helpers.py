"""Shared helpers: tenant-scoped lookups, money parsing, template filters."""
import re
from decimal import Decimal, InvalidOperation

from flask import abort, current_app, request, url_for
from flask_login import current_user

from .models import Customer, Job, JobPhoto, db

MAX_PRICE_CENTS = 100_000_000  # $1,000,000
MONEY_RE = re.compile(r"^\d+(\.\d{1,2})?$")


def current_business():
    business = current_user.business
    if business is None:  # every account is created with a business; defensive only
        abort(403)
    return business


def get_job_or_404(ref: str) -> Job:
    """Fetch a job owned by the logged-in business. Other tenants' jobs are a 404."""
    job = db.session.scalar(db.select(Job).filter_by(ref=ref, business_id=current_business().id))
    if job is None:
        abort(404)
    return job


def get_customer_or_404(ref: str) -> Customer:
    customer = db.session.scalar(
        db.select(Customer).filter_by(ref=ref, business_id=current_business().id))
    if customer is None:
        abort(404)
    return customer


def get_photo_or_404(ref: str) -> JobPhoto:
    photo = db.session.scalar(
        db.select(JobPhoto).filter_by(ref=ref, business_id=current_business().id))
    if photo is None:
        abort(404)
    return photo


def parse_money(raw: str) -> int | None:
    """'1,250.50' -> 125050 cents. Returns None if invalid."""
    cleaned = (raw or "").strip().replace(",", "").replace("$", "")
    if cleaned == "":
        return 0
    # Plain digits with up to 2 decimals only: no signs, exponents ("1e400"), NaN or Infinity.
    if not MONEY_RE.match(cleaned) or len(cleaned) > 15:
        return None
    try:
        cents = int(Decimal(cleaned) * 100)
    except InvalidOperation:
        return None
    return cents if cents <= MAX_PRICE_CENTS else None


def format_money(cents: int) -> str:
    """12500 -> '$125', 125050 -> '$1,250.50'."""
    cents = cents or 0
    if cents % 100 == 0:
        return f"${cents // 100:,}"
    return f"${Decimal(cents) / 100:,.2f}"


def absolute_url(endpoint: str, **values) -> str:
    """Full link (for emails / sharing). Uses APP_BASE_URL when set, else the request host."""
    base = (current_app.config.get("APP_BASE_URL") or "").rstrip("/")
    path = url_for(endpoint, **values)  # includes the mount, e.g. /proofpay
    if not base:
        return request.host_url.rstrip("/") + path
    # APP_BASE_URL may already end with the mount prefix ("https://host/proofpay"); don't repeat it.
    prefix = request.script_root
    if prefix and base.endswith(prefix):
        base = base[: -len(prefix)]
    return base + path


def public_job_url(job: Job) -> str:
    return absolute_url("public.job_page", token=job.share_token)


STATUS_CLASSES = {
    "Draft": "st-draft",
    "In Progress": "st-progress",
    "Completed": "st-done",
    "Payment Due": "st-due",
    "Paid": "st-paid",
}


def register_template_helpers(app) -> None:
    from .pricing import get_plans

    app.jinja_env.filters["money"] = format_money
    app.jinja_env.globals["status_class"] = lambda s: STATUS_CLASSES.get(s, "st-draft")
    app.jinja_env.globals["public_job_url"] = public_job_url

    from . import mailer
    # Optional email: the "Forgot password?" link only shows when email is set up.
    app.jinja_env.globals["mail_on"] = lambda: mailer.mail_enabled(current_app.config.get("MAIL"))

    @app.context_processor
    def pricing_plans():
        # Built per request from this app's PRICE_TEXT, so the price lives in one setting.
        return {"PLANS": get_plans(current_app.config["PRICE_TEXT"])}
