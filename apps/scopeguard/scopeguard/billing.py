"""Free trial and subscription.

Every new account gets a free trial (TRIAL_DAYS, default 30). After that, the app shows the
Subscribe page until the owner switches the account on after a payment:

    flask --app run accounts                      list accounts and their status
    flask --app run activate someone@studio.com   switch on for 1 more month (--months N)
    flask --app run deactivate someone@studio.com switch off (e.g. after a refund)

No payment details are stored here: customers pay on the payment provider's page (PAYMENT_LINK).
Billing can be turned off completely with BILLING_ENABLED=0 (for example on a private copy).

Client review links (/review/...) are public pages for the owner's clients. They are never
blocked by an ended trial: a client must always be able to see and decide on what they were sent.
"""
from datetime import date, datetime, timedelta

import click
from flask import Blueprint, current_app, g, redirect, render_template, request, url_for

from .auth import login_required
from .db import get_db

bp = Blueprint("billing", __name__)

# Pages an account can always reach, even after the trial: billing, account, logging out, legal pages,
# taking its data out and deleting the account. Client review pages (portal.*) are always open too.
# The landing page ("/") is not here: for a signed-in account it is the dashboard.
ALWAYS_OPEN = {"billing.subscribe", "auth.logout", "auth.login", "auth.signup", "auth.setup",
               "auth.forgot_password", "auth.reset_password",
               "main.settings", "main.account_update", "main.password_change", "main.export_all",
               "main.export_zip", "main.account_delete",
               "legal.terms", "legal.privacy", "legal.refunds", "static", "healthz"}


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
    endpoint = request.endpoint or ""
    if g.access["state"] == "ended" and endpoint not in ALWAYS_OPEN and not endpoint.startswith("portal."):
        return redirect(url_for("billing.subscribe"))
    return None


@bp.route("/subscribe")
@login_required
def subscribe():
    return render_template("billing/subscribe.html", access=g.get("access") or {"state": "trial"},
                           payment_link=current_app.config["PAYMENT_LINK"],
                           price=current_app.config["PRICE_TEXT"],
                           support_email=current_app.config["SUPPORT_EMAIL"])


def _iso(value):
    return value.isoformat() if value else None


def list_accounts():
    """All real (non-demo) accounts with their billing state. Call inside an app context.

    Each item: email, business, state ("trial" | "paid" | "ended"), until ("YYYY-MM-DD" or None),
    created ("YYYY-MM-DD" or None). ScopeGuard has no free plan, so "free" is never used.
    """
    rows = get_db().execute("SELECT name, email, is_demo, created_at, paid_until FROM accounts "
                            "WHERE is_demo = 0 ORDER BY id").fetchall()
    result = []
    for r in rows:
        s = access_status(r)
        try:
            created = datetime.fromisoformat(str(r["created_at"])).date().isoformat()
        except (TypeError, ValueError):
            created = None
        result.append({"email": (r["email"] or "").strip().lower(), "business": r["name"] or "",
                       "state": s["state"], "until": _iso(s.get("until")), "created": created})
    return result


def set_paid_until(email, months):
    """Switch an account on for `months` more months (31 days each), or off when months <= 0.

    Extends from the later of today and the current paid-until date. Returns the new date,
    or None when switched off. Raises LookupError if no account has this email.
    """
    db = get_db()
    row = db.execute("SELECT id, paid_until FROM accounts WHERE lower(email) = ?",
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


# Older name, kept so existing scripts keep working.
_set_paid_until = set_paid_until


def _set_paid_until_cli(email, months):
    try:
        return set_paid_until(email, months)
    except LookupError as exc:
        raise click.ClickException(str(exc)) from exc


def register_cli(app):
    @app.cli.command("activate")
    @click.argument("email")
    @click.option("--months", default=1, show_default=True, help="How many paid months to add.")
    def activate(email, months):
        """Switch an account on after a payment."""
        until = _set_paid_until_cli(email, months)
        if until is None:
            click.echo(f"{email}: switched off")
        else:
            click.echo(f"{email}: switched on until {until.isoformat()}")

    @app.cli.command("deactivate")
    @click.argument("email")
    def deactivate(email):
        """Switch an account off (for example after a refund)."""
        _set_paid_until_cli(email, 0)
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
