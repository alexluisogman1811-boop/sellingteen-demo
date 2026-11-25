"""Business accounts: sign-up, log in, log out, account settings.

Each business signs up with its name, email and password. Passwords are stored only
as salted hashes (werkzeug: scrypt). Every page except the public ones (landing,
sign-up, log-in, legal pages, /healthz) needs a signed-in account, and every query
is filtered by that account's id (see current_account_id()).

ContentQueue never asks for Instagram/TikTok/Facebook/LinkedIn/X/YouTube passwords
and never connects to those networks.
"""
import csv
import hashlib
import io
import os
import re
import shutil
import sqlite3
import time
import zipfile

from flask import (Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, session,
                   url_for)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

from . import mailer, timeutil
from .db import SCOPED_TABLES, assign_unowned_data, get_db, now_iso
from .uploads import delete_upload, is_stored_name

bp = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LENGTH = 8
MAX_BUSINESS_NAME = 100
# Checked when the email is unknown, so response time doesn't reveal which emails exist.
DUMMY_HASH = generate_password_hash("not-a-real-password")

# In-memory brute-force protection (per app process), as in the other sellingteen apps.
LOGIN_WINDOW_SECONDS = 15 * 60
MAX_FAILURES_PER_EMAIL = 10
MAX_FAILURES_PER_IP = 30
_failed_logins = {}
# "Forgot password" requests (only when the optional email is switched on).
MAX_RESETS_PER_EMAIL = 3
MAX_RESETS_PER_IP = 10
RESET_MAX_AGE_SECONDS = 60 * 60
_reset_requests = {}
RESET_SENT_MESSAGE = "If an account exists, we sent a link to reset your password. It works for 1 hour."
DELETE_WORD = "DELETE"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

DEFAULT_TRUSTED_HOSTS = ["localhost", "127.0.0.1", "::1", "[::1]"]

# Reachable without an account.
PUBLIC_ENDPOINTS = {
    "main.dashboard",  # shows the public landing page when nobody is signed in
    "auth.login", "auth.signup", "auth.logout", "auth.forgot_password", "auth.reset_password",
    "legal.terms", "legal.privacy", "legal.refunds",
    "static", "healthz",
}


# ------------------------------------------------------------------ helpers

def trusted_hosts_from_env(production):
    """Host allow-list. Local installs only answer to localhost (blocks DNS rebinding);
    a hosted install accepts its public name. CONTENTQUEUE_ALLOWED_HOSTS overrides both."""
    extra = [h.strip() for h in os.environ.get("CONTENTQUEUE_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if extra:
        return DEFAULT_TRUSTED_HOSTS + extra
    return None if production else list(DEFAULT_TRUSTED_HOSTS)


def hash_password(password):
    # PASSWORD_HASH_METHOD only exists so the test suite can use a faster hash; production uses scrypt.
    method = current_app.config.get("PASSWORD_HASH_METHOD") or "scrypt"
    return generate_password_hash(password, method=method)


def bind_error(host, debug):
    """run.py: the Flask debugger can run code, so it may only listen on this computer."""
    if debug and host not in LOOPBACK_HOSTS:
        return "--debug is only allowed on 127.0.0.1 (the debugger can run code)."
    return None


def _recent_failures(key):
    cutoff = time.monotonic() - LOGIN_WINDOW_SECONDS
    attempts = [t for t in _failed_logins.get(key, []) if t > cutoff]
    _failed_logins[key] = attempts
    return attempts


def login_blocked(ip, email):
    return (len(_recent_failures(("email", email))) >= MAX_FAILURES_PER_EMAIL
            or len(_recent_failures(("ip", ip))) >= MAX_FAILURES_PER_IP)


def record_failed_login(ip, email):
    now = time.monotonic()
    for key in (("email", email), ("ip", ip)):
        _failed_logins.setdefault(key, []).append(now)


def is_safe_local_path(target):
    # Reject "//host" and "/\host": browsers treat both as links to another site.
    return bool(target) and target.startswith("/") and not target.startswith(("//", "/\\")) \
        and "\\" not in target


def safe_next(target):
    """Only follow a local path that stays inside this app (works when mounted under a prefix)."""
    if is_safe_local_path(target):
        root = request.script_root or ""
        if not root or target == root or target.startswith((root + "/", root + "?")):
            return target
    return url_for("main.dashboard")


def start_session(account_id):
    session.clear()  # new session on sign-in (prevents session fixation); a fresh CSRF token follows
    session.permanent = True
    session["account_id"] = account_id


def load_current_account():
    """before_request: g.account is the signed-in business, or None."""
    g.account = None
    account_id = session.get("account_id")
    if account_id is not None:
        g.account = get_db().execute(
            "SELECT id, business_name, email, timezone, is_demo, created_at, paid_until "
            "FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if g.account is None:  # account deleted elsewhere
            session.clear()


def current_account_id():
    if g.get("account") is None:
        abort(401)
    return g.account["id"]


def require_login():
    """before_request: every non-public page needs an account."""
    if g.get("account") is not None or request.endpoint in PUBLIC_ENDPOINTS or request.endpoint is None:
        return None
    if request.method == "GET":
        # script_root keeps the mount prefix (e.g. /contentqueue) so login returns to the right page.
        target = request.script_root + (request.full_path if request.query_string else request.path)
        return redirect(url_for("auth.login", next=target))
    abort(401)


def _validate_timezone(name):
    name = (name or "").strip()
    if not name:
        return ""
    tz, _err = timeutil.resolve_timezone(name)
    return name if tz is not None and len(name) <= 64 else None


# ------------------------------------------------------------------ views

@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if g.account:
        return redirect(url_for("main.dashboard"))
    form, errors = {}, {}
    if request.method == "POST":
        form = {k: request.form.get(k, "").strip() for k in ("business_name", "email", "timezone")}
        form["email"] = form["email"].lower()
        password = request.form.get("password", "")
        if not form["business_name"]:
            errors["business_name"] = "Business name is required."
        elif len(form["business_name"]) > MAX_BUSINESS_NAME:
            errors["business_name"] = "Business name is too long."
        if not EMAIL_RE.match(form["email"]) or len(form["email"]) > 254:
            errors["email"] = "Enter a valid email address."
        if len(password) < MIN_PASSWORD_LENGTH:
            errors["password"] = f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        elif len(password) > 200:
            errors["password"] = "Password is too long."
        # Browser-detected timezone; silently fall back to the server default if unusable.
        tz = _validate_timezone(form["timezone"]) or current_app.config.get("TIMEZONE", "") or ""
        if not errors:
            conn = get_db()
            try:
                cur = conn.execute(
                    "INSERT INTO accounts (business_name, email, password_hash, timezone, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (form["business_name"], form["email"], hash_password(password), tz, now_iso()),
                )
            except sqlite3.IntegrityError:
                errors["email"] = "An account with this email already exists. Try logging in instead."
            else:
                account_id = cur.lastrowid
                # Content from before accounts existed belongs to the first business that signs up.
                if conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1:
                    claimed = assign_unowned_data(conn, account_id)
                else:
                    claimed = {}
                conn.commit()
                start_session(account_id)
                if any(claimed.values()):
                    flash(f"Your existing content ({claimed.get('posts', 0)} posts) is now in this account.", "info")
                flash("Welcome to ContentQueue! Your free trial has started.", "success")
                return redirect(url_for("main.dashboard"))
    return render_template("auth/signup.html", form=form, errors=errors), (400 if errors else 200)


@bp.route("/login", methods=["GET", "POST"])
def login():
    next_url = request.values.get("next", "")
    if g.account:
        return redirect(safe_next(next_url))
    email, error = "", None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        ip = request.remote_addr or "unknown"
        if login_blocked(ip, email):
            error = "Too many failed attempts. Please wait 15 minutes and try again."
            return render_template("auth/login.html", email=email, error=error, next_url=next_url), 429
        row = get_db().execute("SELECT id, password_hash FROM accounts WHERE email = ?", (email,)).fetchone()
        valid = check_password_hash(row["password_hash"] if row else DUMMY_HASH, password)
        if row and valid:
            _failed_logins.pop(("email", email), None)
            start_session(row["id"])
            return redirect(safe_next(next_url))
        record_failed_login(ip, email)
        error = "That email and password don't match. Please try again."
    return render_template("auth/login.html", email=email, error=error, next_url=next_url), (401 if error else 200)


@bp.post("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))


@bp.route("/account", methods=["GET", "POST"])
def account():
    acct = g.account
    form = {"business_name": acct["business_name"], "timezone": acct["timezone"]}
    errors = {}
    if request.method == "POST":
        action = request.form.get("action", "profile")
        conn = get_db()
        if action == "password":
            current = request.form.get("current_password", "")
            new = request.form.get("new_password", "")
            row = conn.execute("SELECT password_hash FROM accounts WHERE id = ?", (acct["id"],)).fetchone()
            if not check_password_hash(row["password_hash"], current):
                errors["current_password"] = "Your current password isn't right."
            if len(new) < MIN_PASSWORD_LENGTH:
                errors["new_password"] = f"New password must be at least {MIN_PASSWORD_LENGTH} characters."
            elif len(new) > 200:
                errors["new_password"] = "New password is too long."
            elif new != request.form.get("confirm_password", new):
                errors["confirm_password"] = "The two new passwords don't match."
            if not errors:
                conn.execute("UPDATE accounts SET password_hash = ? WHERE id = ?",
                             (hash_password(new), acct["id"]))
                conn.commit()
                flash("Password changed.", "success")
                return redirect(url_for("auth.account"))
        else:
            form = {k: request.form.get(k, "").strip() for k in ("business_name", "timezone")}
            if not form["business_name"]:
                errors["business_name"] = "Business name is required."
            elif len(form["business_name"]) > MAX_BUSINESS_NAME:
                errors["business_name"] = "Business name is too long."
            tz = _validate_timezone(form["timezone"])
            if tz is None:
                errors["timezone"] = "Unknown timezone. Use a name like Asia/Bangkok or Europe/London."
            if not errors:
                conn.execute("UPDATE accounts SET business_name = ?, timezone = ? WHERE id = ?",
                             (form["business_name"], tz, acct["id"]))
                conn.commit()
                flash("Account saved.", "success")
                return redirect(url_for("auth.account"))
    return render_template("auth/account.html", form=form, errors=errors), (400 if errors else 200)


@bp.route("/account/delete", methods=["GET", "POST"])
def delete_account():
    acct = g.account
    conn = get_db()
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE account_id = ?", (acct["id"],)).fetchone()[0]
              for t in ("posts", "ideas", "templates")}
    error, status = None, 200
    if request.method == "POST":
        ip = request.remote_addr or "unknown"
        typed = request.form.get("confirm_text", "").strip()
        if typed != DELETE_WORD and typed.lower() != acct["email"].lower():
            error, status = (f"Type {DELETE_WORD} (or your email) in the box to confirm. "
                             "Your account has not been deleted."), 400
        elif login_blocked(ip, acct["email"]):
            error, status = "Too many wrong passwords. Please wait 15 minutes and try again.", 429
        else:
            row = conn.execute("SELECT password_hash FROM accounts WHERE id = ?", (acct["id"],)).fetchone()
            if check_password_hash(row["password_hash"], request.form.get("password", "")):
                images = [r["image_filename"] for r in conn.execute(
                    "SELECT image_filename FROM posts WHERE account_id = ? AND image_filename IS NOT NULL",
                    (acct["id"],))]
                # Explicit deletes as well as ON DELETE CASCADE, in case foreign keys are off.
                for table in ("posts", "ideas", "templates", "campaigns", "categories"):
                    conn.execute(f"DELETE FROM {table} WHERE account_id = ?", (acct["id"],))
                conn.execute("DELETE FROM accounts WHERE id = ?", (acct["id"],))
                conn.commit()
                for name in images:
                    delete_upload(current_app.config["UPLOAD_FOLDER"], name)
                # This account's own reset backups (its folder only) are deleted too.
                from .demo import account_backup_folder
                shutil.rmtree(account_backup_folder(current_app.config["BACKUP_FOLDER"], acct["id"]),
                              ignore_errors=True)
                session.clear()
                flash("Your account and all its content have been deleted.", "info")
                return redirect(url_for("main.dashboard"))
            record_failed_login(ip, acct["email"])
            error, status = "That password isn't right. Your account has not been deleted.", 400
    return render_template("auth/delete_account.html", counts=counts, error=error,
                           delete_word=DELETE_WORD), status


# ------------------------------------------------------------------ export all my data

EXPORT_COLUMNS = {
    "posts": ["id", "title", "caption", "hashtags", "platform", "status", "campaign_id", "category_id",
              "planned_date", "planned_time", "notes", "image_filename", "published_at", "published_url",
              "is_demo", "created_at", "updated_at"],
    "campaigns": ["id", "name", "description", "color", "start_date", "end_date", "is_demo", "created_at"],
    "categories": ["id", "name", "description", "is_demo", "created_at"],
    "templates": ["id", "name", "body", "hashtags", "platform", "is_demo", "created_at", "updated_at"],
    "ideas": ["id", "title", "notes", "category_id", "status", "is_demo", "created_at"],
}


def _csv_bytes(rows, columns):
    from .views.main import csv_safe

    buf = io.StringIO()
    buf.write("\ufeff")  # BOM so Excel opens UTF-8 correctly
    writer = csv.writer(buf)
    writer.writerow(columns)
    for r in rows:
        writer.writerow([csv_safe(r[c]) for c in columns])
    return buf.getvalue().encode("utf-8")


@bp.route("/account/export")
def export_data():
    """Everything this account has stored, as a ZIP of CSV files plus its images (never the password)."""
    conn = get_db()
    account_id = current_account_id()
    account_cols = ["business_name", "email", "timezone", "created_at", "paid_until"]
    files = {"account.csv": _csv_bytes(
        conn.execute(f"SELECT {', '.join(account_cols)} FROM accounts WHERE id = ?", (account_id,)).fetchall(),
        account_cols)}
    for table in SCOPED_TABLES:  # table and column names are code constants
        cols = EXPORT_COLUMNS[table]
        rows = conn.execute(f"SELECT {', '.join(cols)} FROM {table} WHERE account_id = ? ORDER BY id",
                            (account_id,)).fetchall()
        files[f"{table}.csv"] = _csv_bytes(rows, cols)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
        folder = current_app.config["UPLOAD_FOLDER"]
        for r in conn.execute("SELECT DISTINCT image_filename FROM posts WHERE account_id = ? "
                              "AND image_filename IS NOT NULL", (account_id,)):
            name = r["image_filename"]
            path = os.path.join(folder, name)
            if is_stored_name(name) and os.path.isfile(path):
                zf.write(path, f"images/{name}")
    filename = f"contentqueue-all-data-{timeutil.today():%Y%m%d}.zip"
    return Response(buf.getvalue(), mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ------------------------------------------------------------------ forgot password (optional email)

def mail_on():
    return mailer.mail_enabled(current_app.config.get("MAIL"))


def _reset_serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="pw-reset")


def _hash_tag(password_hash):
    # Short fingerprint of the current password hash: the link stops working once the password changes.
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]


def _reset_allowed(ip, email):
    cutoff = time.monotonic() - LOGIN_WINDOW_SECONDS
    keys = (("email", email), ("ip", ip))
    for key in keys:
        _reset_requests[key] = [t for t in _reset_requests.get(key, []) if t > cutoff]
    if len(_reset_requests[keys[0]]) >= MAX_RESETS_PER_EMAIL or len(_reset_requests[keys[1]]) >= MAX_RESETS_PER_IP:
        return False
    now = time.monotonic()
    for key in keys:
        _reset_requests[key].append(now)
    return True


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if not mail_on():
        abort(404)
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        ip = request.remote_addr or "unknown"
        if EMAIL_RE.match(email) and _reset_allowed(ip, email):
            row = get_db().execute("SELECT email, password_hash, is_demo FROM accounts WHERE email = ?",
                                   (email,)).fetchone()
            if row and not row["is_demo"]:
                token = _reset_serializer().dumps({"e": row["email"], "h": _hash_tag(row["password_hash"])})
                link = url_for("auth.reset_password", token=token, _external=True)
                mailer.send_mail(current_app.config["MAIL"], row["email"], "Reset your ContentQueue password",
                                 "Someone asked to reset the password for your ContentQueue account.\n\n"
                                 f"To choose a new password, open this link (it works for 1 hour):\n{link}\n\n"
                                 "If this wasn't you, you can ignore this email. Your password has not changed.")
        # Always the same answer, so nobody can find out which emails have an account.
        flash(RESET_SENT_MESSAGE, "info")
        return redirect(url_for("auth.login"))
    return render_template("auth/forgot_password.html")


@bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    if not mail_on():
        abort(404)
    try:
        data = _reset_serializer().loads(token, max_age=RESET_MAX_AGE_SECONDS)
    except (SignatureExpired, BadSignature):
        data = None
    conn = get_db()
    row = None
    if isinstance(data, dict):
        row = conn.execute("SELECT id, password_hash FROM accounts WHERE email = ?",
                           (str(data.get("e", "")),)).fetchone()
    if row is None or data.get("h") != _hash_tag(row["password_hash"]):
        flash("This reset link is not valid any more. Please ask for a new one.", "error")
        return redirect(url_for("auth.forgot_password"))
    errors = {}
    if request.method == "POST":
        new = request.form.get("new_password", "")
        if len(new) < MIN_PASSWORD_LENGTH:
            errors["new_password"] = f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        elif len(new) > 200:
            errors["new_password"] = "Password is too long."
        elif new != request.form.get("confirm_password", ""):
            errors["confirm_password"] = "The two passwords don't match."
        if not errors:
            conn.execute("UPDATE accounts SET password_hash = ? WHERE id = ?", (hash_password(new), row["id"]))
            conn.commit()
            _failed_logins.pop(("email", data["e"]), None)
            session.clear()
            flash("Your password has been changed. Please log in.", "success")
            return redirect(url_for("auth.login"))
    return render_template("auth/reset_password.html", token=token, errors=errors), (400 if errors else 200)
