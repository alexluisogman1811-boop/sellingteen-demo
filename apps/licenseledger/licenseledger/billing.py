"""Free trial and subscription (same scheme as the other sellingteen apps).

Every new account gets a free trial (TRIAL_DAYS, default 30). After that, the app shows the
Subscribe page until the owner switches the account on after a payment:

    flask --app licenseledger accounts                      list accounts and their status
    flask --app licenseledger activate someone@shop.com     switch on for 1 more month (--months N)
    flask --app licenseledger deactivate someone@shop.com   switch off (e.g. after a refund)

No payment details are stored here: sellers pay on the payment provider's page (PAYMENT_LINK).
This is LicenseLedger's own subscription. It is unrelated to the order "payment status" sellers
record for their customers, which LicenseLedger never verifies.
Billing can be turned off completely with BILLING_ENABLED=0 (for example on a private copy).
"""
from datetime import date, datetime, timedelta

import click
from flask import Blueprint, current_app, g, redirect, render_template, request, url_for

from .db import get_db

bp = Blueprint("billing", __name__)

# Pages an account can always reach, even after the trial: subscription, account settings,
# logging out, deleting the account, legal pages, and downloading its own data as CSV/ZIP.
# (LicenseLedger has no public customer pages.)
ALWAYS_OPEN = {"billing.subscribe", "auth.logout", "auth.delete_account", "auth.login", "auth.signup",
               "auth.forgot_password", "auth.reset_password", "main.settings", "main.data", "main.data_export",
               "main.data_export_all", "legal.terms", "legal.privacy", "legal.refunds", "static", "healthz"}


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
    paid_until = account["paid_until"] if "paid_until" in account.keys() else None
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
    """before_request: send accounts whose trial or subscription ended to the Subscribe page."""
    g.access = None
    if not current_app.config["BILLING_ENABLED"] or g.get("account") is None:
        return None
    g.access = access_status(g.account)
    if g.access["state"] == "ended" and request.endpoint not in ALWAYS_OPEN:
        return redirect(url_for("billing.subscribe"))
    return None


@bp.route("/subscribe")
def subscribe():
    return render_template("billing/subscribe.html", access=g.get("access") or {"state": "trial"},
                           payment_link=current_app.config["PAYMENT_LINK"],
                           price=current_app.config["PRICE_TEXT"],
                           support_email=current_app.config["SUPPORT_EMAIL"])


def _date_only(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).date().isoformat()
    except ValueError:
        return None


def list_accounts():
    """All real (non-demo) accounts as dicts: email, business, state, until, created.

    state is "trial", "paid" or "ended" (LicenseLedger has no free plan).
    Call inside an app context."""
    rows = get_db().execute(
        "SELECT business_name, email, is_demo, created_at, paid_until FROM accounts WHERE is_demo = 0 ORDER BY id"
    ).fetchall()
    result = []
    for r in rows:
        s = access_status(r)
        until = s.get("until")
        result.append({
            "email": (r["email"] or "").lower(),
            "business": r["business_name"] or "",
            "state": s["state"],
            "until": until.isoformat() if until else None,
            "created": _date_only(r["created_at"]),
        })
    return result


def set_paid_until(email, months):
    """Switch an account on for `months` more paid months (31 days each), counted from
    today or from the current paid-until date if that is later. months <= 0 switches it
    off (paid until yesterday) and returns None. Raises LookupError for an unknown email.
    Call inside an app context."""
    db = get_db()
    row = db.execute("SELECT id, paid_until FROM accounts WHERE email = ?",
                     ((email or "").strip().lower(),)).fetchone()
    if row is None:
        raise LookupError(f"No account with the email {email}.")
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
        if until is None:
            click.echo(f"{email}: switched off")
        else:
            click.echo(f"{email}: switched on until {until.isoformat()}")

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
