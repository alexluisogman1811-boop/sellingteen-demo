"""Tenant isolation helpers.

All access to tenant data goes through these functions. They resolve the
active business from the session *and* re-check the user's membership on
every request, so a forged or stale session value can never grant access.
"""
from functools import wraps

from flask import abort, g, session
from flask_login import current_user

from .extensions import db
from .models import ROLE_RANK, Activity, Lead, Membership, User

SESSION_KEY = "active_business_id"


def load_active_membership():
    """Populate g.membership / g.business for the logged-in user."""
    g.membership = None
    g.business = None
    if not current_user.is_authenticated:
        return
    memberships = (
        db.session.execute(
            db.select(Membership).where(Membership.user_id == current_user.id).order_by(Membership.id)
        )
        .scalars()
        .all()
    )
    g.memberships = memberships
    if not memberships:
        return
    wanted = session.get(SESSION_KEY)
    chosen = next((m for m in memberships if m.business_id == wanted), memberships[0])
    session[SESSION_KEY] = chosen.business_id
    g.membership = chosen
    g.business = chosen.business


def current_business_id() -> int:
    membership = getattr(g, "membership", None)
    if membership is None:
        abort(403)
    return membership.business_id


def require_role(minimum: str):
    """Decorator: the user's role in the active business must be >= minimum."""

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            membership = getattr(g, "membership", None)
            if membership is None or ROLE_RANK[membership.role] < ROLE_RANK[minimum]:
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


def has_role(minimum: str) -> bool:
    membership = getattr(g, "membership", None)
    return membership is not None and ROLE_RANK[membership.role] >= ROLE_RANK[minimum]


def leads_query():
    """Base SELECT for leads, always scoped to the active business."""
    return db.select(Lead).where(Lead.business_id == current_business_id())


def get_lead_or_404(lead_id: int) -> Lead:
    # Lookups by id are always combined with the tenant filter. A lead that
    # belongs to another business is indistinguishable from a missing one (404),
    # which prevents IDOR and avoids leaking which ids exist.
    lead = db.session.execute(
        leads_query().where(Lead.id == lead_id)
    ).scalar_one_or_none()
    if lead is None:
        abort(404)
    return lead


def get_activity_or_404(lead: Lead, activity_id: int) -> Activity:
    activity = db.session.execute(
        db.select(Activity).where(
            Activity.id == activity_id,
            Activity.lead_id == lead.id,
            Activity.business_id == current_business_id(),
        )
    ).scalar_one_or_none()
    if activity is None:
        abort(404)
    return activity


def business_members(business_id: int | None = None):
    business_id = business_id or current_business_id()
    return (
        db.session.execute(
            db.select(User)
            .join(Membership, Membership.user_id == User.id)
            .where(Membership.business_id == business_id)
            .order_by(User.full_name)
        )
        .scalars()
        .all()
    )


def member_ids(business_id: int | None = None) -> set[int]:
    return {u.id for u in business_members(business_id)}
