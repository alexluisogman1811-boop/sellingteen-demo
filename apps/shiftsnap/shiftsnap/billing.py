"""Free trial and subscription (adapted from ReviewBoost's billing.py).

Every new business gets a free trial (TRIAL_DAYS, default 30). After that, its managers see the
Subscribe page and its employees see a polite "ask your manager" page, until the owner switches
the account on after a payment:

    flask --app shiftsnap accounts                     list businesses and their status
    flask --app shiftsnap activate someone@shop.com    switch on for 1 more month (--months N)
    flask --app shiftsnap deactivate someone@shop.com  switch off (e.g. after a refund)

activate / deactivate also accept the account id shown by `accounts` (useful for a business that
has no email yet, such as one created when an older database was upgraded).

No payment details are stored here: customers pay on the payment provider's page (PAYMENT_LINK).
Billing can be turned off completely with BILLING_ENABLED=0 (for example on a private copy).
All data is kept when a trial or subscription ends.
"""
from datetime import date, datetime, timedelta

import click
from flask import Blueprint, current_app, g, redirect, render_template, request, url_for
from flask.cli import with_appcontext

from .db import get_db

bp = Blueprint("billing", __name__)

# Pages a business can always reach, even after the trial: billing, account (incl. export and
# delete), signing out, password reset, legal pages.
ALWAYS_OPEN = {"billing.subscribe", "billing.paused", "auth.logout", "auth.account", "auth.business",
               "auth.login", "auth.signup", "auth.delete_account", "auth.export_data",
               "auth.forgot_password", "auth.reset_password",
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
    """before_request: when a business's trial or subscription has ended, managers go to the
    Subscribe page and employees to a polite "ask your manager" page. Data is never deleted."""
    g.access = None
    if not current_app.config["BILLING_ENABLED"] or g.get("account") is None:
        return None
    g.access = access_status(g.account)
    if g.access["state"] == "ended" and request.endpoint not in ALWAYS_OPEN:
        if g.user["role"] == "manager":
            return redirect(url_for("billing.subscribe"))
        return redirect(url_for("billing.paused"))
    return None


def _access():
    return g.get("access") or {"state": "trial"}


@bp.route("/subscribe")
def subscribe():
    if g.user["role"] != "manager":
        # Employees never see prices or payment links: subscriptions are their manager's job.
        return redirect(url_for("billing.paused"))
    return render_template("billing/subscribe.html", access=_access(),
                           payment_link=current_app.config["PAYMENT_LINK"],
                           price=current_app.config["PRICE_TEXT"],
                           support_email=current_app.config["SUPPORT_EMAIL"])


@bp.route("/paused")
def paused():
    if g.user["role"] == "manager":
        return redirect(url_for("billing.subscribe"))
    return render_template("billing/paused.html", access=_access())


def _date_str(value):
    return value.isoformat() if value else None


def list_accounts(include_id=False):
    """Every real (non-demo) business with its billing state. Call inside an app context.

    Returns dicts with exactly: email (lowercase), business, state ("trial" | "paid" | "ended"),
    until ("YYYY-MM-DD" or None), created ("YYYY-MM-DD" or None). ShiftSnap has no free plan.
    include_id=True also adds "id" (used by the `accounts` command for businesses without an email).
    """
    rows = get_db().execute("SELECT id, name, email, is_demo, created_at, paid_until FROM accounts "
                            "WHERE is_demo = 0 ORDER BY id").fetchall()
    result = []
    for r in rows:
        s = access_status(r)
        try:
            created = datetime.fromisoformat(str(r["created_at"])).date().isoformat()
        except (TypeError, ValueError):
            created = None
        item = {"email": (r["email"] or "").strip().lower(), "business": r["name"] or "",
                "state": s["state"], "until": _date_str(s.get("until")), "created": created}
        if include_id:
            item["id"] = r["id"]
        result.append(item)
    return result


def set_paid_until(email, months):
    """Switch an account on for `months` more months (31 days each, counted from today or from the
    current paid-until date if that is later), or off when months <= 0 (paid_until = yesterday).
    Returns the new date, or None when switched off. Raises LookupError for an unknown account.
    `email` may also be the account id shown by `flask accounts`."""
    from .auth import find_account
    db = get_db()
    row = find_account((email or "").strip().lower())
    if row is None:
        raise LookupError(f"No account with the email or id {email}.")
    if months <= 0:
        db.execute("UPDATE accounts SET paid_until = ? WHERE id = ?", ((date.today() - timedelta(days=1)).isoformat(), row["id"]))
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
    @with_appcontext
    @click.argument("email")
    @click.option("--months", default=1, show_default=True, help="How many paid months to add.")
    def activate(email, months):
        """Switch an account on after a payment (EMAIL, or the id from `accounts`)."""
        try:
            until = set_paid_until(email, months)
        except LookupError as exc:
            raise click.ClickException(str(exc))
        if until is None:
            click.echo(f"{email}: switched off")
        else:
            click.echo(f"{email}: switched on until {until.isoformat()}")

    @app.cli.command("deactivate")
    @with_appcontext
    @click.argument("email")
    def deactivate(email):
        """Switch an account off, for example after a refund (EMAIL or id)."""
        try:
            set_paid_until(email, 0)
        except LookupError as exc:
            raise click.ClickException(str(exc))
        click.echo(f"{email}: switched off")

    @app.cli.command("accounts")
    @with_appcontext
    def accounts():
        """List businesses and their trial / subscription status."""
        rows = list_accounts(include_id=True)
        if not rows:
            click.echo("No accounts yet.")
        for r in rows:
            extra = f"until {r['until']}" if r["until"] else ""
            click.echo(f"{r['id']:>4}  {(r['email'] or '(no email)'):40} {r['state']:7} {extra:18} {r['business']}")
