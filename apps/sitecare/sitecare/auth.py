"""Accounts: sign-up, log in, log out, account settings and account deletion.

Each developer or studio has its own account and only ever sees its own data. Clients of a
studio never get accounts: they only see a report the studio shares through a private link.
"""
import csv
import hashlib
import io
import re
import secrets
import sqlite3
import time
import zipfile
from datetime import date

from flask import (
    Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, session, url_for,
)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

from . import mailer
from .db import ACCOUNT_TABLES, get_db, wipe_account

bp = Blueprint("auth", __name__)

# Pages anyone can open without logging in.
PUBLIC_ENDPOINTS = {
    "auth.login", "auth.signup", "auth.forgot_password", "auth.reset_password", "static", "healthz",
    "main.shared_report", "main.dashboard", "legal.terms", "legal.privacy", "legal.refunds",
}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 10
MAX_PASSWORD = 128
# Checked against when the email is unknown, so response time doesn't reveal which emails exist.
DUMMY_HASH = generate_password_hash("not-a-real-password")

# Brute-force protection (per app process), like the other sellingteen apps.
LOGIN_WINDOW_SECONDS = 15 * 60
MAX_FAILURES_PER_EMAIL = 5
MAX_FAILURES_PER_IP = 30


# ---------------------------------------------------------------- throttling

def _failures():
    return current_app.extensions.setdefault("sitecare_login_failures", {})


def _recent_failures(key):
    cutoff = time.monotonic() - LOGIN_WINDOW_SECONDS
    attempts = [t for t in _failures().get(key, []) if t > cutoff]
    _failures()[key] = attempts
    return attempts


def login_blocked(ip, email):
    return (len(_recent_failures(("email", email))) >= MAX_FAILURES_PER_EMAIL
            or len(_recent_failures(("ip", ip))) >= MAX_FAILURES_PER_IP)


def record_failed_login(ip, email):
    now = time.monotonic()
    for key in (("email", email), ("ip", ip)):
        _failures().setdefault(key, []).append(now)


# ---------------------------------------------------------------- helpers

def password_problem(password, confirm=None):
    if len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    if len(password) > MAX_PASSWORD:
        return f"Use at most {MAX_PASSWORD} characters."
    if confirm is not None and password != confirm:
        return "The two passwords don't match."
    return None


def set_password(db, account_id, password):
    """Change a password and sign out every existing session of that account."""
    problem = password_problem(password)
    if problem:
        raise ValueError(problem)
    version = secrets.token_hex(8)
    db.execute("UPDATE accounts SET password_hash = ?, session_version = ? WHERE id = ?",
               (generate_password_hash(password), version, account_id))
    db.commit()
    return version


def create_account(db, name, email, password):
    """Insert an account; raises sqlite3.IntegrityError if the email is taken."""
    cur = db.execute(
        "INSERT INTO accounts (name, email, password_hash, session_version) VALUES (?, ?, ?, ?)",
        (name, email.strip().lower(), generate_password_hash(password), secrets.token_hex(8)),
    )
    db.commit()
    return cur.lastrowid


def is_safe_local_path(target):
    """Only paths inside this app (respecting a mount prefix such as /sitecare) are allowed."""
    if not target or not target.startswith("/") or target.startswith(("//", "/\\")) or "\\" in target:
        return False
    root = request.script_root
    return not root or target == root or target.startswith(root + "/")


def _start_session(account):
    session.clear()  # prevents session fixation
    session.permanent = True
    session["account_id"] = account["id"]
    session["session_version"] = account["session_version"]


def load_current_account():
    """before_request: g.account is the signed-in account, or None."""
    g.account = None
    account_id = session.get("account_id")
    if account_id is None:
        return
    row = get_db().execute(
        "SELECT id, name, email, session_version, is_demo, paid_until, created_at FROM accounts WHERE id = ?",
        (account_id,),
    ).fetchone()
    if row is None or row["session_version"] != session.get("session_version"):
        session.pop("account_id", None)  # deleted account, or password changed elsewhere
        session.pop("session_version", None)
        return
    g.account = row


def require_login():
    """before_request: every page except PUBLIC_ENDPOINTS needs a signed-in account."""
    if request.endpoint is None or request.endpoint in PUBLIC_ENDPOINTS or g.get("account") is not None:
        return None
    # script_root keeps the mount prefix (e.g. /sitecare) so login returns to the right page.
    target = request.script_root + request.full_path
    if not request.query_string:
        target = target.rstrip("?")
    return redirect(url_for("auth.login", next=target if request.method == "GET" else None))


# ---------------------------------------------------------------- sign-up, log in, log out

@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if g.account:
        return redirect(url_for("main.dashboard"))
    form, errors = {}, {}
    if request.method == "POST":
        form = {k: request.form.get(k, "").strip() for k in ("name", "email")}
        form["email"] = form["email"].lower()
        password = request.form.get("password", "")
        if not form["name"]:
            errors["name"] = "Your name or studio name is required."
        elif len(form["name"]) > 100:
            errors["name"] = "That name is too long (100 characters at most)."
        if not EMAIL_RE.match(form["email"]) or len(form["email"]) > 254:
            errors["email"] = "Enter a valid email address."
        # The confirmation box is on the form; scripts that only send one password still work.
        if problem := password_problem(password, request.form.get("confirm")):
            errors["password"] = problem
        if not errors:
            db = get_db()
            try:
                account_id = create_account(db, form["name"], form["email"], password)
            except sqlite3.IntegrityError:
                errors["email"] = "An account with this email already exists. Try logging in instead."
            else:
                _start_session(db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone())
                flash("Welcome to SiteCare Desk! Start by adding your first client.", "success")
                return redirect(url_for("main.dashboard"))
    return render_template("auth/signup.html", form=form, errors=errors), (400 if errors else 200)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.account:
        return redirect(url_for("main.dashboard"))
    email, error, status = "", None, 200
    next_target = request.values.get("next", "")
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()[:254]
        password = request.form.get("password", "")[:MAX_PASSWORD]
        ip = request.remote_addr or "unknown"
        if login_blocked(ip, email):
            error, status = "Too many failed attempts. Please wait 15 minutes and try again.", 429
        else:
            row = get_db().execute("SELECT * FROM accounts WHERE email = ?", (email,)).fetchone()
            # An unusable hash (e.g. "!disabled" from a migrated database) simply never matches.
            valid = check_password_hash(row["password_hash"] if row else DUMMY_HASH, password)
            if row and valid:
                _failures().pop(("email", email), None)
                _start_session(row)
                return redirect(next_target if is_safe_local_path(next_target) else url_for("main.dashboard"))
            record_failed_login(ip, email)
            error, status = "That email and password don't match. Please try again.", 401
    return render_template("auth/login.html", email=email, error=error, next=next_target,
                           mail_on=_mail_on()), status


@bp.post("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))


# ---------------------------------------------------------------- account settings

def _check_current_password(field="current_password"):
    """True if the form's current password is right (failures count towards the lockout)."""
    ip = request.remote_addr or "unknown"
    if login_blocked(ip, g.account["email"]):
        return False, "Too many wrong passwords. Please wait 15 minutes and try again."
    row = get_db().execute("SELECT password_hash FROM accounts WHERE id = ?", (g.account["id"],)).fetchone()
    if check_password_hash(row["password_hash"], request.form.get(field, "")[:MAX_PASSWORD]):
        return True, None
    record_failed_login(ip, g.account["email"])
    return False, "That password isn't right."


@bp.route("/account", methods=["GET", "POST"])
def account():
    db = get_db()
    form = {"name": g.account["name"], "email": g.account["email"]}
    errors, status = {}, 200
    if request.method == "POST":
        form = {"name": request.form.get("name", "").strip(), "email": request.form.get("email", "").strip().lower()}
        if not form["name"] or len(form["name"]) > 100:
            errors["name"] = "Enter a name of up to 100 characters."
        if not EMAIL_RE.match(form["email"]) or len(form["email"]) > 254:
            errors["email"] = "Enter a valid email address."
        email_changed = form["email"] != g.account["email"]
        if not errors and email_changed:
            ok, message = _check_current_password()
            if not ok:
                errors["current_password"] = message
        if not errors:
            try:
                db.execute("UPDATE accounts SET name = ?, email = ? WHERE id = ?",
                           (form["name"], form["email"], g.account["id"]))
                db.commit()
            except sqlite3.IntegrityError:
                errors["email"] = "Another account already uses this email."
            else:
                flash("Account details saved.", "success")
                return redirect(url_for("auth.account"))
        status = 400
    return render_template("auth/account.html", form=form, errors=errors, password_errors={}), status


@bp.post("/account/password")
def change_password():
    errors = {}
    ok, message = _check_current_password()
    if not ok:
        errors["current_password"] = message
    elif problem := password_problem(request.form.get("new_password", ""), request.form.get("confirm_password", "")):
        errors["new_password"] = problem
    if errors:
        form = {"name": g.account["name"], "email": g.account["email"]}
        return render_template("auth/account.html", form=form, errors={}, password_errors=errors), 400
    version = set_password(get_db(), g.account["id"], request.form["new_password"])
    session["session_version"] = version  # stay signed in here; other devices are signed out
    flash("Password changed. You were signed out everywhere else.", "success")
    return redirect(url_for("auth.account"))


@bp.route("/account/delete", methods=["GET", "POST"])
def delete_account():
    db = get_db()
    counts = {
        "clients": db.execute("SELECT COUNT(*) FROM clients WHERE account_id = ?", (g.account["id"],)).fetchone()[0],
        "websites": db.execute("SELECT COUNT(*) FROM websites WHERE account_id = ?", (g.account["id"],)).fetchone()[0],
        "reports": db.execute("SELECT COUNT(*) FROM saved_reports WHERE account_id = ?",
                              (g.account["id"],)).fetchone()[0],
    }
    error, status = None, 200
    if request.method == "POST":
        ok, error = _check_current_password("password")
        typed = request.form.get("confirm", "").strip()
        if ok and typed != "DELETE" and typed.lower() != g.account["email"]:
            ok, error = False, "Type DELETE (or your email) to confirm."
        if ok:
            wipe_account(db, g.account["id"], current_app.config["UPLOAD_FOLDER"])
            db.execute("DELETE FROM accounts WHERE id = ?", (g.account["id"],))
            db.commit()
            session.clear()
            flash("Your account and all its data have been deleted. Shared report links no longer work.", "info")
            return redirect(url_for("main.dashboard"))
        status = 429 if error and "Too many" in error else 400
    return render_template("auth/delete_account.html", counts=counts, error=error), status


# ---------------------------------------------------------------- export all my data

# Columns never exported: password hashes and session versions stay private.
EXPORT_ACCOUNT_COLUMNS = ("id", "name", "email", "paid_until", "created_at")


def _safe_cell(value):
    # Stop spreadsheet formula injection when the CSV is opened in Excel.
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _csv_bytes(cursor):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([c[0] for c in cursor.description])
    for row in cursor:
        writer.writerow([_safe_cell(v) for v in row])
    return ("﻿" + out.getvalue()).encode("utf-8")


@bp.get("/account/export")
def export_data():
    """A ZIP of CSV files with every record of the signed-in account (and nothing else)."""
    db = get_db()
    account_id = g.account["id"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("account.csv", _csv_bytes(db.execute(
            f"SELECT {', '.join(EXPORT_ACCOUNT_COLUMNS)} FROM accounts WHERE id = ?", (account_id,))))
        for table in reversed(ACCOUNT_TABLES):  # constant table names
            zf.writestr(f"{table}.csv", _csv_bytes(db.execute(
                f"SELECT * FROM {table} WHERE account_id = ? ORDER BY id", (account_id,))))
    filename = f"sitecare-export-{date.today().isoformat()}.zip"
    return Response(buf.getvalue(), mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---------------------------------------------------------------- optional "forgot password" by email

RESET_MAX_AGE_SECONDS = 60 * 60
RESET_WINDOW_SECONDS = 60 * 60
MAX_RESETS_PER_EMAIL = 3
MAX_RESETS_PER_IP = 10
RESET_SENT_MESSAGE = "If an account exists, we sent a link to reset your password. It works for 1 hour."


def _mail_on():
    return mailer.mail_enabled(current_app.config.get("MAIL"))


def _reset_serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="pw-reset")


def _hash_tag(password_hash):
    """Short fingerprint of the current password hash: the link stops working once the password changes."""
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]


def _reset_allowed(ip, email):
    store = current_app.extensions.setdefault("sitecare_reset_requests", {})
    now = time.monotonic()
    keys = (("email", email), ("ip", ip))
    for key in keys:
        store[key] = [t for t in store.get(key, []) if t > now - RESET_WINDOW_SECONDS]
    if len(store[keys[0]]) >= MAX_RESETS_PER_EMAIL or len(store[keys[1]]) >= MAX_RESETS_PER_IP:
        return False
    for key in keys:
        store[key].append(now)
    return True


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if not _mail_on():
        abort(404)
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()[:254]
        ip = request.remote_addr or "unknown"
        if EMAIL_RE.match(email) and _reset_allowed(ip, email):
            row = get_db().execute("SELECT email, password_hash FROM accounts WHERE email = ? AND is_demo = 0",
                                   (email,)).fetchone()
            if row is not None:
                token = _reset_serializer().dumps({"e": row["email"], "h": _hash_tag(row["password_hash"])})
                link = url_for("auth.reset_password", token=token, _external=True)
                mailer.send_mail(current_app.config["MAIL"], row["email"], "Reset your SiteCare Desk password",
                                 "Someone asked to reset the password for your SiteCare Desk account.\n\n"
                                 f"To choose a new password, open this link within 1 hour:\n{link}\n\n"
                                 "If you did not ask for this, ignore this email. Your password stays the same.")
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
        row = db.execute("SELECT id, password_hash FROM accounts WHERE email = ?", (str(data.get("e", "")),)).fetchone()
    if row is None or _hash_tag(row["password_hash"]) != data.get("h"):
        flash("This reset link is not valid any more. Please ask for a new one.", "error")
        return redirect(url_for("auth.forgot_password"))
    errors = {}
    if request.method == "POST":
        problem = password_problem(request.form.get("new_password", ""), request.form.get("confirm_password", ""))
        if problem:
            errors["new_password"] = problem
        else:
            set_password(db, row["id"], request.form["new_password"])  # also signs out every session
            session.clear()
            flash("Your password was changed. Please log in with the new password.", "success")
            return redirect(url_for("auth.login"))
    return render_template("auth/reset_password.html", token=token, errors=errors), (400 if errors else 200)
