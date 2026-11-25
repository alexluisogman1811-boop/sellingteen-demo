"""Free trial, Pro subscription and Free-plan fallback.

Every new account gets a free trial of Pro (TRIAL_DAYS, default 30). After that, accounts that
haven't paid are NOT locked out: they fall back to the Free plan (FREE_ACTIVE_JOBS active jobs,
basic job reports, no AI summaries). Customer job-report links keep working on every plan.
The owner switches Pro on after a payment:

    flask --app wsgi accounts                      list accounts and their plan
    flask --app wsgi activate someone@shop.com     Pro for 1 more month (--months N)
    flask --app wsgi deactivate someone@shop.com   back to Free (e.g. after a refund)

No payment details are stored here: owners pay on the payment provider's page (PAYMENT_LINK).
Billing can be turned off completely with BILLING_ENABLED=0 (everyone gets Pro).
"""
from datetime import date, timedelta

import click
from flask import Blueprint, current_app, g, render_template
from flask_login import current_user, login_required
from sqlalchemy import func

from .models import Job, JobStatus, User, db, utcnow

bp = Blueprint("billing", __name__)

PRO_STATES = {"demo", "disabled", "paid", "trial"}


def _today() -> date:
    return utcnow().date()  # same clock as created_at


def _created_date(business) -> date:
    created = getattr(business, "created_at", None)
    try:
        return created.date() if hasattr(created, "date") else date.fromisoformat(str(created)[:10])
    except (TypeError, ValueError):
        return _today()


def access_status(business, today: date | None = None) -> dict:
    """state = demo | disabled | paid | trial | free, plus `pro` and the relevant dates."""
    today = today or _today()
    if business.is_demo:
        return {"state": "demo", "pro": True}
    if not current_app.config.get("BILLING_ENABLED", True):
        return {"state": "disabled", "pro": True}
    paid_until = business.paid_until
    if paid_until and paid_until >= today:
        return {"state": "paid", "pro": True, "until": paid_until,
                "days_left": (paid_until - today).days}
    trial_end = _created_date(business) + timedelta(days=current_app.config["TRIAL_DAYS"])
    if trial_end >= today and not paid_until:
        return {"state": "trial", "pro": True, "until": trial_end,
                "days_left": (trial_end - today).days}
    return {"state": "free", "pro": False, "until": paid_until or trial_end,
            "was_paid": bool(paid_until)}


def load_access():
    """before_request: work out the logged-in account's plan. Never redirects or blocks a page."""
    g.access = None
    if current_user.is_authenticated and current_user.business is not None:
        g.access = access_status(current_user.business)
    return None


def has_pro(business) -> bool:
    access = g.get("access")
    if access is None or current_user.business is not business:
        access = access_status(business)
    return access["pro"]


def active_job_count(business, exclude_job=None) -> int:
    query = db.select(func.count(Job.id)).where(Job.business_id == business.id,
                                               Job.status != JobStatus.COMPLETED)
    if exclude_job is not None:
        query = query.where(Job.id != exclude_job.id)
    return db.session.scalar(query) or 0


def can_add_active_job(business, exclude_job=None) -> bool:
    """Free plan: at most FREE_ACTIVE_JOBS jobs that aren't completed. Pro: unlimited."""
    if has_pro(business):
        return True
    return active_job_count(business, exclude_job) < current_app.config["FREE_ACTIVE_JOBS"]


def free_limit_message() -> str:
    n = current_app.config["FREE_ACTIVE_JOBS"]
    return (f"The Free plan includes {n} active jobs. Mark a job completed, "
            f"or upgrade to Pro for unlimited jobs.")


@bp.route("/subscribe")
@login_required
def subscribe():
    business = current_user.business
    return render_template("billing/subscribe.html",
                           access=g.get("access") or access_status(business),
                           account_email=current_user.email,
                           payment_link=current_app.config["PAYMENT_LINK"],
                           price=current_app.config["PRICE_TEXT"],
                           support_email=current_app.config["SUPPORT_EMAIL"],
                           free_jobs=current_app.config["FREE_ACTIVE_JOBS"],
                           trial_days=current_app.config["TRIAL_DAYS"])


def _account_state(business, today: date) -> tuple[str, date | None]:
    """Plan state for the owner's account list, ignoring BILLING_ENABLED: trial | paid | free."""
    paid_until = business.paid_until
    if paid_until and paid_until >= today:
        return "paid", paid_until
    trial_end = _created_date(business) + timedelta(days=current_app.config["TRIAL_DAYS"])
    if trial_end >= today and not paid_until:
        return "trial", trial_end
    # ProofPay has a Free plan: after the trial (or a paid period) the account is on Free, not locked.
    return "free", paid_until or trial_end


def list_accounts() -> list[dict]:
    """Every real (non-demo) account, for the owner's CLI and the hosting dashboard.

    Call inside an app context. Keys: email, business, state ("trial" | "paid" | "free"),
    until ("YYYY-MM-DD" or None), created ("YYYY-MM-DD" or None).
    """
    today = _today()
    users = db.session.scalars(db.select(User).order_by(User.id)).all()
    rows = []
    for user in users:
        business = user.business
        if business is None or business.is_demo:
            continue
        state, until = _account_state(business, today)
        created = getattr(business, "created_at", None)
        rows.append({
            "email": (user.email or "").strip().lower(),
            "business": business.name or "",
            "state": state,
            "until": until.isoformat() if until else None,
            "created": _created_date(business).isoformat() if created else None,
        })
    return rows


def set_paid_until(email: str, months: int):
    """Extend Pro by 31 days per month (from today or the current end date, whichever is later).

    months <= 0 switches Pro off (paid_until = yesterday) and returns None.
    Raises LookupError if there is no such (non-demo) account. Call inside an app context.
    """
    user = db.session.scalar(db.select(User).filter_by(email=(email or "").strip().lower()))
    if user is None or user.business is None or user.business.is_demo:
        raise LookupError(f"No account with the email {email}.")
    business = user.business
    if months <= 0:
        business.paid_until = _today() - timedelta(days=1)
        db.session.commit()
        return None
    start = max(_today(), business.paid_until) if business.paid_until else _today()
    business.paid_until = start + timedelta(days=31 * months)
    db.session.commit()
    return business.paid_until


_set_paid_until = set_paid_until  # old name, kept for existing callers


def _cli_set_paid_until(email: str, months: int):
    try:
        return set_paid_until(email, months)
    except LookupError as e:
        raise click.ClickException(str(e))


def register_cli(app) -> None:
    @app.cli.command("activate")
    @click.argument("email")
    @click.option("--months", default=1, show_default=True, help="How many paid months to add.")
    def activate(email, months):
        """Switch an account to Pro after a payment."""
        if months < 1:
            raise click.ClickException("--months must be 1 or more.")
        until = _cli_set_paid_until(email, months)
        click.echo(f"{email}: Pro until {until.isoformat()}")

    @app.cli.command("deactivate")
    @click.argument("email")
    def deactivate(email):
        """Move an account back to the Free plan (for example after a refund)."""
        _cli_set_paid_until(email, 0)
        click.echo(f"{email}: switched off (Free plan)")

    @app.cli.command("accounts")
    def accounts():
        """List accounts and their trial / subscription status."""
        rows = list_accounts()
        if not rows:
            click.echo("No accounts yet.")
        for r in rows:
            extra = f"until {r['until']}" if r["until"] else ""
            click.echo(f"{r['email']:40} {r['state']:8} {extra:18} {r['business']}")
