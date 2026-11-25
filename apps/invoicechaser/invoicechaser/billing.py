"""Plans: free trial, Free plan and Pro.

* Trial: every new account gets TRIAL_DAYS (default 30, from users.created_at) with everything unlimited.
* Free: after the trial, an account that hasn't paid keeps using the whole app, but can have at most
  FREE_CUSTOMER_LIMIT customers (default 5). Nobody is ever locked out: existing customers, invoices and
  follow-ups always keep working; only adding another customer asks them to upgrade.
* Pro (paid): unlimited, switched on by the owner after a payment:

    flask --app run accounts                         list accounts and their plan
    flask --app run activate someone@business.com    switch Pro on for 1 more month (--months N)
    flask --app run deactivate someone@business.com  switch Pro off, back to Free (e.g. after a refund)

No payment details are stored here: customers pay on the payment provider's page (PAYMENT_LINK).
Billing can be turned off completely with BILLING_ENABLED=0 (for example on a private copy).
"""
from datetime import date, datetime, timedelta

import click
from flask import Blueprint, current_app, g, render_template

from .db import get_db
from .util import login_required

bp = Blueprint("billing", __name__)


def _created_date(user):
    try:
        return datetime.fromisoformat(str(user["created_at"])).date()
    except (TypeError, ValueError, KeyError, IndexError):
        return date.today()


def access_status(user, today=None):
    """Returns a dict: state = paid | trial | free, with the relevant dates."""
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
    # Trial over (or Pro lapsed): the Free plan. Never a lock-out.
    return {"state": "free", "until": until or trial_end, "was_paid": bool(paid_until)}


def customer_count(user_id):
    return get_db().execute("SELECT COUNT(*) FROM customers WHERE user_id = ?", (user_id,)).fetchone()[0]


def check_access():
    """before_request: work out the signed-in account's plan for this request (stored in g.access)."""
    g.access = None
    if not current_app.config["BILLING_ENABLED"] or g.get("user") is None:
        return None
    row = get_db().execute("SELECT created_at, paid_until FROM users WHERE id = ?", (g.user["id"],)).fetchone()
    if row is None:
        return None
    g.access = access_status(row)
    if g.access["state"] == "free":
        g.access["limit"] = current_app.config["FREE_CUSTOMER_LIMIT"]
        g.access["customers"] = customer_count(g.user["id"])
    return None


def free_plan_full():
    """True when the account is on the Free plan and already has its maximum number of customers."""
    access = g.get("access")
    return bool(access and access["state"] == "free"
                and customer_count(g.user["id"]) >= current_app.config["FREE_CUSTOMER_LIMIT"])


def pro_price():
    """Short price for buttons and banners: '$9 per month' -> '$9/month'."""
    return current_app.config["PRICE_TEXT"].replace(" per month", "/month")


def upgrade_page(status=200):
    """Friendly 'upgrade to add more customers' page; existing data is untouched."""
    return render_template("billing/upgrade.html", limit=current_app.config["FREE_CUSTOMER_LIMIT"],
                           customers=customer_count(g.user["id"])), status


@bp.route("/subscribe")
@login_required
def subscribe():
    return render_template("billing/subscribe.html", access=g.get("access") or {"state": "trial"},
                           payment_link=current_app.config["PAYMENT_LINK"],
                           price=current_app.config["PRICE_TEXT"],
                           limit=current_app.config["FREE_CUSTOMER_LIMIT"],
                           support_email=current_app.config["SUPPORT_EMAIL"])


def _iso(value):
    return value.isoformat() if value else None


def _is_demo(row):
    """Demo accounts come from seed_demo.py: demo@... emails and '(demo)' business names."""
    email = (row["email"] or "").strip().lower()
    return email.startswith("demo@") or (row["business_name"] or "").strip().endswith("(demo)")


def list_accounts():
    """All real (non-demo) accounts with their plan. Call inside an app context.

    Each item: email, business, state ("trial" | "paid" | "free"), until ("YYYY-MM-DD" or None),
    created ("YYYY-MM-DD" or None). InvoiceChaser has a Free plan, so an unpaid account whose trial is
    over is "free" (never "ended"): it keeps working with up to FREE_CUSTOMER_LIMIT customers.
    """
    rows = get_db().execute(
        "SELECT email, business_name, created_at, paid_until FROM users ORDER BY id").fetchall()
    result = []
    for r in rows:
        if _is_demo(r):
            continue
        s = access_status(r)
        try:
            created = datetime.fromisoformat(str(r["created_at"])).date().isoformat()
        except (TypeError, ValueError):
            created = None
        until = s.get("until") if s["state"] in ("trial", "paid") else None
        result.append({"email": (r["email"] or "").strip().lower(), "business": r["business_name"] or "",
                       "state": s["state"], "until": _iso(until), "created": created})
    return result


def set_paid_until(email, months):
    """Switch Pro on for `months` more months (31 days each), or off when months <= 0.

    Extends from the later of today and the current paid-until date. Returns the new date,
    or None when switched off (the account goes back to the Free plan). Raises LookupError
    if no account has this email.
    """
    db = get_db()
    row = db.execute("SELECT id, paid_until FROM users WHERE lower(email) = ?",
                     ((email or "").strip().lower(),)).fetchone()
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
        """Switch Pro on for an account after a payment."""
        until = _set_paid_until_cli(email, months)
        click.echo(f"{email}: switched on until {until.isoformat()}")

    @app.cli.command("deactivate")
    @click.argument("email")
    def deactivate(email):
        """Switch Pro off (for example after a refund). The account moves to the Free plan."""
        _set_paid_until_cli(email, 0)
        click.echo(f"{email}: switched off (now on the Free plan)")

    @app.cli.command("accounts")
    def accounts():
        """List accounts and their plan: trial, free or paid (Pro)."""
        rows = list_accounts()
        if not rows:
            click.echo("No accounts yet.")
        limit = app.config["FREE_CUSTOMER_LIMIT"]
        counts = {r["email"]: r["customers"] for r in get_db().execute(
            "SELECT lower(u.email) AS email, (SELECT COUNT(*) FROM customers c WHERE c.user_id = u.id) AS customers"
            " FROM users u").fetchall()}
        for r in rows:
            extra = f"until {r['until']}" if r["until"] else f"{counts.get(r['email'], 0)}/{limit} customers"
            click.echo(f"{r['email']:40} {r['state']:7} {extra:18} {r['business']}")
