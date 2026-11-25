"""Free trial and subscription (copied from ReviewBoost, adapted to SlotSimple).

Every new account gets a free trial (TRIAL_DAYS, default 30). After that, the owner pages show
the Subscribe page until the owner switches the account on after a payment:

    flask --app slotsimple accounts                      list accounts and their status
    flask --app slotsimple activate someone@shop.com     switch on for 1 more month (--months N)
    flask --app slotsimple deactivate someone@shop.com   switch off (e.g. after a refund)

Only the owner's pages (/admin/...) are gated. Public booking pages, customer lookup and
cancellation keep working after a trial ends, so a business's customers are never stranded.

No payment details are stored here: customers pay on the payment provider's page (PAYMENT_LINK).
Billing can be turned off completely with BILLING_ENABLED=0 (for example on a private copy).
"""
from datetime import date, datetime, timedelta

import click
from flask import Blueprint, current_app, g, redirect, render_template, request, url_for

from .db import get_db, get_settings, transaction
from .security import admin_required
from .timeutil import now_utc

bp = Blueprint("billing", __name__)

# Owner pages an account can always reach, even after the trial: billing, account, logging out.
ALWAYS_OPEN = {"billing.subscribe", "admin.logout", "admin.account", "admin.login", "admin.signup",
               "admin.delete_account", "admin.export_data", "admin.forgot_password", "admin.reset_password"}


def _today():
    return now_utc().date()


def _created_date(account):
    try:
        return datetime.fromisoformat(str(account["created_at"]).replace("Z", "+00:00")).date()
    except (TypeError, ValueError, KeyError, IndexError):
        return _today()


def access_status(account, today=None):
    """Returns a dict: state = demo | paid | trial | ended, with the relevant dates."""
    today = today or _today()
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
    """before_request: send owners whose trial or subscription ended to the Subscribe page.
    Applies to owner pages only; public booking pages are never blocked."""
    g.access = None
    if not current_app.config["BILLING_ENABLED"] or g.get("account") is None:
        return None
    g.access = access_status(g.account)
    if (g.access["state"] == "ended" and request.blueprint == "admin"
            and request.endpoint not in ALWAYS_OPEN):
        return redirect(url_for("billing.subscribe"))
    return None


@bp.route("/admin/subscribe")
@admin_required
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
    created ("YYYY-MM-DD" or None). SlotSimple has no free plan, so "free" is never used.
    """
    db = get_db()
    rows = db.execute("SELECT * FROM accounts WHERE is_demo = 0 ORDER BY id").fetchall()
    result = []
    for r in rows:
        s = access_status(r)
        try:
            created = datetime.fromisoformat(str(r["created_at"]).replace("Z", "+00:00")).date().isoformat()
        except (TypeError, ValueError):
            created = None
        result.append({"email": (r["email"] or "").strip().lower(),
                       "business": get_settings(db, r["id"]).get("business_name") or "",
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
        with transaction(db):
            db.execute("UPDATE accounts SET paid_until = ? WHERE id = ?",
                       ((_today() - timedelta(days=1)).isoformat(), row["id"]))
        return None
    start = _today()
    if row["paid_until"]:
        try:
            start = max(start, date.fromisoformat(row["paid_until"]))
        except ValueError:
            pass
    until = start + timedelta(days=31 * months)
    with transaction(db):
        db.execute("UPDATE accounts SET paid_until = ? WHERE id = ?", (until.isoformat(), row["id"]))
    return until


# Older name, kept so existing scripts keep working.
_set_paid_until = set_paid_until


def _set_paid_until_cli(email, months):
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
        slugs = {row["email"]: row["slug"] for row in get_db().execute("SELECT email, slug FROM accounts")}
        for r in rows:
            extra = f"until {r['until']}" if r["until"] else ""
            click.echo(f"{r['email']:40} {r['state']:7} {extra:18} {r['business']} (/book/{slugs.get(r['email'], '')})")
