"""Password change and one-time reset links (no email is ever sent).

* Anyone can change their own password (needs the current one); every other device is signed out.
* A workspace Owner can create a one-time, 24-hour reset link for a team member and send it
  themselves (text, phone call...). To stop one owner from taking over an account that also
  belongs to someone else's workspace, this only works for members whose ONLY workspace is
  the owner's, and never for another owner.
* Support can create a link for anyone, e.g. an Owner who forgot their own password, with
  `flask --app wsgi reset-link owner@email` after confirming who they are (see docs/SUPPORT.md).
"""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from . import audit
from .extensions import db
from .forms import ChangePasswordForm, ResetPasswordForm
from .models import ROLE_OWNER, Membership, PasswordReset, User

bp = Blueprint("passwords", __name__)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def owner_can_reset(membership: Membership) -> tuple[bool, str]:
    """Can the active workspace's owner create a reset link for this membership?"""
    if membership.user_id == current_user.id:
        return False, "Use Change password for your own account."
    if membership.role == ROLE_OWNER:
        return False, "Owners can't reset another owner's password."
    others = db.session.execute(
        db.select(db.func.count()).select_from(Membership)
        .where(Membership.user_id == membership.user_id, Membership.business_id != membership.business_id)
    ).scalar()
    if others:
        return False, ("This person also belongs to another workspace, so only LeadRescue support can reset "
                       "their password.")
    return True, ""


def create_reset_link(user: User, business_id: int | None, created_by_id: int | None) -> str:
    """Create a one-time reset token for `user` and return the token (shown once, never stored)."""
    token = secrets.token_urlsafe(32)
    hours = current_app.config.get("PASSWORD_RESET_HOURS", 24)
    db.session.add(PasswordReset(
        token_hash=_hash(token), user_id=user.id, business_id=business_id, created_by_id=created_by_id,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=hours),
    ))
    return token


def find_valid_reset(token: str | None) -> PasswordReset | None:
    if not token or len(token) > 200:
        return None
    reset = db.session.execute(
        db.select(PasswordReset).where(PasswordReset.token_hash == _hash(token))
    ).scalar_one_or_none()
    if reset is None or reset.used_at is not None or _aware(reset.expires_at) < datetime.now(timezone.utc):
        return None
    if reset.business_id is not None:
        # An owner-created link stops working if the person left the workspace, became an
        # owner, or joined another workspace since the link was made.
        memberships = db.session.execute(
            db.select(Membership).where(Membership.user_id == reset.user_id)
        ).scalars().all()
        if [m.business_id for m in memberships] != [reset.business_id] or memberships[0].role == ROLE_OWNER:
            return None
    return reset


@bp.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    form = ChangePasswordForm()
    if form.validate_on_submit():
        if not current_user.check_password(form.current_password.data):
            form.current_password.errors.append("That isn't your current password.")
        else:
            user = db.session.get(User, current_user.id)
            user.set_password(form.password.data)
            user.rotate_session_token()   # every other device is signed out...
            db.session.commit()
            business_id = session.get("active_business_id")
            session.clear()
            login_user(user)              # ...and this one stays signed in with the new token
            session.permanent = True
            if business_id:
                session["active_business_id"] = business_id
            flash("Password changed. You've been signed out on every other device.", "success")
            return redirect(url_for("data.account"))
    return render_template("passwords/change.html", form=form)


@bp.route("/workspace/team/<int:membership_id>/reset-link", methods=["POST"])
@login_required
def create_member_reset(membership_id):
    if g.membership is None or g.membership.role != ROLE_OWNER:
        abort(403)
    membership = db.session.execute(
        db.select(Membership).where(Membership.id == membership_id, Membership.business_id == g.membership.business_id)
    ).scalar_one_or_none()
    if membership is None:
        abort(404)
    ok, reason = owner_can_reset(membership)
    if not ok:
        flash(reason, "error")
        return redirect(url_for("workspace.team"))
    token = create_reset_link(membership.user, membership.business_id, current_user.id)
    audit.record(membership.business_id, "password_reset_link", f"For user #{membership.user_id}.")
    db.session.commit()
    link = url_for("passwords.reset_password", token=token, _external=True)
    from .workspace import _team_context
    flash(f"Password reset link created for {membership.user.full_name}. Copy it now; it won't be shown again.", "success")
    return render_template("workspace/team.html", **_team_context(), reset_link=link, reset_for=membership.user.full_name)


@bp.route("/reset/<token>", methods=["GET", "POST"])
def reset_password(token):
    reset = find_valid_reset(token)
    if reset is None:
        flash("That password reset link is invalid, expired or already used. Ask for a new one.", "error")
        return redirect(url_for("auth.login"))
    form = ResetPasswordForm()
    if form.validate_on_submit():
        user = reset.user
        user.set_password(form.password.data)
        user.rotate_session_token()
        user.failed_logins = 0
        user.locked_until = None
        reset.used_at = datetime.now(timezone.utc)
        db.session.commit()
        if current_user.is_authenticated:
            logout_user()
        session.clear()
        flash("Your password was changed. Log in with the new password.", "success")
        return redirect(url_for("auth.login"))
    return render_template("passwords/reset.html", form=form, email=reset.user.email)
