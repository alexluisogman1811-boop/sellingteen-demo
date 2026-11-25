import hashlib
import re
import sqlite3
import time

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

from . import mailer
from .db import get_db, now_iso
from .security import end_session, start_session

bp = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 8
# Used to keep login timing similar whether or not the email exists.
_DUMMY_HASH = generate_password_hash("not-a-real-password")


def _attempts():
    """Failed logins per (client IP, email), kept in memory for this process."""
    store = current_app.extensions.setdefault("jobproof_login_failures", {})
    return store


def _key(email):
    return (request.remote_addr or "?", email)


def _recent(email):
    window = current_app.config["LOGIN_WINDOW_SECONDS"]
    now = time.monotonic()
    times = [t for t in _attempts().get(_key(email), []) if now - t < window]
    _attempts()[_key(email)] = times
    return times


def _too_many_failures(email):
    return len(_recent(email)) >= current_app.config["LOGIN_MAX_FAILURES"]


def _record_failure(email):
    _recent(email).append(time.monotonic())


def _clear_failures(email):
    _attempts().pop(_key(email), None)


def _safe_next(target):
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None


def validate_signup(email, password, confirm):
    errors = []
    if not email or not EMAIL_RE.match(email) or len(email) > 254:
        errors.append("Enter a valid email address.")
    if len(password) < MIN_PASSWORD:
        errors.append(f"Password must be at least {MIN_PASSWORD} characters.")
    elif len(password) > 128:
        errors.append("Password must be 128 characters or fewer.")
    if password != confirm:
        errors.append("Passwords do not match.")
    return errors


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if g.user:
        return redirect(url_for("dashboard.index"))
    form = {"email": ""}
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        form["email"] = email
        errors = validate_signup(email, password, confirm)
        db = get_db()
        if not errors:
            try:
                cur = db.execute(
                    "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
                    (email, generate_password_hash(password), now_iso()),
                )
                db.execute(
                    "INSERT INTO businesses (user_id, email, updated_at) VALUES (?, ?, ?)",
                    (cur.lastrowid, email, now_iso()),
                )
                db.commit()
            except sqlite3.IntegrityError:
                db.rollback()
                errors.append("An account with that email already exists. Try logging in instead.")
            else:
                start_session(cur.lastrowid)
                flash("Welcome to JobProof AI! Start by adding your business details.", "success")
                return redirect(url_for("profile.edit", welcome=1))
        for e in errors:
            flash(e, "error")
        return render_template("auth/signup.html", form=form), 400
    return render_template("auth/signup.html", form=form)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("dashboard.index"))
    form = {"email": ""}
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        form["email"] = email
        if _too_many_failures(email):
            flash("Too many failed login attempts. Please wait 15 minutes and try again.", "error")
            return render_template("auth/login.html", form=form), 429
        user = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        valid = check_password_hash(user["password_hash"] if user else _DUMMY_HASH, password)
        if user and valid:
            _clear_failures(email)
            start_session(user["id"])
            return redirect(_safe_next(request.args.get("next")) or url_for("dashboard.index"))
        _record_failure(email)
        flash("Email or password is incorrect.", "error")
        return render_template("auth/login.html", form=form), 401
    return render_template("auth/login.html", form=form)


@bp.route("/logout", methods=["POST"])
def logout():
    end_session()
    flash("You have been logged out.", "success")
    return redirect(url_for("auth.login"))


# ---------------------------------------------------------------- optional email password reset

RESET_MAX_AGE = 60 * 60  # links work for 1 hour
RESET_LIMIT = 3          # requests per IP and per email ...
RESET_WINDOW = 60 * 60   # ... per hour


def mail_on():
    return mailer.mail_enabled(current_app.config.get("MAIL"))


@bp.app_context_processor
def _mail_context():
    return {"mail_enabled": mail_on()}


def _serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="pw-reset")


def _pw_fingerprint(password_hash):
    # Changes whenever the password changes, so a used link stops working.
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()[:16]


def _reset_throttled(email):
    store = current_app.extensions.setdefault("jobproof_reset_requests", {})
    now = time.monotonic()
    blocked = False
    for key in (("ip", request.remote_addr or "?"), ("email", email)):
        times = [t for t in store.get(key, []) if now - t < RESET_WINDOW]
        if len(times) >= RESET_LIMIT:
            blocked = True
        times.append(now)
        store[key] = times
    return blocked


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if not mail_on():
        abort(404)
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if email and len(email) <= 254 and not _reset_throttled(email):
            user = get_db().execute("SELECT email, password_hash FROM users WHERE email = ?", (email,)).fetchone()
            if user:
                token = _serializer().dumps({"e": user["email"], "p": _pw_fingerprint(user["password_hash"])})
                link = url_for("auth.reset_password", token=token, _external=True)
                mailer.send_mail(current_app.config["MAIL"], user["email"], "Reset your JobProof AI password",
                                 "Someone asked to reset the password for your JobProof AI account.\n\n"
                                 f"Set a new password here (the link works for 1 hour):\n{link}\n\n"
                                 "If this wasn't you, you can ignore this email. Your password has not changed.")
        # Always the same answer, so the page never tells anyone which emails have accounts.
        return render_template("auth/forgot.html", sent=True)
    return render_template("auth/forgot.html", sent=False)


def _user_from_token(token):
    try:
        data = _serializer().loads(token, max_age=RESET_MAX_AGE)
    except (SignatureExpired, BadSignature):
        return None
    if not isinstance(data, dict):
        return None
    user = get_db().execute("SELECT id, email, password_hash FROM users WHERE email = ?",
                            (str(data.get("e", "")),)).fetchone()
    if user is None or data.get("p") != _pw_fingerprint(user["password_hash"]):
        return None
    return user


@bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    if not mail_on():
        abort(404)
    user = _user_from_token(token)
    if user is None:
        return render_template("auth/reset.html", invalid=True, token=token), 400
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        errors = [e for e in validate_signup(user["email"], password, confirm) if "email" not in e]
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("auth/reset.html", invalid=False, token=token), 400
        db = get_db()
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(password), user["id"]))
        db.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))  # sign out everywhere
        db.commit()
        _clear_failures(user["email"])
        flash("Your password has been changed. Log in with your new password.", "success")
        return redirect(url_for("auth.login"))
    return render_template("auth/reset.html", invalid=False, token=token)
