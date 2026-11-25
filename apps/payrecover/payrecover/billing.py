"""Free trial and subscription.

Every new account gets a free trial (TRIAL_DAYS, default 30). After that, the app shows the
Subscribe page until the owner switches the account on after a payment:

    flask --app payrecover accounts                      list accounts and their status
    flask --app payrecover activate someone@shop.com     switch on for 1 more month (--months N)
    flask --app payrecover deactivate someone@shop.com   switch off (e.g. after a refund)

No payment details are stored here: customers pay on the payment provider's page (PAYMENT_LINK).
Billing can be turned off completely with BILLING_ENABLED=0 (for example on a private copy).

Trial and subscription dates use the real calendar date, not PAYRECOVER_TODAY (which only
changes the date used for invoices and follow-ups in demos).
"""
from datetime import date, datetime, timedelta

import click
from flask import Blueprint, current_app, g, redirect, render_template, request, url_for

from .auth import login_required
from .db import get_db

bp = Blueprint("billing", __name__)

# Pages an account can always reach, even after the trial: billing, logging in/out, legal pages,
# the account page (so an owner can always change their password or delete their data) and /healthz.
ALWAYS_OPEN = {"billing.subscribe", "auth.logout", "auth.login", "auth.signup", "routes.index",
               "legal.terms", "legal.privacy", "legal.refunds", "static", "routes.healthz",
               "account.settings", "account.change_password", "account.delete", "account.export",
               "auth.forgot_password", "auth.reset_password"}


def _created_date(user):
    try:
        return datetime.fromisoformat(str(user["created_at"])).date()
    except (TypeError, ValueError, KeyError, IndexError):
        return date.today()


def access_status(user, today=None):
    """Returns a dict: state = paid | trial | ended, with the relevant dates."""
    today = today or date.today()
    paid_until = user["paid_until"] if "paid_until" in user.keys() else None
    until = None
    if paid_until:
        try:
            until = date.fromisoformat(paid_until)
        except ValueError:
            until = None
        if until and until >= today:
            return {"state": "paid", "until": until, "days_left": (until - today).days}
    trial_end = _created_date(user) + timedelta(days=current_app.config["TRIAL_DAYS"])
    if trial_end >= today and not paid_until:
        return {"state": "trial", "until": trial_end, "days_left": (trial_end - today).days}
    return {"state": "ended", "until": until if paid_until and until else trial_end, "was_paid": bool(paid_until)}


def check_access():
    """before_request: send accounts whose trial or subscription ended to the Subscribe page."""
    g.access = None
    if not current_app.config["BILLING_ENABLED"] or g.get("user") is None:
        return None
    row = get_db().execute("SELECT created_at, paid_until FROM users WHERE id = ?",
                           (g.user["id"],)).fetchone()
    if row is None:
        return None
    g.access = access_status(row)
    if g.access["state"] == "ended" and request.endpoint not in ALWAYS_OPEN:
        return redirect(url_for("billing.subscribe"))
    return None


@bp.route("/subscribe")
@login_required
def subscribe():
    return render_template("billing/subscribe.html", access=g.get("access") or {"state": "trial"},
                           payment_link=current_app.config["PAYMENT_LINK"],
                           price=current_app.config["PRICE_TEXT"],
                           support_email=current_app.config["SUPPORT_EMAIL"])


# Sample account made by seed_demo.py; never listed as a customer.
DEMO_EMAILS = {"demo@payrecover.test"}


def list_accounts():
    """All real (non-demo) accounts with their billing state. Call inside an app context.

    Each item: email, business, state ("trial" | "paid" | "ended"), until (YYYY-MM-DD or None),
    created (YYYY-MM-DD or None). PayRecover has no free plan, so "free" is never used."""
    rows = get_db().execute(
        "SELECT email, business_name, created_at, paid_until FROM users ORDER BY id").fetchall()
    result = []
    for r in rows:
        email = (r["email"] or "").strip().lower()
        if email in DEMO_EMAILS:
            continue
        s = access_status(r)
        try:
            created = datetime.fromisoformat(str(r["created_at"])).date().isoformat()
        except (TypeError, ValueError):
            created = None
        until = s.get("until")
        result.append({"email": email, "business": r["business_name"] or "", "state": s["state"],
                       "until": until.isoformat() if until else None, "created": created})
    return result


def set_paid_until(email, months):
    """Switch an account on for `months` more months (from today or its current end date, whichever is
    later; 31 days a month) and return the new end date. months <= 0 switches it off and returns None.
    Raises LookupError if there is no such account. Call inside an app context."""
    db = get_db()
    row = db.execute("SELECT id, paid_until FROM users WHERE email = ?",
                     ((email or "").strip().lower(),)).fetchone()
    if row is None:
        raise LookupError(f"No account with the email {email}.")
    if months <= 0:
        db.execute("UPDATE users SET paid_until = ? WHERE id = ?", ((date.today() - timedelta(days=1)).isoformat(), row["id"]))
        db.commit()
        return None
    start = date.today()
    if row["paid_until"]:
        try:
            start = max(start, date.fromisoformat(row["paid_until"]))
        except ValueError:
            pass
    until = start + timedelta(days=31 * months)
    db.execute("UPDATE users SET paid_until = ? WHERE id = ?", (until.isoformat(), row["id"]))
    db.commit()
    return until


_set_paid_until = set_paid_until  # older name, kept for compatibility


def _cli_set(email, months):
    try:
        return set_paid_until(email, months)
    except LookupError as exc:
        raise click.ClickException(str(exc)) from None


def register_cli(app):
    @app.cli.command("activate")
    @click.argument("email")
    @click.option("--months", default=1, show_default=True, help="How many paid months to add.")
    def activate(email, months):
        """Switch an account on after a payment."""
        until = _cli_set(email, months)
        if until is None:
            click.echo(f"{email}: switched off")
            return
        click.echo(f"{email}: switched on until {until.isoformat()}")

    @app.cli.command("deactivate")
    @click.argument("email")
    def deactivate(email):
        """Switch an account off (for example after a refund)."""
        _cli_set(email, 0)
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
