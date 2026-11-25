"""Free trial and subscription (adapted from ReviewBoost for business workspaces).

The trial and the paid status belong to the BUSINESS WORKSPACE, not to a person: every
member of a workspace shares its status. A new workspace gets a free trial (TRIAL_DAYS,
default 14) counted from when it was created. After that the app shows the Subscribe page
until the operator switches the workspace on after a payment:

    flask --app wsgi accounts                       list workspaces and their status
    flask --app wsgi activate owner@roofer.com      switch on for 1 more month (--months N)
    flask --app wsgi deactivate owner@roofer.com    switch off (e.g. after a refund)

Workspaces are looked up by their OWNER's email. No payment details are stored here:
customers pay on the payment provider's page (PAYMENT_LINK). BILLING_ENABLED=0 turns it off.

When a trial or subscription ends nothing is deleted: Owners see the Subscribe page,
other members see "ask your owner", and the Owner can still export or delete the data.
"""
from datetime import date, timedelta, timezone

import click
from flask import Blueprint, current_app, g, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .dates import local_today
from .extensions import db
from .models import ROLE_OWNER, Business, Membership, User

bp = Blueprint("billing", __name__)

# Pages a workspace can always reach after its trial ends: subscribing, logging in/out,
# your own account and password, switching to another workspace, exporting and deleting
# the data, and the legal pages.
ALWAYS_OPEN = {
    "billing.subscribe", "static", "main.home", "main.healthz",
    "main.privacy", "main.terms", "main.refunds",
    "auth.login", "auth.logout", "auth.signup", "auth.join",
    "passwords.reset_password", "passwords.change_password",
    "data.page", "data.export_workspace", "data.delete_workspace", "data.account",
    "workspace.switch", "workspace.join_pending",
}


def _created_date(business) -> date:
    created = business.created_at
    if created is None:
        return local_today()
    if created.tzinfo is None:  # SQLite returns naive UTC datetimes
        created = created.replace(tzinfo=timezone.utc)
    return created.date()


def access_status(business, today: date | None = None) -> dict:
    """state = demo | paid | trial | ended, with the relevant dates."""
    today = today or local_today()
    if business.is_demo:
        return {"state": "demo"}
    until = business.paid_until
    if until and until >= today:
        return {"state": "paid", "until": until, "days_left": (until - today).days}
    trial_end = _created_date(business) + timedelta(days=current_app.config["TRIAL_DAYS"])
    if not until and trial_end >= today:
        return {"state": "trial", "until": trial_end, "days_left": (trial_end - today).days}
    return {"state": "ended", "until": until or trial_end, "was_paid": bool(until)}


def check_access():
    """before_request: send workspaces whose trial or subscription ended to the Subscribe page."""
    g.access = None
    business = g.get("business")
    if not current_app.config.get("BILLING_ENABLED") or business is None:
        return None
    g.access = access_status(business)
    if g.access["state"] == "ended" and request.endpoint not in ALWAYS_OPEN:
        return redirect(url_for("billing.subscribe"))
    return None


def owner_of(business) -> User | None:
    return db.session.execute(
        db.select(User).join(Membership, Membership.user_id == User.id)
        .where(Membership.business_id == business.id, Membership.role == ROLE_OWNER)
        .order_by(Membership.id)
    ).scalars().first()


@bp.route("/subscribe")
@login_required
def subscribe():
    business = g.get("business")
    if business is None:
        return redirect(url_for("data.account"))
    access = g.get("access") or access_status(business)
    is_owner = g.membership is not None and g.membership.role == ROLE_OWNER
    owner = owner_of(business)
    return render_template(
        "billing/subscribe.html",
        access=access,
        is_owner_view=is_owner,
        owner=owner,
        payment_link=current_app.config.get("PAYMENT_LINK", ""),
        price=current_app.config["PRICE_TEXT"],
        support_email=current_app.config.get("SUPPORT_EMAIL", ""),
        billing_enabled=current_app.config.get("BILLING_ENABLED"),
    )


# --- Operator functions (also used by the sellingteen Control Center through Registry) ----------

def _iso(value):
    return value.isoformat() if value else None


def list_accounts():
    """Every real (non-demo) workspace with its billing state. Call inside an app context.

    Each item: email (the owner's), business, state ("trial" | "paid" | "ended"),
    until ("YYYY-MM-DD" or None), created ("YYYY-MM-DD" or None)."""
    businesses = db.session.execute(
        db.select(Business).where(Business.is_demo.is_(False)).order_by(Business.id)
    ).scalars().all()
    result = []
    for b in businesses:
        s = access_status(b, today=date.today())
        owner = owner_of(b)
        result.append({
            "email": (owner.email if owner else "").strip().lower(),
            "business": b.name or "",
            "state": s["state"],
            "until": _iso(s.get("until")),
            "created": _iso(_created_date(b)),
        })
    return result


def _owned_workspace(email: str) -> Business:
    email = (email or "").strip().lower()
    user = db.session.execute(db.select(User).where(User.email == email)).scalar_one_or_none()
    if user is None:
        raise LookupError(f"No account with the email {email}.")
    owned = db.session.execute(
        db.select(Business).join(Membership)
        .where(Membership.user_id == user.id, Membership.role == ROLE_OWNER, Business.is_demo.is_(False))
        .order_by(Business.id)
    ).scalars().all()
    if not owned:
        raise LookupError(f"{email} doesn't own a workspace.")
    if len(owned) > 1:
        names = ", ".join(f"#{b.id} {b.name}" for b in owned)
        raise LookupError(f"{email} owns more than one workspace ({names}); switch one on in the database by id.")
    return owned[0]


def set_paid_until(email, months):
    """Switch the workspace OWNED by `email` on for `months` more months (31 days each), or off when
    months <= 0. Extends from the later of today and the current paid-until date. Returns the new
    date, or None when switched off. Raises LookupError if there's no such owner."""
    business = _owned_workspace(email)
    today = date.today()
    if int(months) <= 0:
        business.paid_until = today - timedelta(days=1)
        db.session.commit()
        return None
    start = max(today, business.paid_until) if business.paid_until else today
    business.paid_until = start + timedelta(days=31 * int(months))
    db.session.commit()
    return business.paid_until


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
        """Switch a workspace on after a payment (looked up by its owner's email)."""
        until = _set_paid_until_cli(email, months)
        click.echo(f"{email}: switched on until {until.isoformat()}")

    @app.cli.command("deactivate")
    @click.argument("email")
    def deactivate(email):
        """Switch a workspace off (for example after a refund)."""
        _set_paid_until_cli(email, 0)
        click.echo(f"{email}: switched off")

    @app.cli.command("accounts")
    def accounts():
        """List workspaces and their trial / subscription status."""
        rows = list_accounts()
        if not rows:
            click.echo("No accounts yet.")
        for r in rows:
            extra = f"until {r['until']}" if r["until"] else ""
            click.echo(f"{r['email']:40} {r['state']:7} {extra:18} {r['business']}")
