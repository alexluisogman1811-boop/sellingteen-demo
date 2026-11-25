import hashlib
import hmac
from datetime import datetime, timedelta, timezone

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from .extensions import db
from .forms import LoginForm, SignupForm
from .models import ROLE_OWNER, Business, Invitation, Membership, User
from .security import is_safe_redirect, with_script_root
from .tenancy import SESSION_KEY

bp = Blueprint("auth", __name__)

INVITE_SESSION_KEY = "pending_invite"

# Used to equalise timing when the email doesn't exist (avoids user enumeration).
_DUMMY_HASH = None


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def find_valid_invitation(token: str | None) -> Invitation | None:
    if not token or len(token) > 200:
        return None
    invite = db.session.execute(
        db.select(Invitation).where(Invitation.token_hash == hash_token(token))
    ).scalar_one_or_none()
    if invite is None or invite.used_at is not None:
        return None
    expires = invite.expires_at
    if expires.tzinfo is None:  # SQLite returns naive datetimes
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < datetime.now(timezone.utc):
        return None
    return invite


def _start_session(user: User, business_id: int | None = None) -> None:
    # Clear any pre-login session data to prevent session fixation.
    pending_invite = session.get(INVITE_SESSION_KEY)
    session.clear()
    if pending_invite:
        session[INVITE_SESSION_KEY] = pending_invite
    if not user.session_token:
        user.ensure_session_token()
        db.session.commit()
    login_user(user)
    # Permanent sessions carry a signed timestamp; Flask rejects them after
    # PERMANENT_SESSION_LIFETIME (12 h), so a copied cookie can't live forever.
    session.permanent = True
    if business_id:
        session[SESSION_KEY] = business_id


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    invite = find_valid_invitation(session.get(INVITE_SESSION_KEY))
    form = SignupForm()
    required_code = current_app.config.get("SIGNUP_ACCESS_CODE", "")
    needs_code = bool(required_code) and not invite
    if form.validate_on_submit():
        if needs_code and not hmac.compare_digest((form.access_code.data or "").encode(), required_code.encode()):
            form.access_code.errors.append("That pilot access code isn't right.")
        elif not invite and not form.business_name.data:
            form.business_name.errors.append("Enter your business name.")
        elif db.session.execute(db.select(User).where(User.email == form.email.data)).scalar_one_or_none():
            # Generic message; don't confirm which emails are registered beyond this form.
            form.email.errors.append("That email can't be used. Try logging in instead.")
        else:
            user = User(email=form.email.data, full_name=form.full_name.data,
                        terms_accepted_at=datetime.now(timezone.utc))
            user.set_password(form.password.data)
            db.session.add(user)
            if invite:
                business = invite.business
                db.session.add(Membership(user=user, business=business, role=invite.role))
                invite.used_at = datetime.now(timezone.utc)
                db.session.flush()
                invite.used_by_id = user.id
            else:
                business = Business(name=form.business_name.data)
                db.session.add(business)
                db.session.add(Membership(user=user, business=business, role=ROLE_OWNER))
            db.session.commit()
            session.pop(INVITE_SESSION_KEY, None)
            _start_session(user, business.id)
            flash(f"Welcome to LeadRescue, {user.full_name}!", "success")
            return redirect(url_for("main.dashboard"))
    return render_template("auth/signup.html", form=form, invite=invite, needs_code=needs_code)


@bp.route("/login", methods=["GET", "POST"])
def login():
    global _DUMMY_HASH
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    form = LoginForm()
    if form.validate_on_submit():
        user = db.session.execute(db.select(User).where(User.email == form.email.data)).scalar_one_or_none()
        now = datetime.now(timezone.utc)
        error = "Invalid email or password."
        if user is None:
            if _DUMMY_HASH is None:
                tmp = User(email="", full_name="")
                tmp.set_password("timing-equaliser")
                _DUMMY_HASH = tmp
            _DUMMY_HASH.check_password(form.password.data)
        else:
            locked_until = user.locked_until
            if locked_until is not None and locked_until.tzinfo is None:
                locked_until = locked_until.replace(tzinfo=timezone.utc)
            if locked_until and locked_until > now:
                error = "Too many failed attempts. Try again in a few minutes."
            elif user.check_password(form.password.data) and user.is_active:
                user.failed_logins = 0
                user.locked_until = None
                db.session.commit()
                _start_session(user)
                next_url = request.args.get("next")
                if session.get(INVITE_SESSION_KEY):
                    return redirect(url_for("workspace.join_pending"))
                return redirect(with_script_root(next_url) if is_safe_redirect(next_url) else url_for("main.dashboard"))
            else:
                user.failed_logins = (user.failed_logins or 0) + 1
                if user.failed_logins >= current_app.config["LOGIN_MAX_FAILURES"]:
                    user.locked_until = now + timedelta(minutes=current_app.config["LOGIN_LOCKOUT_MINUTES"])
                    user.failed_logins = 0
                db.session.commit()
        flash(error, "error")
    return render_template("auth/login.html", form=form)


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    # Rotate the session token so any copy of this session cookie stops working
    # (this signs the user out on every device).
    current_user.rotate_session_token()
    db.session.commit()
    logout_user()
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))


@bp.route("/join/<token>")
def join(token):
    invite = find_valid_invitation(token)
    if invite is None:
        flash("That invitation link is invalid, expired or already used.", "error")
        return redirect(url_for("auth.login"))
    session[INVITE_SESSION_KEY] = token
    if current_user.is_authenticated:
        return redirect(url_for("workspace.join_pending"))
    flash(f"You've been invited to join {invite.business.name}. Create an account or log in to accept.", "info")
    return redirect(url_for("auth.signup"))
