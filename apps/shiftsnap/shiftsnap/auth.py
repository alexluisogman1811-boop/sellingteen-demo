"""Sign-up, sign-in, roles and permissions.

Many businesses share one ShiftSnap. Each business is an *account*; everything it stores
(employees, shifts, sign-ins, settings) carries its account id, and a signed-in user only ever
works inside their own account (see services.aid()).

Two roles inside a business:
  * manager  - full access (employees, shifts, exports, demo/data tools, sign-ins, subscription).
  * employee - read-only: their own dashboard, the team schedule without other people's
               notes/warnings/contact details, and their own password.

Sessions are Flask's signed cookies holding the user id and a `session_version`. Bumping a
user's session_version (password reset, sign-in removed) invalidates every existing session.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import secrets
import sqlite3
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from functools import wraps

import click
from flask.cli import with_appcontext
from flask import (Blueprint, Response, abort, current_app, flash, g, redirect, render_template,
                   request, session, url_for)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

from . import mailer
from .csrf import SESSION_KEY as CSRF_SESSION_KEY
from .db import get_db

bp = Blueprint("auth", __name__)

USERNAME_RE = re.compile(r"^[A-Za-z0-9._@-]{3,40}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_EMAIL = 100
MAX_BUSINESS_NAME = 100
MIN_PASSWORD = 10
MAX_PASSWORD = 200
LOCKOUT_WINDOW = timedelta(minutes=15)
MAX_FAILURES_PER_USER = 5
MAX_FAILURES_PER_IP = 20
SIGNUP_WINDOW = timedelta(hours=1)
MAX_SIGNUPS_PER_IP = 5
# Pages anyone can open without signing in. ("main.dashboard" is "/", which shows the public
# landing page to visitors and the dashboard to signed-in users.)
PUBLIC_ENDPOINTS = {"auth.login", "auth.signup", "auth.forgot_password", "auth.reset_password",
                    "static", "healthz", "main.dashboard", "legal.terms", "legal.privacy", "legal.refunds"}
# Endpoints a user who must change their password may still reach.
PASSWORD_CHANGE_ENDPOINTS = {"auth.account", "auth.logout", "auth.delete_account", "auth.export_data",
                             "static", "healthz",
                             "legal.terms", "legal.privacy", "legal.refunds"}

# Compared against when the username doesn't exist, so response time doesn't reveal it.
_DUMMY_HASH = generate_password_hash(secrets.token_hex(16))


# ---- passwords & users --------------------------------------------------------

def hash_password(password: str) -> str:
    method = current_app.config.get("PASSWORD_HASH_METHOD")
    return generate_password_hash(password, method=method) if method else generate_password_hash(password)


def validate_password(password: str, username: str = "") -> str | None:
    """Return an error message, or None if the password is acceptable."""
    if len(password) < MIN_PASSWORD:
        return f"Password must be at least {MIN_PASSWORD} characters."
    if len(password) > MAX_PASSWORD:
        return f"Password must be {MAX_PASSWORD} characters or fewer."
    if username and password.lower() == username.lower():
        return "Password must not be the same as the username."
    return None


def validate_username(username: str) -> str | None:
    if not USERNAME_RE.match(username or ""):
        return "Username must be 3–40 characters: letters, digits, dot, dash, underscore or @."
    return None


def validate_email(email: str) -> str | None:
    if not email or len(email) > MAX_EMAIL or not EMAIL_RE.match(email):
        return "Enter a valid email address."
    return None


def get_user(user_id: int):
    """A user by id (any business): only used for the session's own user and CLI tools."""
    return get_db().execute(
        "SELECT u.*, e.name AS employee_name, e.active AS employee_active "
        "FROM users u LEFT JOIN employees e ON e.id = u.employee_id AND e.account_id = u.account_id "
        "WHERE u.id = ?", (user_id,)).fetchone()


def get_user_by_username(username: str):
    return get_db().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def get_user_for_employee(employee_id: int):
    """The sign-in linked to an employee of the *current* business (None for other businesses)."""
    from .services import aid
    return get_db().execute("SELECT * FROM users WHERE employee_id = ? AND account_id = ?",
                            (employee_id, aid())).fetchone()


def username_taken(username: str) -> bool:
    """Usernames are unique across ShiftSnap; a business's email is reserved as its manager's username."""
    db = get_db()
    return bool(db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
                or db.execute("SELECT 1 FROM accounts WHERE email = ?", (username,)).fetchone())


def create_account(name: str, email: str | None) -> int:
    db = get_db()
    with db:
        return db.execute("INSERT INTO accounts (name, email) VALUES (?, ?)", (name, email)).lastrowid


def create_user(username: str, password: str, role: str, employee_id: int | None = None,
                must_change_password: bool = False, account_id: int | None = None) -> int:
    """Create a sign-in in `account_id` (default: the current business)."""
    if account_id is None:
        from .services import aid
        account_id = aid()
    db = get_db()
    with db:
        cur = db.execute(
            "INSERT INTO users (account_id, username, password_hash, role, employee_id, must_change_password) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (account_id, username, hash_password(password), role, employee_id, int(must_change_password)))
    return cur.lastrowid


def set_password(user_id: int, password: str, must_change: bool = False) -> None:
    """Change a password and sign out every existing session for that user."""
    db = get_db()
    with db:
        db.execute("UPDATE users SET password_hash = ?, must_change_password = ?, "
                   "session_version = session_version + 1 WHERE id = ?",
                   (hash_password(password), int(must_change), user_id))


def delete_user(user_id: int) -> None:
    from .services import aid
    db = get_db()
    with db:
        db.execute("DELETE FROM users WHERE id = ? AND account_id = ?", (user_id, aid()))


def user_can_sign_in(user) -> bool:
    if not user or not user["active"]:
        return False
    if user["employee_id"] is not None:
        emp = get_db().execute("SELECT active FROM employees WHERE id = ? AND account_id = ?",
                               (user["employee_id"], user["account_id"])).fetchone()
        if user["role"] == "employee" or emp is not None:
            return bool(emp and emp["active"])
    return True


# ---- throttling -----------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _client_ip() -> str:
    return request.remote_addr or "unknown"


def is_throttled(username: str) -> bool:
    since = (_now() - LOCKOUT_WINDOW).isoformat()
    db = get_db()
    per_user = db.execute("SELECT COUNT(*) FROM login_failures WHERE username = ? AND failed_at >= ?",
                          (username, since)).fetchone()[0]
    per_ip = db.execute("SELECT COUNT(*) FROM login_failures WHERE ip = ? AND failed_at >= ?",
                        (_client_ip(), since)).fetchone()[0]
    return per_user >= MAX_FAILURES_PER_USER or per_ip >= MAX_FAILURES_PER_IP


def record_failure(username: str) -> None:
    db = get_db()
    user = get_user_by_username(username) if username else None
    with db:
        db.execute("INSERT INTO login_failures (account_id, username, ip, failed_at) VALUES (?, ?, ?, ?)",
                   (user["account_id"] if user else None, username[:100], _client_ip(), _now().isoformat()))
        db.execute("DELETE FROM login_failures WHERE failed_at < ?",
                   ((_now() - timedelta(days=1)).isoformat(),))


def clear_failures(username: str) -> None:
    db = get_db()
    with db:
        db.execute("DELETE FROM login_failures WHERE username = ?", (username,))


def signups_blocked() -> bool:
    since = (_now() - SIGNUP_WINDOW).isoformat()
    return get_db().execute("SELECT COUNT(*) FROM signup_attempts WHERE ip = ? AND attempted_at >= ?",
                            (_client_ip(), since)).fetchone()[0] >= MAX_SIGNUPS_PER_IP


def record_signup() -> None:
    db = get_db()
    with db:
        db.execute("INSERT INTO signup_attempts (ip, attempted_at) VALUES (?, ?)",
                   (_client_ip(), _now().isoformat()))
        db.execute("DELETE FROM signup_attempts WHERE attempted_at < ?",
                   ((_now() - timedelta(days=1)).isoformat(),))


# ---- request hooks & decorators ---------------------------------------------------

def load_current_user():
    g.user = None
    g.account = None
    g.account_id = None
    uid = session.get("user_id")
    if uid is None:
        return
    user = get_user(uid)
    if not user or user["session_version"] != session.get("sv") or not user_can_sign_in(user):
        session.clear()
        flash("Your session has ended. Please sign in again.", "warning")
        return
    g.user = user
    g.account_id = user["account_id"]
    g.account = get_db().execute("SELECT * FROM accounts WHERE id = ?", (user["account_id"],)).fetchone()


def login_url_with_next():
    """The sign-in page, returning afterwards to this page. script_root keeps the address prefix
    (e.g. /shiftsnap) when ShiftSnap runs under one."""
    here = request.script_root + request.full_path
    return url_for("auth.login", next=here[:-1] if here.endswith("?") else here)


def require_login():
    endpoint = request.endpoint or ""
    if endpoint == "":
        return None
    if g.user is None:
        if endpoint in PUBLIC_ENDPOINTS:
            return None
        if request.method == "GET":
            return redirect(login_url_with_next())
        abort(401)
    if g.user["must_change_password"] and endpoint not in PASSWORD_CHANGE_ENDPOINTS:
        flash("Please choose a new password before continuing.", "warning")
        return redirect(url_for("auth.account"))
    return None


def is_manager() -> bool:
    return g.get("user") is not None and g.user["role"] == "manager"


def manager_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_manager():
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def _start_session(user) -> None:
    """New session on every sign-in (prevents session fixation) with a fresh CSRF token."""
    session.clear()
    session.permanent = True
    session["user_id"] = user["id"]
    session["sv"] = user["session_version"]
    session[CSRF_SESSION_KEY] = secrets.token_urlsafe(32)


# ---- routes -----------------------------------------------------------------------

@bp.route("/signup", methods=["GET", "POST"])
def signup():
    """Create a new business (account) and its first manager, then sign them in."""
    if g.user is not None:
        return redirect(url_for("main.dashboard"))
    form = {"business_name": "", "email": ""}
    errors: dict[str, str] = {}
    status = 200
    if request.method == "POST":
        form = {"business_name": " ".join((request.form.get("business_name") or "").split()),
                "email": (request.form.get("email") or "").strip().lower()}
        password = request.form.get("password") or ""
        if signups_blocked():
            errors["form"] = "Too many new accounts from your connection. Please try again in an hour."
            status = 429
        else:
            if not form["business_name"]:
                errors["business_name"] = "Business name is required."
            elif len(form["business_name"]) > MAX_BUSINESS_NAME:
                errors["business_name"] = f"Business name must be {MAX_BUSINESS_NAME} characters or fewer."
            if (msg := validate_email(form["email"])):
                errors["email"] = msg
            elif username_taken(form["email"]):
                errors["email"] = "An account with this email already exists. Try signing in instead."
            if (msg := validate_password(password, form["email"])):
                errors["password"] = msg
            if not errors:
                record_signup()
                try:
                    account_id = create_account(form["business_name"], form["email"])
                    user_id = create_user(form["email"], password, "manager", account_id=account_id)
                except sqlite3.IntegrityError:  # someone took the email a moment ago
                    errors["email"] = "An account with this email already exists. Try signing in instead."
                else:
                    _start_session(get_user(user_id))
                    flash(f"Welcome to ShiftSnap, {form['business_name']}! Your "
                          f"{current_app.config['TRIAL_DAYS']}-day free trial has started. "
                          "Add your first employee to get going.", "success")
                    return redirect(url_for("main.dashboard"))
            status = 400
    return render_template("signup.html", form=form, errors=errors,
                           trial_days=current_app.config["TRIAL_DAYS"]), status


@bp.route("/login", methods=["GET", "POST"])
def login():
    from .views import _safe_next  # local import avoids a cycle
    if g.user is not None:
        return redirect(url_for("main.dashboard"))
    error = None
    username = ""
    status = 200
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()[:100]
        password = request.form.get("password") or ""
        if is_throttled(username):
            error = "Too many failed sign-in attempts. Wait 15 minutes and try again."
            status = 429
        else:
            user = get_user_by_username(username) if username else None
            ok = check_password_hash(user["password_hash"] if user else _DUMMY_HASH, password)
            if ok and user_can_sign_in(user):
                clear_failures(username)
                db = get_db()
                with db:
                    db.execute("UPDATE users SET last_login_at = ? WHERE id = ?",
                               (_now().isoformat(timespec="seconds"), user["id"]))
                _start_session(user)
                return redirect(_safe_next(url_for("main.dashboard")))
            record_failure(username)
            # Same message for unknown user, wrong password and disabled account.
            error = "Incorrect username or password, or this account is disabled."
            status = 401
    return render_template("login.html", error=error, username=username), status


@bp.post("/logout")
def logout():
    session.clear()
    flash("You have been signed out.", "success")
    return redirect(url_for("auth.login"))


@bp.route("/account", methods=["GET", "POST"])
def account():
    errors: dict[str, str] = {}
    if request.method == "POST":
        current = request.form.get("current_password") or ""
        new = request.form.get("new_password") or ""
        confirm = request.form.get("confirm_password") or ""
        if not check_password_hash(g.user["password_hash"], current):
            errors["current_password"] = "Current password is incorrect."
        elif (msg := validate_password(new, g.user["username"])):
            errors["new_password"] = msg
        elif new != confirm:
            errors["confirm_password"] = "The two new passwords don't match."
        elif check_password_hash(g.user["password_hash"], new):
            errors["new_password"] = "Choose a password different from the current one."
        if not errors:
            set_password(g.user["id"], new)
            _start_session(get_user(g.user["id"]))  # keep this session, end all others
            flash("Password changed. Other devices have been signed out.", "success")
            return redirect(url_for("main.dashboard"))
    return render_template("account.html", errors=errors, business_errors={},
                           business=_business_form()), (400 if errors else 200)


def _business_form() -> dict:
    return {"name": g.account["name"], "email": g.account["email"] or ""}


# ---- export all data / delete the account ---------------------------------------------------

EXPORT_TABLES = (
    # (file name, SQL, columns). Never exports password hashes.
    ("business.csv", "SELECT id, name, email, paid_until, created_at FROM accounts WHERE id = ?",
     ("id", "name", "email", "paid_until", "created_at")),
    ("employees.csv", "SELECT id, name, email, phone, role, notes, active, track_availability, created_at "
                      "FROM employees WHERE account_id = ? ORDER BY id",
     ("id", "name", "email", "phone", "role", "notes", "active", "track_availability", "created_at")),
    ("availability.csv", "SELECT employee_id, weekday, start_time, end_time FROM availability "
                         "WHERE account_id = ? ORDER BY employee_id, weekday",
     ("employee_id", "weekday", "start_time", "end_time")),
    ("shifts.csv", "SELECT s.id, s.shift_date, s.start_time, s.end_time, s.employee_id, e.name AS employee_name, "
                   "s.role, s.notes, s.created_at FROM shifts s LEFT JOIN employees e "
                   "ON e.id = s.employee_id AND e.account_id = s.account_id WHERE s.account_id = ? "
                   "ORDER BY s.shift_date, s.start_time, s.id",
     ("id", "shift_date", "start_time", "end_time", "employee_id", "employee_name", "role", "notes", "created_at")),
    ("sign_ins.csv", "SELECT id, username, role, employee_id, active, created_at, last_login_at FROM users "
                     "WHERE account_id = ? ORDER BY id",
     ("id", "username", "role", "employee_id", "active", "created_at", "last_login_at")),
    ("settings.csv", "SELECT key, value FROM settings WHERE account_id = ? ORDER BY key", ("key", "value")),
)


@bp.get("/account/export.zip")
@manager_required
def export_data():
    """All of this business's data as a ZIP of CSV files (works after the trial ends too)."""
    from .views import _csv_cell
    db = get_db()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, sql, cols in EXPORT_TABLES:
            out = io.StringIO()
            w = csv.writer(out)
            w.writerow(cols)
            for row in db.execute(sql, (g.account_id,)):
                w.writerow([_csv_cell(row[c]) for c in cols])
            z.writestr(name, "﻿" + out.getvalue())
    filename = f"shiftsnap-export-{_now().date().isoformat()}.zip"
    return Response(buf.getvalue(), mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def delete_account_data(account_id: int) -> None:
    """Delete every row that belongs to one business. (ShiftSnap stores no uploaded files.)"""
    db = get_db()
    with db:
        usernames = [r["username"] for r in db.execute("SELECT username FROM users WHERE account_id = ?",
                                                       (account_id,))]
        for table in ("availability", "shifts", "users", "settings", "login_failures", "employees"):
            db.execute(f"DELETE FROM {table} WHERE account_id = ?", (account_id,))
        for name in usernames:
            db.execute("DELETE FROM login_failures WHERE username = ?", (name,))
        db.execute("DELETE FROM accounts WHERE id = ?", (account_id,))


@bp.route("/account/delete", methods=["GET", "POST"])
@manager_required
def delete_account():
    """Delete the whole business: needs the password and the word DELETE (or the business email)."""
    errors: dict[str, str] = {}
    if request.method == "POST":
        typed = (request.form.get("confirm") or "").strip()
        password = request.form.get("password") or ""
        email = (g.account["email"] or "").lower()
        if typed != "DELETE" and not (email and typed.lower() == email):
            errors["confirm"] = "Type DELETE (or your business email) to confirm."
        if not check_password_hash(g.user["password_hash"], password):
            errors["password"] = "Password is incorrect."
        if not errors:
            delete_account_data(g.account_id)
            session.clear()
            flash("Your account and all of its data have been deleted.", "success")
            return redirect(url_for("main.dashboard"))
    return render_template("account_delete.html", errors=errors), (400 if errors else 200)


# ---- optional "forgot password" by email (only when email is set up) -------------------------

RESET_MAX_AGE = 3600  # seconds: links work for 1 hour
RESET_WINDOW = 3600
RESET_MAX_PER_IP = 5
RESET_MAX_PER_EMAIL = 3
RESET_SENT_MESSAGE = "If an account exists, we sent a link to that email to reset the password. It works for 1 hour."
_reset_lock = threading.Lock()


def mail_on() -> bool:
    return mailer.mail_enabled(current_app.config.get("MAIL"))


def _reset_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.secret_key, salt="pw-reset")


def _hash_fingerprint(password_hash: str) -> str:
    """Short hash of the current password hash: the link stops working once the password changes."""
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()[:16]


def _reset_throttled(email: str) -> bool:
    """Simple in-memory limit per IP address and per email (per process)."""
    now = time.time()
    hits = current_app.extensions.setdefault("shiftsnap_reset_hits", {})
    with _reset_lock:
        for key in list(hits):
            hits[key] = [t for t in hits[key] if now - t < RESET_WINDOW]
            if not hits[key]:
                del hits[key]
        ip_key, email_key = "ip:" + _client_ip(), "email:" + email
        if len(hits.get(ip_key, [])) >= RESET_MAX_PER_IP or len(hits.get(email_key, [])) >= RESET_MAX_PER_EMAIL:
            return True
        hits.setdefault(ip_key, []).append(now)
        hits.setdefault(email_key, []).append(now)
        return False


def _user_for_reset_email(email: str):
    """The sign-in for this email: a user whose username is the email, else the business's manager."""
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE username = ?", (email,)).fetchone()
    if user is None:
        account = db.execute("SELECT id FROM accounts WHERE email = ?", (email,)).fetchone()
        if account is not None:
            user = db.execute("SELECT * FROM users WHERE account_id = ? AND role = 'manager' AND active = 1 "
                              "ORDER BY id LIMIT 1", (account["id"],)).fetchone()
    return user if user is not None and user_can_sign_in(user) else None


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if not mail_on():
        abort(404)
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()[:MAX_EMAIL]
        if email and not validate_email(email) and not _reset_throttled(email):
            user = _user_for_reset_email(email)
            if user is not None:
                token = _reset_serializer().dumps(
                    {"u": user["id"], "e": email, "h": _hash_fingerprint(user["password_hash"])})
                link = url_for("auth.reset_password", token=token, _external=True)
                mailer.send_mail(current_app.config["MAIL"], email, "Reset your ShiftSnap password",
                                 "Someone (hopefully you) asked to reset the password for "
                                 f"{user['username']} on ShiftSnap.\n\n"
                                 f"Open this link to choose a new password. It works for 1 hour, once:\n{link}\n\n"
                                 "If you did not ask for this, ignore this email. Your password stays the same.")
        flash(RESET_SENT_MESSAGE, "success")
        return redirect(url_for("auth.login"))
    return render_template("forgot_password.html")


@bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    if not mail_on():
        abort(404)
    user = None
    try:
        data = _reset_serializer().loads(token, max_age=RESET_MAX_AGE)
        user = get_user(int(data["u"]))
        if user is None or data.get("h") != _hash_fingerprint(user["password_hash"]) or not user_can_sign_in(user):
            user = None
    except (BadSignature, SignatureExpired, KeyError, TypeError, ValueError):
        user = None
    if user is None:
        return render_template("reset_password.html", invalid=True, errors={}), 400
    errors: dict[str, str] = {}
    if request.method == "POST":
        new = request.form.get("new_password") or ""
        confirm = request.form.get("confirm_password") or ""
        if (msg := validate_password(new, user["username"])):
            errors["new_password"] = msg
        elif new != confirm:
            errors["confirm_password"] = "The two new passwords don't match."
        if not errors:
            set_password(user["id"], new)  # also signs out every existing session
            clear_failures(user["username"])
            flash("Password changed. You can sign in now.", "success")
            return redirect(url_for("auth.login"))
    return render_template("reset_password.html", invalid=False, errors=errors), (400 if errors else 200)


@bp.post("/account/business")
@manager_required
def business():
    """Managers can rename their business and change its contact / billing email."""
    from . import services
    form = {"name": " ".join((request.form.get("name") or "").split()),
            "email": (request.form.get("email") or "").strip().lower()}
    errors: dict[str, str] = {}
    if not form["name"]:
        errors["name"] = "Business name is required."
    elif len(form["name"]) > MAX_BUSINESS_NAME:
        errors["name"] = f"Business name must be {MAX_BUSINESS_NAME} characters or fewer."
    if (msg := validate_email(form["email"])):
        errors["email"] = msg
    elif form["email"] != (g.account["email"] or "").lower() and (
            get_db().execute("SELECT 1 FROM accounts WHERE email = ? AND id != ?",
                             (form["email"], g.account_id)).fetchone()
            or get_db().execute("SELECT 1 FROM users WHERE username = ? AND account_id != ?",
                                (form["email"], g.account_id)).fetchone()):
        errors["email"] = "Another account already uses this email."
    if not errors:
        try:
            services.update_account(form["name"], form["email"])
        except sqlite3.IntegrityError:
            errors["email"] = "Another account already uses this email."
        else:
            flash("Business details saved.", "success")
            return redirect(url_for("auth.account"))
    return render_template("account.html", errors={}, business_errors=errors, business=form), 400


# ---- CLI --------------------------------------------------------------------------

def _cli_password(password: str | None) -> str:
    return password or click.prompt("Password", hide_input=True, confirmation_prompt=True)


def find_account(ref: str):
    """An account by its email or its numeric id (as shown by `flask accounts`)."""
    db = get_db()
    ref = (ref or "").strip()
    if ref.isdigit():
        return db.execute("SELECT * FROM accounts WHERE id = ?", (int(ref),)).fetchone()
    return db.execute("SELECT * FROM accounts WHERE email = ?", (ref.lower(),)).fetchone()


@click.command("create-manager")
@with_appcontext
@click.argument("username")
@click.option("--password", default=None, help="Omit to be prompted (recommended).")
@click.option("--account", "account_ref", default=None,
              help="Add the manager to this existing business (its email or id from `accounts`).")
@click.option("--business", default=None, help="Name for a new business (default: My business).")
@click.option("--email", default=None, help="Contact email for a new business (default: USERNAME if it is an email).")
def create_manager_command(username, password, account_ref, business, email):
    """Create a manager sign-in.

    Without --account this creates a NEW business, just like the sign-up page. Exception: a
    database upgraded from the single-business version whose business has no manager yet - the
    first manager then joins that business, so it keeps its existing data.
    """
    if (msg := validate_username(username)) and validate_email(username.lower()):
        raise click.ClickException(msg)
    if username_taken(username):
        raise click.ClickException(f"User '{username}' already exists.")
    db = get_db()
    if account_ref:
        account = find_account(account_ref)
        if account is None:
            raise click.ClickException(f"No business '{account_ref}'. See `flask accounts`.")
    else:
        account = db.execute("SELECT a.* FROM accounts a WHERE NOT EXISTS (SELECT 1 FROM users u "
                             "WHERE u.account_id = a.id AND u.role = 'manager') ORDER BY a.id LIMIT 1").fetchone()
        if account is not None and business is None and email is None:
            click.echo(f"Adding to existing business '{account['name']}' (id {account['id']}), "
                       "which had no manager yet.")
        else:
            account = None
    password = _cli_password(password)
    if (msg := validate_password(password, username)):
        raise click.ClickException(msg)
    if account is None:
        email = (email or (username if not validate_email(username.lower()) else "")).strip().lower() or None
        if email and (msg := validate_email(email)):
            raise click.ClickException(msg)
        if email and find_account(email):
            raise click.ClickException(f"A business with the email {email} already exists.")
        account_id = create_account((business or "My business").strip()[:MAX_BUSINESS_NAME], email)
        click.echo(f"Business '{business or 'My business'}' created (id {account_id}).")
    else:
        account_id = account["id"]
    create_user(username, password, "manager", account_id=account_id)
    click.echo(f"Manager '{username}' created.")


@click.command("reset-password")
@with_appcontext
@click.argument("username")
@click.option("--password", default=None, help="Omit to be prompted (recommended).")
def reset_password_command(username, password):
    """Reset any user's password (signs out their sessions)."""
    user = get_user_by_username(username)
    if not user:
        raise click.ClickException(f"No user '{username}'.")
    password = _cli_password(password)
    if (msg := validate_password(password, username)):
        raise click.ClickException(msg)
    set_password(user["id"], password)
    clear_failures(username)
    click.echo(f"Password for '{username}' reset.")


@click.command("list-users")
@with_appcontext
def list_users_command():
    """List sign-ins with their business (never prints password hashes)."""
    for u in get_db().execute("SELECT id, account_id, username, role, employee_id, active, last_login_at "
                              "FROM users ORDER BY account_id, role, username"):
        click.echo(f"{u['id']:>4}  business={u['account_id']:<4} {u['username']:<30} {u['role']:<9} "
                   f"employee={u['employee_id']} active={u['active']} last_login={u['last_login_at'] or '-'}")


def init_app(app):
    app.before_request(load_current_user)
    app.before_request(require_login)
    app.cli.add_command(create_manager_command)
    app.cli.add_command(reset_password_command)
    app.cli.add_command(list_users_command)

    @app.context_processor
    def inject_user():
        return {"current_user": g.get("user"), "current_account": g.get("account"),
                "is_manager": is_manager(), "mail_on": mail_on()}
