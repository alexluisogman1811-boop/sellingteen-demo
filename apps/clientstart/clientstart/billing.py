"""Free trial and subscription (same model as the other sellingteen apps, e.g. ReviewBoost).

Every new account gets a free trial (TRIAL_DAYS, default 30). After that, the owner pages show the
Subscribe page until the account is switched on after a payment:

    flask --app clientstart accounts                      list accounts and their status
    flask --app clientstart activate someone@shop.com     switch on for 1 more month (--months N)
    flask --app clientstart deactivate someone@shop.com   switch off (e.g. after a refund)

Client portal links (/p/<token>) are never blocked: an owner's clients can keep submitting
information even after the owner's trial ends. Their data is kept either way.

No payment details are stored here: customers pay on the payment provider's page (PAYMENT_LINK).
Billing can be turned off completely with BILLING_ENABLED=0 (for example on a private copy).
"""
from datetime import date, datetime, timedelta

import click
from flask import Blueprint, current_app, g, redirect, render_template, request, url_for
from flask.cli import with_appcontext

from .db import get_db, today
from .auth import login_required

bp = Blueprint("billing", __name__)

# Pages an account can always reach, even after the trial: billing, account, logging out, legal
# pages, the landing page, CSV / ZIP exports (so an owner can always take their data with them), and
# deleting the account.
ALWAYS_OPEN = {"billing.subscribe", "auth.logout", "auth.account", "auth.login", "auth.signup",
               "auth.change_password", "auth.delete_account", "auth.forgot_password", "auth.reset_password",
               "legal.terms", "legal.privacy", "legal.refunds", "public.index", "public.healthz", "static",
               "owner.export_clients", "owner.tasks_export", "owner.export_all"}


def _always_open(endpoint):
    # Client portal pages belong to the owner's clients and must keep working.
    return endpoint is None or endpoint in ALWAYS_OPEN or endpoint.startswith("portal.")


def _created_date(account):
    try:
        return datetime.fromisoformat(str(account["created_at"])).date()
    except (TypeError, ValueError, KeyError, IndexError):
        return today()


def access_status(account, on=None):
    """Returns a dict: state = paid | trial | ended, with the relevant dates."""
    on = on or today()
    paid_until = account["paid_until"] if "paid_until" in account.keys() else None
    until = None
    if paid_until:
        try:
            until = date.fromisoformat(paid_until)
        except ValueError:
            until = None
        if until and until >= on:
            return {"state": "paid", "until": until, "days_left": (until - on).days}
    trial_end = _created_date(account) + timedelta(days=current_app.config["TRIAL_DAYS"])
    if trial_end >= on and not paid_until:
        return {"state": "trial", "until": trial_end, "days_left": (trial_end - on).days}
    return {"state": "ended", "until": until if paid_until and until else trial_end, "was_paid": bool(paid_until)}


def check_access():
    """before_request: send accounts whose trial or subscription ended to the Subscribe page."""
    g.access = None
    if not current_app.config["BILLING_ENABLED"] or g.get("account") is None:
        return None
    row = get_db().execute("SELECT created_at, paid_until FROM accounts WHERE id = ?",
                           (g.account["id"],)).fetchone()
    if row is None:
        return None
    g.access = access_status(row)
    if g.access["state"] == "ended" and not _always_open(request.endpoint):
        return redirect(url_for("billing.subscribe"))
    return None


@bp.route("/subscribe")
@login_required
def subscribe():
    return render_template("billing/subscribe.html", access=g.get("access") or {"state": "trial"},
                           payment_link=current_app.config["PAYMENT_LINK"],
                           price=current_app.config["PRICE_TEXT"],
                           support_email=current_app.config["SUPPORT_EMAIL"])


# Email domains used only for demo / example accounts; they are left out of list_accounts().
DEMO_EMAIL_SUFFIXES = (".invalid",)  # e.g. someone@demo.invalid


def _is_demo_email(email):
    return (email or "").strip().lower().endswith(DEMO_EMAIL_SUFFIXES)


def list_accounts():
    """Billing standard: every real account (demo accounts left out) with its billing state.

    Call inside an app context. Returns a list of dicts with exactly these keys:
    email, business, state ("trial" | "paid" | "ended"), until ("YYYY-MM-DD" or None),
    created ("YYYY-MM-DD" or None). ClientStart has no free plan, so "free" is never used.
    """
    rows = get_db().execute("SELECT name, email, created_at, paid_until FROM accounts ORDER BY id").fetchall()
    result = []
    for r in rows:
        if _is_demo_email(r["email"]):
            continue
        s = access_status(r)
        try:
            created = datetime.fromisoformat(str(r["created_at"])).date().isoformat()
        except (TypeError, ValueError):
            created = None
        until = s.get("until")
        result.append({
            "email": (r["email"] or "").strip().lower(),
            "business": r["name"] or "",
            "state": s["state"],
            "until": until.isoformat() if until else None,
            "created": created,
        })
    return result


def set_paid_until(email, months):
    """Billing standard: switch an account on for `months` more months, or off when months <= 0.

    Call inside an app context. Months > 0 extend from the later of today and the current paid-until
    date by 31 days per month and return the new date. Months <= 0 set paid_until to yesterday and
    return None. Raises LookupError if there is no account with that email.
    """
    db = get_db()
    row = db.execute("SELECT id, paid_until FROM accounts WHERE lower(email) = ?",
                     ((email or "").strip().lower(),)).fetchone()
    if row is None:
        raise LookupError(f"No account with the email {email}.")
    if months <= 0:
        db.execute("UPDATE accounts SET paid_until = ? WHERE id = ?", ((today() - timedelta(days=1)).isoformat(), row["id"]))
        db.commit()
        return None
    start = today()
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
        raise click.ClickException(str(e))


def register_cli(app):
    @app.cli.command("activate")
    @click.argument("email")
    @click.option("--months", default=1, show_default=True, help="How many paid months to add.")
    @with_appcontext
    def activate(email, months):
        """Switch an account on after a payment."""
        until = _cli_set_paid_until(email, months)
        click.echo(f"{email}: switched on until {until.isoformat()}")

    @app.cli.command("deactivate")
    @click.argument("email")
    @with_appcontext
    def deactivate(email):
        """Switch an account off (for example after a refund)."""
        _cli_set_paid_until(email, 0)
        click.echo(f"{email}: switched off")

    @app.cli.command("accounts")
    @with_appcontext
    def accounts():
        """List accounts and their trial / subscription status."""
        rows = list_accounts()
        if not rows:
            click.echo("No accounts yet.")
        for r in rows:
            extra = f"until {r['until']}" if r["until"] else ""
            click.echo(f"{r['email']:40} {r['state']:7} {extra:18} {r['business']}")
