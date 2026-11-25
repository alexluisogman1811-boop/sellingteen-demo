"""Free trial and subscription, per COMPANY.

Every new company gets a free trial (TRIAL_DAYS, default 30). After that, the Owner sees the
Subscribe page and Office/Foreman users see "ask your owner" until the company is switched on
after a payment. Data is always kept. Public GC signing links keep working regardless.

    flask --app signedextra accounts                       list companies and their status
    flask --app signedextra activate owner@company.com     switch on for 1 more month (--months N)
    flask --app signedextra deactivate owner@company.com   switch off (e.g. after a refund)

Companies are looked up by their Owner's email. No payment details are stored here: customers pay
on the payment provider's page (PAYMENT_LINK). BILLING_ENABLED=0 turns billing off completely.
"""
from datetime import date, datetime, timedelta

import click
from flask import Blueprint, current_app, g, redirect, render_template, request, url_for

from .db import get_db
from .security import audit, login_required

bp = Blueprint("billing", __name__)

# Endpoints a logged-in user can always reach, even after the trial ends.
ALWAYS_OPEN = {
    "billing.subscribe", "billing.ask_owner", "auth.logout", "auth.login", "auth.signup",
    "auth.accept_invite", "auth.reset_password", "auth.forgot", "auth.forgot_email", "auth.email_reset",
    "account.overview", "account.change_password", "account.export_company", "account.delete_company",
    "legal.terms", "legal.privacy", "legal.refunds", "static", "healthz",
}
# Anything a GC needs to sign or decline: never blocked, whoever is logged in on the device.
ALWAYS_OPEN_PREFIXES = ("public.",)


def _created_date(company):
    try:
        return datetime.fromisoformat(str(company["created_at"])).date()
    except (TypeError, ValueError, KeyError, IndexError):
        return date.today()


def access_status(company, today=None):
    """Returns a dict: state = demo | paid | trial | ended, with the relevant dates."""
    today = today or date.today()
    if company["is_demo"]:
        return {"state": "demo"}
    paid_until = company["paid_until"] if "paid_until" in company.keys() else None
    until = None
    if paid_until:
        try:
            until = date.fromisoformat(paid_until)
        except ValueError:
            until = None
        if until and until >= today:
            return {"state": "paid", "until": until, "days_left": (until - today).days}
    trial_end = _created_date(company) + timedelta(days=current_app.config["TRIAL_DAYS"])
    if not paid_until and trial_end >= today:
        return {"state": "trial", "until": trial_end, "days_left": (trial_end - today).days}
    return {"state": "ended", "until": until or trial_end, "was_paid": bool(paid_until)}


def _is_open(endpoint):
    return endpoint is None or endpoint in ALWAYS_OPEN or endpoint.startswith(ALWAYS_OPEN_PREFIXES)


def check_access():
    """before_request: companies whose trial or subscription ended can't use the app (data is kept)."""
    g.access = None
    if not current_app.config["BILLING_ENABLED"] or g.get("company") is None:
        return None
    g.access = access_status(g.company)
    if g.access["state"] == "ended" and not _is_open(request.endpoint):
        if g.user["role"] == "owner":
            return redirect(url_for("billing.subscribe"))
        return redirect(url_for("billing.ask_owner"))
    return None


def _context():
    return {"access": g.get("access") or {"state": "trial"},
            "payment_link": current_app.config["PAYMENT_LINK"],
            "price": current_app.config["PRICE_TEXT"],
            "support_email": current_app.config["SUPPORT_EMAIL"]}


@bp.route("/subscribe")
@login_required
def subscribe():
    if g.user["role"] != "owner":
        return redirect(url_for("billing.ask_owner"))
    return render_template("billing/subscribe.html", **_context())


@bp.route("/subscription-needed")
@login_required
def ask_owner():
    if g.user["role"] == "owner":
        return redirect(url_for("billing.subscribe"))
    owner = get_db().execute("SELECT name FROM users WHERE company_id = ? AND role = 'owner' ORDER BY id LIMIT 1",
                             (g.company["id"],)).fetchone()
    return render_template("billing/ask_owner.html", owner_name=owner["name"] if owner else "your owner",
                           **_context())


def _owner_company(email):
    db = get_db()
    row = db.execute(
        "SELECT c.* FROM companies c JOIN users u ON u.company_id = c.id"
        " WHERE u.email = ? AND u.role = 'owner'", ((email or "").strip().lower(),)).fetchone()
    if row is None:
        raise LookupError(f"No company has an Owner with the email {email}.")
    return row


def _iso_day(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return None


def list_accounts():
    """Billing standard API: one dict per (non-demo) company, looked up by its Owner's email.
    Keys: email, business, state ("trial" | "paid" | "ended"), until ("YYYY-MM-DD" or None),
    created ("YYYY-MM-DD" or None). Call inside an app context."""
    rows = get_db().execute(
        "SELECT c.*, (SELECT email FROM users u WHERE u.company_id = c.id AND u.role = 'owner'"
        " ORDER BY u.id LIMIT 1) AS owner_email FROM companies c WHERE c.is_demo = 0 ORDER BY c.id").fetchall()
    result = []
    for r in rows:
        s = access_status(r)
        until = s.get("until")
        result.append({
            "email": (r["owner_email"] or "").strip().lower(),
            "business": r["name"] or "",
            "state": s["state"],
            "until": until.isoformat() if until else None,
            "created": _iso_day(r["created_at"]),
        })
    return result


def set_paid_until(email, months):
    """Billing standard API: months > 0 extends from max(today, current paid_until) by 31*months days and
    returns the new end date; months <= 0 switches off (paid_until = yesterday) and returns None.
    Raises LookupError when no company has an Owner with that email. Call inside an app context."""
    db = get_db()
    company = _owner_company(email)
    if months <= 0:
        until = date.today() - timedelta(days=1)
    else:
        start = date.today()
        if company["paid_until"]:
            try:
                start = max(start, date.fromisoformat(company["paid_until"]))
            except ValueError:
                pass
        until = start + timedelta(days=31 * months)
    db.execute("UPDATE companies SET paid_until = ? WHERE id = ?", (until.isoformat(), company["id"]))
    audit(company["id"], "billing.activated" if months > 0 else "billing.deactivated",
          details={"paid_until": until.isoformat()}, actor="operator (command line)", user_id=None)
    db.commit()
    return until if months > 0 else None


_set_paid_until = set_paid_until  # older name, kept as an alias


def register_cli(app):
    @app.cli.command("activate")
    @click.argument("email")
    @click.option("--months", default=1, show_default=True, help="How many paid months to add.")
    def activate(email, months):
        """Switch a company on after a payment (looked up by its Owner's email)."""
        try:
            until = set_paid_until(email, months)
        except LookupError as exc:
            raise click.ClickException(str(exc))
        if until is None:
            click.echo(f"{email}: switched off")
        else:
            click.echo(f"{email}: switched on until {until.isoformat()}")

    @app.cli.command("deactivate")
    @click.argument("email")
    def deactivate(email):
        """Switch a company off, for example after a refund (looked up by its Owner's email)."""
        try:
            set_paid_until(email, 0)
        except LookupError as exc:
            raise click.ClickException(str(exc))
        click.echo(f"{email}: switched off")

    @app.cli.command("accounts")
    def accounts():
        """List companies with their Owner email and trial / subscription status."""
        rows = list_accounts()
        if not rows:
            click.echo("No companies yet.")
        for r in rows:
            extra = f"until {r['until']}" if r["until"] else ""
            click.echo(f"{(r['email'] or '-'):40} {r['state']:7} {extra:18} {r['business']}")
