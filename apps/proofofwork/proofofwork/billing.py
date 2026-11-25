"""Free trial and subscription (copied from the ReviewBoost reference and adapted to business accounts).

Every new business account gets a free trial (TRIAL_DAYS, default 30). After that, managers see the
Subscribe page and workers see a polite "ask your manager" message, until the owner switches the account
on after a payment:

    flask --app proofofwork accounts                      list accounts and their status
    flask --app proofofwork activate someone@shop.com     switch on for 1 more month (--months N)
    flask --app proofofwork deactivate someone@shop.com   switch off (e.g. after a refund)

Workers belong to their manager's business account. They are not separate paying accounts, so they never
appear in list_accounts() and their email can't be used to activate an account.

Customer report links (/r/<token>) are public pages and keep working whatever the account's status.
No payment details are stored here: customers pay on the payment provider's page (PAYMENT_LINK).
Billing can be turned off completely with BILLING_ENABLED=0 (for example on a private copy).
"""
from datetime import date, datetime, timedelta

import click
from flask import Blueprint, current_app, g, redirect, render_template, request, url_for

from .auth import login_required
from .db import get_db

bp = Blueprint("billing", __name__)

# Pages always reachable, even after the trial: subscription, account settings, export, delete, logging in/out,
# password reset, legal pages, health check, and the public customer report links.
ALWAYS_OPEN = {"billing.subscribe", "auth.logout", "auth.login", "auth.signup", "auth.setup", "auth.change_password",
               "auth.business_name", "auth.delete_account", "auth.export_data", "auth.forgot_password",
               "auth.reset_password", "legal.terms", "legal.privacy", "legal.refunds", "static", "healthz",
               "main.shared_report", "main.shared_photo"}


def _created_date(account):
    try:
        return datetime.fromisoformat(str(account["created_at"])).date()
    except (TypeError, ValueError, KeyError, IndexError):
        return date.today()


def access_status(account, today=None):
    """Returns a dict: state = demo | paid | trial | ended, with the relevant dates."""
    today = today or date.today()
    if account["is_demo"]:
        return {"state": "demo"}
    paid_until = account["paid_until"]
    until = None
    if paid_until:
        try:
            until = date.fromisoformat(paid_until)
        except ValueError:
            until = None
        if until and until >= today:
            return {"state": "paid", "until": until, "days_left": (until - today).days}
    trial_end = _created_date(account) + timedelta(days=current_app.config["TRIAL_DAYS"])
    if trial_end >= today and not paid_until:
        return {"state": "trial", "until": trial_end, "days_left": (trial_end - today).days}
    return {"state": "ended", "until": until if paid_until and until else trial_end, "was_paid": bool(paid_until)}


def check_access():
    """before_request: accounts whose trial or subscription ended can only reach the pages in ALWAYS_OPEN."""
    g.access = None
    if not current_app.config["BILLING_ENABLED"] or g.get("account") is None:
        return None
    g.access = access_status(g.account)
    if g.access["state"] == "ended" and request.endpoint not in ALWAYS_OPEN:
        return redirect(url_for("billing.subscribe"))
    return None


@bp.route("/subscribe")
@login_required
def subscribe():
    access = g.get("access") or {"state": "trial"}
    if g.user["role"] != "manager":
        # Workers can't pay for the business: tell them who can, instead of showing a payment page.
        return render_template("billing/ask_manager.html", access=access)
    return render_template("billing/subscribe.html", access=access,
                           payment_link=current_app.config["PAYMENT_LINK"],
                           price=current_app.config["PRICE_TEXT"],
                           support_email=current_app.config["SUPPORT_EMAIL"])


def _find_account(db, email):
    """An account by its sign-up email, or by the email of one of its managers (never by a worker's email)."""
    email = (email or "").strip()
    return (db.execute("SELECT id, paid_until FROM accounts WHERE email = ? ORDER BY id LIMIT 1", (email,)).fetchone()
            or db.execute("SELECT a.id, a.paid_until FROM accounts a JOIN users u ON u.account_id = a.id"
                          " WHERE u.email = ? AND u.role = 'manager' LIMIT 1", (email,)).fetchone())


def _iso_day(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).date().isoformat()
    except ValueError:
        return None


def list_accounts():
    """Every business account except demo accounts. Call inside an app context.

    Returns a list of dicts with exactly these keys: email (lowercase), business, state ("trial" | "paid" |
    "ended"), until ("YYYY-MM-DD" or None) and created ("YYYY-MM-DD" or None)."""
    rows = get_db().execute(
        "SELECT name, email, is_demo, created_at, paid_until FROM accounts WHERE is_demo = 0 ORDER BY id").fetchall()
    result = []
    for r in rows:
        status = access_status(r)
        until = status.get("until")
        result.append({
            "email": (r["email"] or "").strip().lower(),
            "business": r["name"] or "",
            "state": status["state"],
            "until": until.isoformat() if until else None,
            "created": _iso_day(r["created_at"]),
        })
    return result


def set_paid_until(email, months):
    """Switch an account on or off. Call inside an app context.

    months > 0: adds 31 days per month from today or the current paid-until date (whichever is later) and returns
    the new date. months <= 0: switches the account off (paid until yesterday) and returns None.
    Raises LookupError when no account has that email."""
    db = get_db()
    row = _find_account(db, email)
    if row is None:
        raise LookupError(f"No account with the email {(email or '').strip()}.")
    if months <= 0:
        db.execute("UPDATE accounts SET paid_until = ? WHERE id = ?",
                   ((date.today() - timedelta(days=1)).isoformat(), row["id"]))
        db.commit()
        return None
    start = date.today()
    if row["paid_until"]:
        try:
            start = max(start, date.fromisoformat(row["paid_until"]))
        except ValueError:
            pass
    until = start + timedelta(days=31 * months)
    db.execute("UPDATE accounts SET paid_until = ? WHERE id = ?", (until.isoformat(), row["id"]))
    db.commit()
    return until


_set_paid_until = set_paid_until  # older name, kept as an alias


def register_cli(app):
    @app.cli.command("activate")
    @click.argument("email")
    @click.option("--months", default=1, show_default=True, help="How many paid months to add.")
    def activate(email, months):
        """Switch an account on after a payment."""
        try:
            until = set_paid_until(email, months)
        except LookupError as exc:
            raise click.ClickException(str(exc))
        click.echo(f"{email}: switched on until {until.isoformat()}" if until else f"{email}: switched off")

    @app.cli.command("deactivate")
    @click.argument("email")
    def deactivate(email):
        """Switch an account off (for example after a refund)."""
        try:
            set_paid_until(email, 0)
        except LookupError as exc:
            raise click.ClickException(str(exc))
        click.echo(f"{email}: switched off")

    @app.cli.command("accounts")
    def accounts():
        """List accounts and their trial / subscription status."""
        rows = list_accounts()
        if not rows:
            click.echo("No accounts yet.")
        for r in rows:
            extra = f"until {r['until']}" if r["until"] else ""
            click.echo(f"{r['email']:40} {r['state']:7} {extra:18} {r['business']}")
