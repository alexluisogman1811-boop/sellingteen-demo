import secrets
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required

from . import audit
from .auth import INVITE_SESSION_KEY, find_valid_invitation, hash_token
from .dates import DEFAULT_TZ
from .demo import add_demo_leads, remove_demo_leads
from .extensions import db
from .forms import BusinessForm, InviteForm, RoleForm
from .models import ROLE_ADMIN, ROLE_OWNER, AuditEvent, Invitation, Lead, Membership
from .tenancy import SESSION_KEY, current_business_id, has_role, require_role

bp = Blueprint("workspace", __name__, url_prefix="/workspace")


def _membership_in_current_business(membership_id: int) -> Membership:
    membership = db.session.execute(
        db.select(Membership).where(
            Membership.id == membership_id,
            Membership.business_id == current_business_id(),
        )
    ).scalar_one_or_none()
    if membership is None:
        abort(404)
    return membership


def _team_context(new_link=None):
    business_id = current_business_id()
    memberships = (
        db.session.execute(
            db.select(Membership).where(Membership.business_id == business_id).order_by(Membership.id)
        )
        .scalars()
        .all()
    )
    invites = (
        db.session.execute(
            db.select(Invitation)
            .where(Invitation.business_id == business_id, Invitation.used_at.is_(None))
            .order_by(Invitation.created_at.desc())
        )
        .scalars()
        .all()
    )
    return dict(
        memberships=memberships,
        invites=invites,
        invite_form=InviteForm(),
        role_form=RoleForm(),
        new_link=new_link,
        now=datetime.now(timezone.utc),
    )


@bp.route("/team")
@login_required
def team():
    return render_template("workspace/team.html", **_team_context())


@bp.route("/team/invite", methods=["POST"])
@login_required
@require_role(ROLE_ADMIN)
def create_invite():
    form = InviteForm()
    if not form.validate_on_submit():
        abort(400)
    token = secrets.token_urlsafe(32)
    db.session.add(
        Invitation(
            business_id=current_business_id(),
            token_hash=hash_token(token),
            role=form.role.data,
            created_by_id=current_user.id,
            expires_at=datetime.now(timezone.utc) + timedelta(days=current_app.config["INVITE_VALID_DAYS"]),
        )
    )
    db.session.commit()
    link = url_for("auth.join", token=token, _external=True)
    flash("Invitation link created. Copy it now; it will not be shown again.", "success")
    return render_template("workspace/team.html", **_team_context(new_link=link))


@bp.route("/team/invite/<int:invite_id>/revoke", methods=["POST"])
@login_required
@require_role(ROLE_ADMIN)
def revoke_invite(invite_id):
    invite = db.session.execute(
        db.select(Invitation).where(Invitation.id == invite_id, Invitation.business_id == current_business_id())
    ).scalar_one_or_none()
    if invite is None:
        abort(404)
    db.session.delete(invite)
    db.session.commit()
    flash("Invitation revoked.", "info")
    return redirect(url_for("workspace.team"))


@bp.route("/team/<int:membership_id>/role", methods=["POST"])
@login_required
@require_role(ROLE_OWNER)
def change_role(membership_id):
    membership = _membership_in_current_business(membership_id)
    form = RoleForm()
    if not form.validate_on_submit():
        abort(400)
    if membership.role == ROLE_OWNER:
        flash("The owner's role can't be changed.", "error")
    else:
        membership.role = form.role.data
        db.session.commit()
        flash(f"{membership.user.full_name} is now {membership.role}.", "success")
    return redirect(url_for("workspace.team"))


@bp.route("/team/<int:membership_id>/remove", methods=["POST"])
@login_required
@require_role(ROLE_OWNER)
def remove_member(membership_id):
    membership = _membership_in_current_business(membership_id)
    if membership.role == ROLE_OWNER:
        flash("The owner can't be removed.", "error")
        return redirect(url_for("workspace.team"))
    name = membership.user.full_name
    # Leads stay with the business but are no longer assigned to someone outside it.
    db.session.execute(
        db.update(Lead)
        .where(Lead.business_id == membership.business_id, Lead.assigned_to_id == membership.user_id)
        .values(assigned_to_id=None)
    )
    audit.record(membership.business_id, "member_remove", f"Removed user #{membership.user_id} ({membership.role}).")
    db.session.delete(membership)
    db.session.commit()
    flash(f"{name} was removed from this workspace.", "info")
    return redirect(url_for("workspace.team"))


@bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    form = BusinessForm(obj=g.business)
    valid_zones = {tz for tz, _ in form.timezone.choices}
    current_zone = g.business.timezone or current_app.config.get("APP_TIMEZONE", DEFAULT_TZ)
    if request.method == "GET" or "timezone" not in request.form:
        # Show (and, if the field wasn't posted, keep) the zone currently in effect.
        form.timezone.data = current_zone if current_zone in valid_zones else DEFAULT_TZ
    if request.method == "POST":
        if not has_role(ROLE_OWNER):
            abort(403)
        if form.validate_on_submit():
            g.business.name = form.name.data
            if "timezone" in request.form:
                g.business.timezone = form.timezone.data
            db.session.commit()
            flash("Settings saved.", "success")
            return redirect(url_for("workspace.settings"))
    return render_template("workspace/settings.html", form=form, current_zone=current_zone)


@bp.route("/audit")
@login_required
@require_role(ROLE_OWNER)
def audit_log():
    events = db.session.execute(
        db.select(AuditEvent).where(AuditEvent.business_id == current_business_id())
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(500)
    ).scalars().all()
    return render_template("workspace/audit.html", events=events)


@bp.route("/demo/add", methods=["POST"])
@login_required
@require_role(ROLE_ADMIN)
def demo_add():
    count = add_demo_leads(g.business, current_user)
    flash(f"Added {count} DEMO leads. They are clearly labelled and can be removed any time.", "success")
    return redirect(url_for("leads.index"))


@bp.route("/demo/remove", methods=["POST"])
@login_required
@require_role(ROLE_ADMIN)
def demo_remove():
    count = remove_demo_leads(g.business)
    if count:
        audit.record(g.business.id, "demo_remove", f"{count} demo lead(s).")
        db.session.commit()
    flash(f"Removed {count} demo leads.", "info")
    return redirect(url_for("workspace.settings"))


@bp.route("/switch", methods=["POST"])
@login_required
def switch():
    raw = request.form.get("business_id", "")
    if not raw.isdigit():
        abort(400)
    business_id = int(raw)
    if business_id not in {m.business_id for m in g.memberships}:
        abort(404)
    session[SESSION_KEY] = business_id
    return redirect(url_for("main.dashboard"))


@bp.route("/join", methods=["GET", "POST"])
@login_required
def join_pending():
    invite = find_valid_invitation(session.get(INVITE_SESSION_KEY))
    if invite is None:
        session.pop(INVITE_SESSION_KEY, None)
        flash("That invitation is no longer valid.", "error")
        return redirect(url_for("main.dashboard"))
    already = any(m.business_id == invite.business_id for m in g.memberships)
    if request.method == "POST":
        # CSRF is enforced globally by CSRFProtect for this POST.
        if not already:
            db.session.add(Membership(user_id=current_user.id, business_id=invite.business_id, role=invite.role))
            invite.used_at = datetime.now(timezone.utc)
            invite.used_by_id = current_user.id
            db.session.commit()
        session.pop(INVITE_SESSION_KEY, None)
        session[SESSION_KEY] = invite.business_id
        flash(f"You joined {invite.business.name}.", "success")
        return redirect(url_for("main.dashboard"))
    return render_template("workspace/join.html", invite=invite, already=already)
