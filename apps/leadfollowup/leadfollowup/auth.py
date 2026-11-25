import hashlib
import re
import secrets
import sqlite3
import time

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

from . import mailer
from .db import get_db, utcnow_iso
from .security import safe_next

bp = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 8
MAX_PASSWORD = 256
# Checked when the email is unknown, so failed logins take the same time
# whether or not an account exists (no account enumeration by timing).
_DUMMY_HASH = generate_password_hash("not-a-real-password")


def _start_session(user_id: int):
    session.clear()  # prevent session fixation
    session["user_id"] = user_id
    session.permanent = True


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if g.user:
        return redirect(url_for("leads.dashboard"))
    errors, form = {}, {}
    if request.method == "POST":
        form = {
            "business_name": request.form.get("business_name", "").strip(),
            "email": request.form.get("email", "").strip().lower(),
        }
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if not form["business_name"]:
            errors["business_name"] = "Enter your business name."
        elif len(form["business_name"]) > 100:
            errors["business_name"] = "Business name must be 100 characters or fewer."
        if not EMAIL_RE.match(form["email"]) or len(form["email"]) > 254:
            errors["email"] = "Enter a valid email address."
        if len(password) < MIN_PASSWORD:
            errors["password"] = f"Password must be at least {MIN_PASSWORD} characters."
        elif len(password) > MAX_PASSWORD:
            errors["password"] = f"Password must be {MAX_PASSWORD} characters or fewer."
        elif password != confirm:
            errors["confirm_password"] = "Passwords don't match."

        if not errors:
            db = get_db()
            try:
                cur = db.execute(
                    "INSERT INTO users (email, password_hash, business_name, created_at) VALUES (?, ?, ?, ?)",
                    (form["email"], generate_password_hash(password), form["business_name"], utcnow_iso()),
                )
                db.commit()
            except sqlite3.IntegrityError:
                errors["email"] = "An account with this email already exists. Try logging in instead."
            else:
                _start_session(cur.lastrowid)
                flash(f"Welcome to LeadFollowUp AI, {form['business_name']}!", "success")
                return redirect(url_for("leads.dashboard"))
    status = 400 if errors else 200
    return render_template("auth/signup.html", errors=errors, form=form), status


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("leads.dashboard"))
    error, email = None, ""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = get_db().execute("SELECT id, password_hash FROM users WHERE email = ?", (email,)).fetchone()
        valid = check_password_hash(user["password_hash"] if user else _DUMMY_HASH, password[:MAX_PASSWORD])
        if user is None or not valid:
            error = "Incorrect email or password."
        else:
            _start_session(user["id"])
            return redirect(safe_next(request.args.get("next"), url_for("leads.dashboard")))
    return render_template("auth/login.html", error=error, email=email), (401 if error else 200)


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You've been logged out.", "info")
    return redirect(url_for("auth.login"))


# ---------------------------------------------------------------- optional "Forgot password?" by email
# Only switched on when email is configured (mailer.mail_enabled). When email is off these routes do
# not exist (404) and the login page shows no link; a forgotten password is then reset by the owner.

RESET_MAX_AGE = 3600  # links work for 1 hour
RESET_SENT = "If an account exists for that email, we sent a link to reset your password. It works for 1 hour."
_THROTTLE_WINDOW = 3600
_THROTTLE_PER_IP = 10
_THROTTLE_PER_EMAIL = 3


def mail_on() -> bool:
    return mailer.mail_enabled(current_app.config.get("MAIL"))


def _serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="pw-reset")


def _pw_fingerprint(password_hash: str) -> str:
    # Changes whenever the password changes, so a used link stops working.
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()[:16]


def _throttled(email: str) -> bool:
    """Simple in-memory limit per IP address and per email (per running app)."""
    hits = current_app.extensions.setdefault("leadfollowup_reset_hits", {})
    now = time.time()
    keys = [("ip", request.remote_addr or "?", _THROTTLE_PER_IP), ("email", email, _THROTTLE_PER_EMAIL)]
    blocked = False
    for kind, value, limit in keys:
        recent = [t for t in hits.get((kind, value), []) if now - t < _THROTTLE_WINDOW]
        hits[(kind, value)] = recent
        if len(recent) >= limit:
            blocked = True
    if not blocked:
        for kind, value, _limit in keys:
            hits[(kind, value)].append(now)
    return blocked


def forgot_password():
    if not mail_on():
        abort(404)
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()[:254]
        if EMAIL_RE.match(email) and not _throttled(email):
            user = get_db().execute("SELECT id, email, password_hash, business_name FROM users WHERE email = ?",
                                    (email,)).fetchone()
            if user is not None:
                token = _serializer().dumps({"e": user["email"], "h": _pw_fingerprint(user["password_hash"])})
                link = url_for("auth.reset_password", token=token, _external=True)
                mailer.send_mail(
                    current_app.config["MAIL"], user["email"], "Reset your LeadFollowUp AI password",
                    f"Hi {user['business_name']},\n\nSomeone asked to reset the password for your LeadFollowUp AI "
                    f"account. To choose a new password, open this link (it works for 1 hour):\n\n{link}\n\n"
                    "If you didn't ask for this, you can ignore this email. Your password stays the same.\n",
                )
        # Always the same answer, so nobody can find out which emails have accounts.
        flash(RESET_SENT, "info")
        return redirect(url_for("auth.login"))
    return render_template("auth/forgot.html")


def _user_from_token(token: str):
    try:
        data = _serializer().loads(token, max_age=RESET_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict):
        return None
    user = get_db().execute("SELECT id, password_hash FROM users WHERE email = ?",
                            (str(data.get("e", "")).lower(),)).fetchone()
    if user is None or not secrets.compare_digest(str(data.get("h", "")), _pw_fingerprint(user["password_hash"])):
        return None
    return user


def reset_password(token):
    if not mail_on():
        abort(404)
    user = _user_from_token(token)
    if user is None:
        flash("This reset link is invalid, expired or already used. Please ask for a new one.", "error")
        return redirect(url_for("auth.forgot_password"))
    errors = {}
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if len(password) < MIN_PASSWORD:
            errors["password"] = f"Password must be at least {MIN_PASSWORD} characters."
        elif len(password) > MAX_PASSWORD:
            errors["password"] = f"Password must be {MAX_PASSWORD} characters or fewer."
        elif password != confirm:
            errors["confirm_password"] = "Passwords don't match."
        if not errors:
            db = get_db()
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                       (generate_password_hash(password), user["id"]))
            db.commit()
            session.clear()
            flash("Your password has been changed. Please log in with your new password.", "success")
            return redirect(url_for("auth.login"))
    return render_template("auth/reset.html", errors=errors), (400 if errors else 200)


def register_password_reset(app):
    """Add the reset routes only when email is configured (otherwise they are 404)."""
    if not mailer.mail_enabled(app.config.get("MAIL")):
        return
    app.add_url_rule("/forgot-password", endpoint="auth.forgot_password", view_func=forgot_password,
                     methods=["GET", "POST"])
    app.add_url_rule("/reset-password/<token>", endpoint="auth.reset_password", view_func=reset_password,
                     methods=["GET", "POST"])
