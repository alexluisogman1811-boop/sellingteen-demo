"""Seller accounts: sign-up, login, logout, password reset and account deletion.

Each seller (account) only ever sees its own records. The signed-in account is
loaded into g.account / g.account_id before every request (load_account) and
every data query filters by it (services.acct()).
"""
import hashlib
import sqlite3
import time

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

from . import mailer, security
from .db import DATA_TABLES, get_db, now_iso, seed_templates

bp = Blueprint("auth", __name__)

# Checked when the email is unknown, so the response time doesn't reveal which emails exist.
DUMMY_HASH = generate_password_hash("not-a-real-password")
BUSINESS_NAME_MAX = 120
RESET_MAX_AGE = 3600  # password reset links work for 1 hour
RESET_WINDOW_SECONDS = 15 * 60
RESET_MAX_PER_EMAIL = 3
RESET_MAX_PER_IP = 10
RESET_SENT_MESSAGE = "If an account exists, we sent a link. Check your email (it can take a few minutes)."


def safe_next(target):
    """Only follow local paths (they include the URL prefix, e.g. /licenseledger/...)."""
    if (target and target.startswith("/") and not target.startswith(("//", "/\\")) and "\\" not in target
            and not any(ord(ch) < 32 for ch in target)):
        return target
    return url_for("main.dashboard")


def validate_new_password(password, confirm=None):
    errors = []
    if len(password or "") < security.MIN_PASSWORD_LEN:
        errors.append(f"Password must be at least {security.MIN_PASSWORD_LEN} characters.")
    if confirm is not None and password != confirm:
        errors.append("Passwords do not match.")
    return errors


def validate_business_name(name, errors):
    """Business name: required, no control characters, license keys or card numbers."""
    from .services import clean_text  # local import: services imports nothing from auth

    return clean_text(name, "Business name", errors, required=True, max_len=BUSINESS_NAME_MAX)


def validate_account_email(email, errors):
    from .services import looks_like_card_number, validate_email

    email = validate_email(email, errors)
    if email and looks_like_card_number(email):
        errors.append("Email appears to contain a payment card number. Card data must not be stored.")
    return email


def load_account():
    """Load the signed-in account, or nobody."""
    g.account = None
    g.account_id = None
    account_id = session.get("account_id")
    if account_id is None:
        return
    row = get_db().execute(
        "SELECT id, business_name, email, auth_version, is_demo, paid_until, created_at FROM accounts WHERE id = ?",
        (account_id,),
    ).fetchone()
    # A password change (auth_version) or a deleted account ends every other session.
    if row is None or session.get("auth_version") != row["auth_version"]:
        session.clear()
        return
    g.account = row
    g.account_id = row["id"]


def bump_auth_version(db, account_id):
    """Invalidate every existing session of this account (used when the password changes)."""
    db.execute("UPDATE accounts SET auth_version = auth_version + 1 WHERE id = ?", (account_id,))
    return db.execute("SELECT auth_version FROM accounts WHERE id = ?", (account_id,)).fetchone()[0]


def _start_session(account):
    session.clear()
    session["account_id"] = account["id"]
    session["auth_version"] = account["auth_version"]
    session.permanent = True


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if g.account:
        return redirect(url_for("main.dashboard"))
    form = {}
    if request.method == "POST":
        errors = []
        form = {k: request.form.get(k, "").strip() for k in ("business_name", "email")}
        form["business_name"] = validate_business_name(form["business_name"], errors)
        form["email"] = validate_account_email(form["email"], errors)
        password = request.form.get("password", "")
        errors += validate_new_password(password, request.form.get("confirm", ""))
        if not errors:
            db = get_db()
            try:
                cur = db.execute(
                    "INSERT INTO accounts (business_name, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
                    (form["business_name"], form["email"], generate_password_hash(password), now_iso()),
                )
            except sqlite3.IntegrityError:
                db.rollback()
                errors.append("An account with this email already exists. Try logging in instead.")
            else:
                seed_templates(db, cur.lastrowid)
                db.commit()
                account = db.execute("SELECT id, auth_version FROM accounts WHERE id = ?", (cur.lastrowid,)).fetchone()
                _start_session(account)
                current_app.logger.info("New account created")
                flash("Welcome to LicenseLedger! Add your first product to get started.", "success")
                return redirect(url_for("main.dashboard"))
        for message in errors:
            flash(message, "error")
        return render_template("auth/signup.html", form=form), 400
    return render_template("auth/signup.html", form=form)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.account:
        return redirect(url_for("main.dashboard"))
    email = ""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        ip = request.remote_addr or "local"
        if security.login_locked(ip, email):
            flash("Too many failed attempts. Please wait 15 minutes and try again.", "error")
            return render_template("auth/login.html", email=email, next=request.values.get("next", "")), 429
        row = get_db().execute(
            "SELECT id, password_hash, auth_version FROM accounts WHERE email = ?", (email,)
        ).fetchone()
        if check_password_hash(row["password_hash"] if row else DUMMY_HASH, password) and row:
            security.clear_login_failures(ip, email)
            _start_session(row)
            return redirect(safe_next(request.form.get("next") or request.args.get("next")))
        security.record_login_failure(ip, email)
        current_app.logger.warning("Failed login attempt")
        flash("Incorrect password or email address.", "error")
        return render_template("auth/login.html", email=email, next=request.values.get("next", "")), 401
    return render_template("auth/login.html", email=email, next=request.values.get("next", ""))


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("Signed out.", "success")
    return redirect(url_for("auth.login"))


# --------------------------------------------------------------------------- password reset by email (optional)

def _mail_on():
    return mailer.mail_enabled(current_app.config.get("MAIL"))


def _serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="pw-reset")


def _hash_fingerprint(password_hash):
    """Short hash of the stored password hash: the link stops working once the password changes."""
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]


def make_reset_token(email, password_hash):
    return _serializer().dumps({"e": email, "h": _hash_fingerprint(password_hash)})


def _reset_throttled(ip, email):
    store = current_app.extensions.setdefault("reset_requests", {})
    cutoff = time.monotonic() - RESET_WINDOW_SECONDS
    keys = (("ip", ip), ("email", email))
    for key in keys:
        store[key] = [t for t in store.get(key, []) if t > cutoff]
    if len(store[("ip", ip)]) >= RESET_MAX_PER_IP or len(store[("email", email)]) >= RESET_MAX_PER_EMAIL:
        return True
    for key in keys:
        store[key].append(time.monotonic())
    return False


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if not _mail_on():
        abort(404)
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()[:254]
        if email and not _reset_throttled(request.remote_addr or "local", email):
            row = get_db().execute("SELECT email, password_hash FROM accounts WHERE email = ? AND is_demo = 0",
                                   (email,)).fetchone()
            if row is not None:
                link = url_for("auth.reset_password", token=make_reset_token(row["email"], row["password_hash"]),
                               _external=True)
                mailer.send_mail(
                    current_app.config["MAIL"], row["email"], "Reset your LicenseLedger password",
                    "Someone (hopefully you) asked to reset the password for your LicenseLedger account.\n\n"
                    f"Open this link within 1 hour to choose a new password:\n{link}\n\n"
                    "If you didn't ask for this, you can ignore this email. Your password has not changed.\n",
                )
        # Always the same answer, so this page doesn't reveal which emails have accounts.
        flash(RESET_SENT_MESSAGE, "success")
        return redirect(url_for("auth.login"))
    return render_template("auth/forgot.html")


@bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    if not _mail_on():
        abort(404)
    try:
        data = _serializer().loads(token, max_age=RESET_MAX_AGE)
    except (SignatureExpired, BadSignature):
        data = None
    db = get_db()
    row = None
    if isinstance(data, dict):
        row = db.execute("SELECT id, password_hash FROM accounts WHERE email = ?", (str(data.get("e", "")),)).fetchone()
        if row is not None and data.get("h") != _hash_fingerprint(row["password_hash"]):
            row = None  # already used, or the password was changed since
    if row is None:
        flash("That reset link is invalid or has expired. Please ask for a new one.", "error")
        return redirect(url_for("auth.forgot_password"))
    if request.method == "POST":
        password = request.form.get("password", "")
        errors = validate_new_password(password, request.form.get("confirm", ""))
        if not errors:
            db.execute("UPDATE accounts SET password_hash = ? WHERE id = ?", (generate_password_hash(password), row["id"]))
            bump_auth_version(db, row["id"])  # signs out every other session
            db.commit()
            session.clear()
            flash("Password changed. You can now log in with your new password.", "success")
            return redirect(url_for("auth.login"))
        for message in errors:
            flash(message, "error")
    return render_template("auth/reset.html", token=token)


# --------------------------------------------------------------------------- delete account

@bp.route("/account/delete", methods=["GET", "POST"])
def delete_account():
    """Delete the signed-in account and every record it owns (password + typing DELETE or the email)."""
    from . import demo, services

    db = get_db()
    account = g.account
    counts = services.account_counts(db)
    if request.method == "POST":
        ip = request.remote_addr or "local"
        row = db.execute("SELECT password_hash FROM accounts WHERE id = ?", (account["id"],)).fetchone()
        typed = request.form.get("confirm", "").strip()
        if security.login_locked(ip, account["email"]):
            flash("Too many wrong passwords. Please wait 15 minutes and try again.", "error")
        elif typed != "DELETE" and typed.lower() != account["email"].lower():
            flash("Type DELETE (in capitals) or your email address to confirm. Nothing was deleted.", "error")
        elif not check_password_hash(row["password_hash"], request.form.get("password", "")):
            security.record_login_failure(ip, account["email"])
            flash("That password isn't right. Your account has not been deleted.", "error")
        else:
            # Children first, so foreign keys are satisfied; then the account itself.
            for table in DATA_TABLES + ["reminder_templates"]:
                db.execute(f"DELETE FROM {table} WHERE account_id = ?", (account["id"],))
            db.execute("DELETE FROM accounts WHERE id = ?", (account["id"],))
            db.commit()
            demo.delete_account_backups(current_app.config["BACKUP_DIR"], account["id"])
            current_app.logger.info("Account deleted")
            session.clear()
            flash("Your account and all its data have been deleted.", "success")
            return redirect(url_for("main.dashboard"))
        return render_template("auth/delete_account.html", counts=counts), 400
    return render_template("auth/delete_account.html", counts=counts)
