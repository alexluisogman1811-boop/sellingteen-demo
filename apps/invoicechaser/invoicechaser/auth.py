import hashlib
import re
import secrets

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

from . import mailer
from .db import get_db
from .util import CURRENCIES, limiter, safe_next, valid_timezone

bp = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_FAILURES = 5
MIN_PASSWORD = 8
# A hash to compare against when the email is unknown, so response time
# does not reveal which emails have accounts.
_DUMMY_HASH = generate_password_hash(secrets.token_hex(16))


def login_limiter():
    return limiter("login", LOGIN_MAX_FAILURES, LOGIN_WINDOW_SECONDS)


def start_session(user):
    session.clear()  # prevent session fixation
    session.permanent = True
    session["user_id"] = user["id"]
    session["session_version"] = user["session_version"]
    session["csrf_token"] = secrets.token_urlsafe(32)


def validate_password(password):
    if len(password) < MIN_PASSWORD or len(password) > 200:
        return f"Password must be at least {MIN_PASSWORD} characters."
    return None


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if g.user:
        return redirect(url_for("dashboard.index"))
    form = request.form
    errors = {}
    status = 200
    if request.method == "POST":
        business = " ".join(form.get("business_name", "").split())
        email = form.get("email", "").strip().lower()
        password = form.get("password", "")
        currency = form.get("currency", "USD")
        tz = form.get("timezone", "")
        if not valid_timezone(tz):
            tz = "UTC"
        signup_limit = limiter("signup", 10, 60 * 60)
        if signup_limit.blocked(request.remote_addr):
            errors["form"] = "Too many sign-ups from this network. Please try again later."
            status = 429
        else:
            if not business or len(business) > 120:
                errors["business_name"] = "Enter your business name (max 120 characters)."
            if not EMAIL_RE.match(email) or len(email) > 254:
                errors["email"] = "Enter a valid email address, like name@business.com."
            if pw_error := validate_password(password):
                errors["password"] = pw_error
            if currency not in CURRENCIES:
                errors["currency"] = "Choose a currency."
            if not errors:
                db = get_db()
                if db.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
                    errors["email"] = "An account with this email already exists. Try logging in instead."
                else:
                    cur = db.execute(
                        "INSERT INTO users (email, password_hash, business_name, currency, timezone) VALUES (?, ?, ?, ?, ?)",
                        (email, generate_password_hash(password), business, currency, tz),
                    )
                    db.commit()
                    signup_limit.hit(request.remote_addr)
                    start_session(db.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone())
                    flash("Your account is ready. Let's add your first customer.", "success")
                    return redirect(url_for("dashboard.index"))
            status = 400
    return render_template("auth/signup.html", form=form, errors=errors, currencies=CURRENCIES), status


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("dashboard.index"))
    error = None
    status = 200
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        keys = (f"ip:{request.remote_addr}", f"email:{email}")
        limit = login_limiter()
        if any(limit.blocked(k) for k in keys):
            error = "Too many failed attempts. Please wait 15 minutes and try again."
            status = 429
        else:
            user = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            ok = check_password_hash(user["password_hash"] if user else _DUMMY_HASH, password)
            if user and ok:
                for k in keys:
                    limit.reset(k)
                start_session(user)
                return redirect(safe_next(request.args.get("next")) or url_for("dashboard.index"))
            for k in keys:
                limit.hit(k)
            error = "That email and password don't match. Please try again."
            status = 401
    return render_template("auth/login.html", error=error, email=request.form.get("email", "")), status


@bp.post("/logout")
def logout():
    session.clear()
    flash("You've been logged out.", "info")
    return redirect(url_for("auth.login"))


# Forgot password (only when the optional email is switched on) ---------------------------

RESET_MAX_AGE_SECONDS = 60 * 60
RESET_SENT_MESSAGE = "If an account exists, we sent a link to reset your password. It works for 1 hour."


def _mail_on():
    return mailer.mail_enabled(current_app.config.get("MAIL"))


def _reset_serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="pw-reset")


def _hash_tag(password_hash):
    # Short fingerprint of the current password hash: the link stops working once the password changes.
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if not _mail_on():
        abort(404)
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        per_email = limiter("reset-email", 3, LOGIN_WINDOW_SECONDS)
        per_ip = limiter("reset-ip", 10, LOGIN_WINDOW_SECONDS)
        ip = request.remote_addr or "unknown"
        if EMAIL_RE.match(email) and not per_email.blocked(email) and not per_ip.blocked(ip):
            per_email.hit(email)
            per_ip.hit(ip)
            row = get_db().execute("SELECT email, password_hash FROM users WHERE email = ?", (email,)).fetchone()
            if row:
                token = _reset_serializer().dumps({"e": row["email"], "h": _hash_tag(row["password_hash"])})
                link = url_for("auth.reset_password", token=token, _external=True)
                mailer.send_mail(current_app.config["MAIL"], row["email"], "Reset your InvoiceChaser password",
                                 "Someone asked to reset the password for your InvoiceChaser account.\n\n"
                                 f"To choose a new password, open this link (it works for 1 hour):\n{link}\n\n"
                                 "If this wasn't you, you can ignore this email. Your password has not changed.")
        # Always the same answer, so nobody can find out which emails have an account.
        flash(RESET_SENT_MESSAGE, "info")
        return redirect(url_for("auth.login"))
    return render_template("auth/forgot_password.html")


@bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    if not _mail_on():
        abort(404)
    try:
        data = _reset_serializer().loads(token, max_age=RESET_MAX_AGE_SECONDS)
    except (SignatureExpired, BadSignature):
        data = None
    db = get_db()
    row = None
    if isinstance(data, dict):
        row = db.execute("SELECT id, email, password_hash FROM users WHERE email = ?",
                         (str(data.get("e", "")),)).fetchone()
    if row is None or data.get("h") != _hash_tag(row["password_hash"]):
        flash("This reset link is not valid any more. Please ask for a new one.", "error")
        return redirect(url_for("auth.forgot_password"))
    errors = {}
    if request.method == "POST":
        new = request.form.get("new_password", "")
        if pw_error := validate_password(new):
            errors["new_password"] = pw_error
        elif new != request.form.get("confirm_password", ""):
            errors["confirm_password"] = "The two passwords don't match."
        if not errors:
            # session_version + 1 signs out every device that was logged in.
            db.execute("UPDATE users SET password_hash = ?, session_version = session_version + 1 WHERE id = ?",
                       (generate_password_hash(new), row["id"]))
            db.commit()
            login_limiter().reset(f"email:{row['email']}")
            session.clear()
            flash("Your password has been changed. Please log in.", "success")
            return redirect(url_for("auth.login"))
    return render_template("auth/reset_password.html", token=token, errors=errors), (400 if errors else 200)
