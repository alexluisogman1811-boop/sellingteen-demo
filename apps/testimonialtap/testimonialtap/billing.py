"""Free trial and subscription (adapted from ReviewBoost's billing.py).

Every new account gets a free trial (TRIAL_DAYS, default 30). After that, the owner pages show
the Subscribe page until the account is switched on after a payment:

    flask --app run accounts                      list accounts and their status
    flask --app run activate someone@agency.com   switch on for 1 more month (--months N)
    flask --app run deactivate someone@agency.com switch off (e.g. after a refund)

The owner's public pages (submission links /s/..., testimonial pages /w/... and embeds) are
for their clients and website visitors, so they keep working whatever the subscription state.

No payment details are stored here: customers pay on the payment provider's page (PAYMENT_LINK).
Billing can be turned off completely with BILLING_ENABLED=0 (for example on a private copy).
"""
from datetime import date, datetime, timedelta

import click
from flask import Blueprint, current_app, g, redirect, render_template, request, url_for

from .db import get_db
from .security import login_required

bp = Blueprint("billing", __name__)

# Owner pages an account can always reach, even after the trial: subscribing, the profile and
# password, exporting their data, logging in/out and the legal pages.
ALWAYS_OPEN = {"billing.subscribe", "admin.logout", "admin.login", "admin.signup", "admin.profile",
               "admin.change_password", "admin.export_filtered", "admin.export_all", "admin.delete_account",
               "admin.forgot_password", "admin.reset_password", "legal.terms", "legal.privacy",
               "legal.refunds", "static", "healthz"}
# Blueprints that are never gated: the owner's clients and visitors use these.
NEVER_GATED_BLUEPRINTS = {"public", "legal"}


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
    if request.blueprint in NEVER_GATED_BLUEPRINTS:
        return None
    g.access = access_status(g.account)
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


def _iso_day(value):
    """'YYYY-MM-DD' from a stored date/datetime string, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).date().isoformat()
    except ValueError:
        try:
            return date.fromisoformat(str(value)[:10]).isoformat()
        except ValueError:
            return None


def list_accounts():
    """Standard billing API: every real (non-demo) account with its status. Needs an app context."""
    rows = get_db().execute("SELECT business_name, email, is_demo, created_at, paid_until FROM accounts "
                            "WHERE is_demo = 0 ORDER BY id").fetchall()
    result = []
    for r in rows:
        s = access_status(r)
        until = s.get("until")
        result.append({
            "email": (r["email"] or "").strip().lower(),
            "business": r["business_name"] or "",
            "state": s["state"],
            "until": until.isoformat() if until else None,
            "created": _iso_day(r["created_at"]),
        })
    return result


def set_paid_until(email, months):
    """Standard billing API: months > 0 extends the subscription by 31 days per month (from today or
    the current end date, whichever is later) and returns the new end date; months <= 0 switches the
    account off and returns None. Raises LookupError if there is no such account."""
    db = get_db()
    row = db.execute("SELECT id, paid_until FROM accounts WHERE email = ? COLLATE NOCASE",
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


def _cli_set_paid_until(email, months):
    try:
        return set_paid_until(email, months)
    except LookupError as e:
        raise click.ClickException(str(e)) from None


def register_cli(app):
    @app.cli.command("activate")
    @click.argument("email")
    @click.option("--months", default=1, show_default=True, help="How many paid months to add.")
    def activate(email, months):
        """Switch an account on after a payment."""
        until = _cli_set_paid_until(email, months)
        if until is None:
            click.echo(f"{email}: switched off")
        else:
            click.echo(f"{email}: switched on until {until.isoformat()}")

    @app.cli.command("deactivate")
    @click.argument("email")
    def deactivate(email):
        """Switch an account off (for example after a refund)."""
        _cli_set_paid_until(email, 0)
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
