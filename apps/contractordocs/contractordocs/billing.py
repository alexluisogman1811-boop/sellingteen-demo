"""Free trial and subscription.

Every new account gets a free trial (TRIAL_DAYS, default 30, counted from the account's created_at).
After that, the app shows the Subscribe page until the owner switches the account on after a payment:

    flask --app run accounts                         list accounts and their status
    flask --app run activate someone@company.com     switch on for 1 more month (--months N)
    flask --app run deactivate someone@company.com   switch off (e.g. after a refund)

No payment details are stored here: customers pay on the payment provider's page (PAYMENT_LINK).
Billing can be turned off completely with BILLING_ENABLED=0 (for example on a private copy).
"""
from datetime import date, datetime, timedelta

import click
from flask import Blueprint, current_app, g, redirect, render_template, request, url_for

from .db import get_db
from .routes import login_required

bp = Blueprint("billing", __name__)

# Pages an account can always reach, even after the trial: billing, account settings, logging in/out,
# legal pages and static files. (ContractorDocs has no public pages for subcontractors.)
ALWAYS_OPEN = {"billing.subscribe", "main.logout", "main.settings", "main.login", "main.signup",
               "main.account", "main.change_password", "main.delete_account", "main.export_data",
               "main.forgot_password", "main.reset_password", "main.healthz",
               "legal.terms", "legal.privacy", "legal.refunds", "static"}


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


def _date_str(value):
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
    """All accounts (call inside an app context). ContractorDocs has no demo accounts and no free plan.

    Each item: {"email", "business", "state" ("trial" | "paid" | "ended"), "until" ("YYYY-MM-DD" or None),
    "created" ("YYYY-MM-DD" or None)}.
    """
    rows = get_db().execute("SELECT company_name, email, created_at, paid_until FROM users ORDER BY id").fetchall()
    out = []
    for r in rows:
        s = access_status(r)
        until = s.get("until")
        out.append({
            "email": (r["email"] or "").lower(),
            "business": r["company_name"] or "",
            "state": s["state"],
            "until": until.isoformat() if until else None,
            "created": _date_str(r["created_at"]),
        })
    return out


def set_paid_until(email, months):
    """Switch an account on for `months` more months (from today or its current end date, whichever is later).

    months <= 0 switches it off. Returns the new end date, or None when switched off.
    Raises LookupError when no account has that email. Call inside an app context.
    """
    db = get_db()
    row = db.execute("SELECT id, paid_until FROM users WHERE email = ?", ((email or "").strip().lower(),)).fetchone()
    if row is None:
        raise LookupError(f"No account with the email {email}.")
    if months <= 0:
        db.execute("UPDATE users SET paid_until = ? WHERE id = ?",
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
    db.execute("UPDATE users SET paid_until = ? WHERE id = ?", (until.isoformat(), row["id"]))
    db.commit()
    return until


_set_paid_until = set_paid_until  # old name, kept as an alias


def _cli_set(email, months):
    try:
        return set_paid_until(email, months)
    except LookupError as exc:
        raise click.ClickException(str(exc))


def register_cli(app):
    @app.cli.command("activate")
    @click.argument("email")
    @click.option("--months", default=1, show_default=True, help="How many paid months to add.")
    def activate(email, months):
        """Switch an account on after a payment."""
        until = _cli_set(email, months)
        if until is None:
            click.echo(f"{email}: switched off")
        else:
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
