import hashlib
import hmac
import re
import secrets
import time
from datetime import datetime, timezone
from functools import wraps

from flask import (Blueprint, abort, current_app, flash, g, redirect, render_template, request,
                   session, url_for)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

from . import mailer
from .db import get_db

bp = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[A-Za-z]{2,}$")
MIN_PASSWORD, MAX_PASSWORD = 8, 128
MAX_FAILURES, LOCKOUT_SECONDS = 5, 300
_failures = {}  # email -> (count, first_failure_time); in-memory, per process

# A real hash to compare against when the email is unknown, so response time
# doesn't reveal whether an account exists.
_DUMMY_HASH = generate_password_hash("not-a-real-password")


def csrf_token():
    token = session.get("csrf")
    if not token:
        token = session["csrf"] = secrets.token_urlsafe(32)
    return token


def check_csrf():
    if request.method != "POST":
        return
    sent = request.form.get("csrf_token", "")
    expected = session.get("csrf", "")
    if not sent or not expected or not hmac.compare_digest(sent, expected):
        abort(400, "This form expired. Go back, refresh the page and try again.")


def password_fingerprint(password_hash):
    """Short one-way tag of the password hash, kept in the session so a password
    change logs out every other browser."""
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()[:16]


def load_user():
    g.user = None
    user_id = session.get("user_id")
    if user_id is not None:
        row = get_db().execute(
            "SELECT id, email, business_name, password_hash FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None or session.get("pw") != password_fingerprint(row["password_hash"]):
            session.clear()
            return
        g.user = {"id": row["id"], "email": row["email"], "business_name": row["business_name"]}


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            # script_root keeps the address prefix when the app runs under one (e.g. /payrecover)
            return redirect(url_for("auth.login", next=_here()))
        return view(*args, **kwargs)
    return wrapped


def _here():
    """The current address including any prefix (e.g. /payrecover) and query string."""
    path = request.full_path
    if path.endswith("?"):
        path = path[:-1]
    return request.script_root + path


def _safe_next(target):
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("routes.dashboard")


def start_session(user_id, password_hash):
    session.clear()  # new session on login prevents session fixation
    session["user_id"] = user_id
    session["pw"] = password_fingerprint(password_hash)
    session.permanent = True
    csrf_token()


def locked_out(email):
    count, first = _failures.get(email, (0, 0))
    if count and time.time() - first > LOCKOUT_SECONDS:
        _failures.pop(email, None)
        return False
    return count >= MAX_FAILURES


def record_failure(email):
    count, first = _failures.get(email, (0, time.time()))
    _failures[email] = (count + 1, first)


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if g.user:
        return redirect(url_for("routes.dashboard"))
    form = {"business_name": "", "email": ""}
    errors = {}
    if request.method == "POST":
        form["business_name"] = request.form.get("business_name", "").strip()
        form["email"] = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not 1 <= len(form["business_name"]) <= 80:
            errors["business_name"] = "Enter your business name (up to 80 characters)."
        if not EMAIL_RE.match(form["email"]) or len(form["email"]) > 254:
            errors["email"] = "Enter a valid email address."
        if not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
            errors["password"] = f"Use at least {MIN_PASSWORD} characters."
        db = get_db()
        if not errors and db.execute("SELECT 1 FROM users WHERE email = ?", (form["email"],)).fetchone():
            errors["email"] = "An account with this email already exists. Try logging in."
        if not errors:
            password_hash = generate_password_hash(password)
            cur = db.execute(
                "INSERT INTO users (email, password_hash, business_name, created_at) VALUES (?, ?, ?, ?)",
                (form["email"], password_hash, form["business_name"],
                 datetime.now(timezone.utc).isoformat()),
            )
            db.commit()
            start_session(cur.lastrowid, password_hash)
            flash("Welcome to PayRecover! Start by adding a customer you've invoiced.", "success")
            return redirect(url_for("routes.dashboard"))
    return render_template("signup.html", form=form, errors=errors), (400 if errors else 200)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("routes.dashboard"))
    email = ""
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()[:254]
        password = request.form.get("password", "")[:MAX_PASSWORD]
        if locked_out(email):
            error = "Too many failed attempts. Please wait 5 minutes and try again."
        else:
            user = get_db().execute(
                "SELECT id, password_hash FROM users WHERE email = ?", (email,)
            ).fetchone()
            ok = check_password_hash(user["password_hash"] if user else _DUMMY_HASH, password)
            if user and ok:
                _failures.pop(email, None)
                start_session(user["id"], user["password_hash"])
                return redirect(_safe_next(request.args.get("next")))
            record_failure(email)
            error = "Email or password is incorrect."
    return render_template("login.html", email=email, error=error), (401 if error else 200)


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You've been logged out.", "success")
    return redirect(url_for("auth.login"))


# ---------------------------------------------------------------- forgot password (optional email)

RESET_MAX_AGE = 3600  # links work for 1 hour
RESET_LIMIT, RESET_WINDOW = 3, 3600  # at most 3 requests per email / per IP address each hour
RESET_SENT = "If an account exists for that email, we sent a link to reset your password. It works for 1 hour."
_reset_requests = {}  # key -> list of request times; in-memory, per process


def email_enabled():
    return mailer.mail_enabled(current_app.config.get("MAIL"))


def _serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="pw-reset")


def _throttled(*keys):
    now = time.time()
    blocked = False
    for key in keys:
        recent = [t for t in _reset_requests.get(key, []) if now - t < RESET_WINDOW]
        if len(recent) >= RESET_LIMIT:
            blocked = True
        _reset_requests[key] = recent
    if not blocked:
        for key in keys:
            _reset_requests[key].append(now)
    return blocked


@bp.route("/forgot", methods=["GET", "POST"])
def forgot_password():
    if not email_enabled():
        abort(404)
    if g.user:
        return redirect(url_for("routes.dashboard"))
    sent = False
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()[:254]
        sent = True  # always the same answer, so nobody can find out which emails have accounts
        if EMAIL_RE.match(email) and not _throttled("email:" + email, "ip:" + (request.remote_addr or "")):
            user = get_db().execute("SELECT email, password_hash FROM users WHERE email = ?", (email,)).fetchone()
            if user:
                token = _serializer().dumps({"e": user["email"], "p": password_fingerprint(user["password_hash"])})
                link = url_for("auth.reset_password", token=token, _external=True)
                mailer.send_mail(current_app.config["MAIL"], user["email"], "Reset your PayRecover password",
                                 "Someone (hopefully you) asked to reset your PayRecover password.\n\n"
                                 f"Open this link to choose a new password. It works for 1 hour:\n{link}\n\n"
                                 "If you didn't ask for this, you can ignore this email. Your password stays the same.")
    return render_template("forgot.html", sent=sent, message=RESET_SENT)


def _user_from_token(token):
    try:
        data = _serializer().loads(token, max_age=RESET_MAX_AGE)
    except (SignatureExpired, BadSignature):
        return None
    if not isinstance(data, dict):
        return None
    user = get_db().execute("SELECT id, email, password_hash FROM users WHERE email = ?",
                            (str(data.get("e", "")),)).fetchone()
    if user is None or not hmac.compare_digest(str(data.get("p", "")), password_fingerprint(user["password_hash"])):
        return None  # unknown account, or the password changed since (so a used link stops working)
    return user


@bp.route("/reset/<token>", methods=["GET", "POST"])
def reset_password(token):
    if not email_enabled():
        abort(404)
    user = _user_from_token(token)
    if user is None:
        return render_template("reset.html", invalid=True, error=None), 400
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        if not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
            error = f"Use at least {MIN_PASSWORD} characters."
        elif password != request.form.get("confirm_password", ""):
            error = "The two passwords don't match."
        else:
            password_hash = generate_password_hash(password)
            db = get_db()
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user["id"]))
            db.commit()
            _failures.pop(user["email"], None)
            session.clear()  # every browser has to log in with the new password
            flash("Your password has been changed. You can log in now.", "success")
            return redirect(url_for("auth.login"))
    return render_template("reset.html", invalid=False, error=error), (400 if error else 200)
