"""Business accounts: sign up, log in, log out, account settings.

Each business that signs up gets its own account and only ever sees its own data: every owner
query filters on g.account["id"]. Clients never log in; they use the private portal link their
business sends them (/p/<token>).
"""
import hashlib
import re
import sqlite3
import time
from functools import wraps

import click
from flask import (Blueprint, abort, current_app, flash, g, redirect, render_template, request, session,
                   url_for)
from flask.cli import with_appcontext
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

from . import mailer
from .db import get_db, now_iso
from .security import get_csrf_token

bp = Blueprint("auth", __name__)

MIN_PASSWORD_LENGTH = 10
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Checked against when the email is unknown, so response time doesn't reveal which emails exist.
DUMMY_HASH = generate_password_hash("not-a-real-password")

# In-memory brute-force protection (per app process), as in the other sellingteen apps.
FAILURE_WINDOW = 15 * 60
MAX_FAILURES = 5            # per email address within the window
MAX_FAILURES_PER_IP = 30    # per IP address within the window (many emails from one address)


# ---------------------------------------------------------------- password storage

def validate_password(password):
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")


def set_account_password(account_id, password):
    validate_password(password)
    db = get_db()
    db.execute("UPDATE accounts SET password_hash = ? WHERE id = ?", (generate_password_hash(password), account_id))
    db.commit()


def _fingerprint(pw_hash: str) -> str:
    """Stored in the session; changes when the password changes, which signs out old sessions."""
    return hashlib.sha256(pw_hash.encode()).hexdigest()[:24]


def start_session(account):
    session.clear()  # new session on login (prevents session fixation)
    session.permanent = True
    session["account_id"] = account["id"]
    session["pw"] = _fingerprint(account["password_hash"])
    get_csrf_token()


def load_current_account():
    """before_request: g.account is the signed-in business, or None."""
    g.account = None
    account_id = session.get("account_id")
    if account_id is None:
        return
    row = get_db().execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None or session.get("pw") != _fingerprint(row["password_hash"]):
        session.pop("account_id", None)
        session.pop("pw", None)
        return
    g.account = row


def is_owner() -> bool:
    return g.get("account") is not None


# ---------------------------------------------------------------- login throttling

def _failures():
    return current_app.extensions.setdefault("clientstart_login_failures", {})


def _recent(key):
    cutoff = time.monotonic() - FAILURE_WINDOW
    recent = [t for t in _failures().get(key, []) if t > cutoff]
    _failures()[key] = recent
    return recent


def login_blocked(ip, email):
    return len(_recent(("email", email))) >= MAX_FAILURES or len(_recent(("ip", ip))) >= MAX_FAILURES_PER_IP


def record_failed_login(ip, email):
    now = time.monotonic()
    for key in (("email", email), ("ip", ip)):
        _failures().setdefault(key, []).append(now)


# ---------------------------------------------------------------- redirects & guards

def safe_next(target):
    """Only local paths inside this app (they include request.script_root, e.g. /clientstart)."""
    root = request.script_root or ""
    if (target and target.startswith("/") and not target.startswith(("//", "/\\")) and "\\" not in target
            and (not root or target == root or target.startswith(root + "/"))):
        return target
    return url_for("owner.dashboard")


def login_url():
    # script_root keeps the mount prefix (e.g. /clientstart) so login returns to the right page.
    return url_for("auth.login", next=request.script_root + request.full_path.rstrip("?"))


def require_owner():
    """before_request for the owner blueprint: every owner page needs a signed-in account."""
    if is_owner():
        return None
    if request.method == "GET":
        return redirect(login_url())
    abort(401)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_owner():
            return redirect(login_url())
        return view(*args, **kwargs)
    return wrapped


# ---------------------------------------------------------------- views

def _clean_name(value):
    return (value or "").strip()[:100]


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if is_owner():
        return redirect(url_for("owner.dashboard"))
    form, errors = {}, {}
    if request.method == "POST":
        form = {"name": _clean_name(request.form.get("name")),
                "email": (request.form.get("email") or "").strip().lower()}
        password = request.form.get("password", "")
        if not form["name"]:
            errors["name"] = "Business name is required."
        if not EMAIL_RE.match(form["email"]) or len(form["email"]) > 254:
            errors["email"] = "Enter a valid email address."
        try:
            validate_password(password)
        except ValueError as e:
            errors["password"] = str(e)
        if not errors:
            db = get_db()
            try:
                account_id = db.execute(
                    "INSERT INTO accounts (name, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
                    (form["name"], form["email"], generate_password_hash(password), now_iso())).lastrowid
            except sqlite3.IntegrityError:
                errors["email"] = "An account with this email already exists. Try logging in instead."
            else:
                from .services import seed_reminder_templates
                seed_reminder_templates(account_id)
                db.commit()
                start_session(db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone())
                flash(f"Welcome to ClientStart, {form['name']}! Create your first onboarding to get started.", "success")
                return redirect(url_for("owner.dashboard"))
    return render_template("auth/signup.html", form=form, errors=errors,
                           min_length=MIN_PASSWORD_LENGTH), (400 if errors else 200)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if is_owner():
        return redirect(safe_next(request.args.get("next")))
    email = ""
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password", "")
        ip = request.remote_addr or "unknown"
        if login_blocked(ip, email):
            flash("Too many failed attempts. Wait 15 minutes and try again.", "error")
            return render_template("login.html", email=email), 429
        row = get_db().execute("SELECT * FROM accounts WHERE email = ?", (email,)).fetchone()
        valid = check_password_hash(row["password_hash"] if row else DUMMY_HASH, password)
        if row and valid:
            _failures().pop(("email", email), None)
            start_session(row)
            return redirect(safe_next(request.form.get("next") or request.args.get("next")))
        record_failed_login(ip, email)
        flash("That email and password don't match. Please try again.", "error")
        return render_template("login.html", email=email), 401
    return render_template("login.html", email=email)


@bp.post("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("auth.login"))


@bp.get("/setup")
def setup():
    """Old single-owner first-run page; accounts are created by signing up now."""
    return redirect(url_for("auth.signup"))


@bp.route("/account", methods=["GET", "POST"])
@login_required
def account():
    """Business name (shown to your clients) and login email."""
    form, errors = {"name": g.account["name"], "email": g.account["email"]}, {}
    if request.method == "POST":
        form = {"name": _clean_name(request.form.get("name")),
                "email": (request.form.get("email") or "").strip().lower()}
        if not form["name"]:
            errors["name"] = "Business name is required."
        if not EMAIL_RE.match(form["email"]) or len(form["email"]) > 254:
            errors["email"] = "Enter a valid email address."
        if not errors:
            db = get_db()
            try:
                db.execute("UPDATE accounts SET name = ?, email = ? WHERE id = ?",
                           (form["name"], form["email"], g.account["id"]))
                db.commit()
            except sqlite3.IntegrityError:
                errors["email"] = "Another account already uses this email."
            else:
                flash("Account saved.", "success")
                return redirect(url_for("auth.account"))
    return render_template("auth/account.html", form=form, errors=errors), (400 if errors else 200)


@bp.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        if not check_password_hash(g.account["password_hash"], request.form.get("current", "")):
            flash("Current password is incorrect.", "error")
        elif request.form.get("password") != request.form.get("confirm"):
            flash("The new passwords don't match.", "error")
        else:
            try:
                set_account_password(g.account["id"], request.form.get("password", ""))
            except ValueError as e:
                flash(str(e), "error")
            else:
                row = get_db().execute("SELECT * FROM accounts WHERE id = ?", (g.account["id"],)).fetchone()
                session["pw"] = _fingerprint(row["password_hash"])  # keep this session; others end
                flash("Password changed. Other signed-in browsers have been signed out.", "success")
                return redirect(url_for("auth.account"))
    return render_template("change_password.html", min_length=MIN_PASSWORD_LENGTH)


@bp.route("/account/delete", methods=["GET", "POST"])
@login_required
def delete_account():
    """Delete this account, all its records and all its uploaded files, then log out."""
    account = g.account
    db = get_db()
    counts = {t: db.execute(f"SELECT COUNT(*) FROM {t} WHERE account_id = ?", (account["id"],)).fetchone()[0]
              for t in ("clients", "tasks", "uploads", "onboarding_templates")}
    error, status = None, 200
    if request.method == "POST":
        typed = (request.form.get("confirm") or "").strip()
        ip = request.remote_addr or "unknown"
        if typed != "DELETE" and typed.lower() != account["email"].lower():
            error, status = "Type DELETE (or your email) to confirm. Your account has not been deleted.", 400
        elif login_blocked(ip, account["email"]):
            error, status = "Too many wrong passwords. Please wait 15 minutes and try again.", 429
        elif not check_password_hash(account["password_hash"], request.form.get("password", "")):
            record_failed_login(ip, account["email"])
            error, status = "That password is not right. Your account has not been deleted.", 400
        else:
            from .services import ACCOUNT_TABLES, _remove_files
            files = db.execute("SELECT stored_name FROM uploads WHERE account_id = ?", (account["id"],)).fetchall()
            # Explicit deletes as well as ON DELETE CASCADE, in case foreign keys are off on an old database.
            for table in ACCOUNT_TABLES:
                db.execute(f"DELETE FROM {table} WHERE account_id = ?", (account["id"],))
            db.execute("DELETE FROM accounts WHERE id = ?", (account["id"],))
            db.commit()
            _remove_files(files)
            session.clear()
            flash("Your account and all its data have been deleted.", "success")
            return redirect(url_for("public.index"))
    return render_template("auth/delete_account.html", counts=counts, error=error), status


# ---------------------------------------------------------------- optional "forgot password" email

RESET_SALT = "pw-reset"
RESET_MAX_AGE = 60 * 60          # links work for 1 hour
RESET_WINDOW = 60 * 60
RESET_MAX_PER_EMAIL = 3          # reset emails per address per hour
RESET_MAX_PER_IP = 10            # reset requests per IP address per hour
RESET_SENT_MESSAGE = "If an account exists, we sent a link to reset the password. It works for 1 hour."


def email_enabled():
    return mailer.mail_enabled(current_app.config.get("MAIL"))


def _reset_serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt=RESET_SALT)


def _pw_tag(pw_hash):
    # Short hash of the current password hash: the link stops working once the password changes.
    return hashlib.sha256(pw_hash.encode()).hexdigest()[:16]


def make_reset_token(account):
    return _reset_serializer().dumps({"email": account["email"], "pw": _pw_tag(account["password_hash"])})


def _account_from_reset_token(token):
    try:
        data = _reset_serializer().loads(token, max_age=RESET_MAX_AGE)
    except (SignatureExpired, BadSignature):
        return None
    if not isinstance(data, dict):
        return None
    row = get_db().execute("SELECT * FROM accounts WHERE email = ?", (str(data.get("email", "")).lower(),)).fetchone()
    if row is None or data.get("pw") != _pw_tag(row["password_hash"]):
        return None
    return row


def _reset_allowed(ip, email):
    """Simple throttle: a few reset emails per address and per IP address each hour."""
    store = current_app.extensions.setdefault("clientstart_reset_requests", {})
    cutoff = time.monotonic() - RESET_WINDOW
    keys = (("ip", ip), ("email", email))
    for key in keys:
        store[key] = [t for t in store.get(key, []) if t > cutoff]
    if len(store[keys[0]]) >= RESET_MAX_PER_IP or len(store[keys[1]]) >= RESET_MAX_PER_EMAIL:
        return False
    now = time.monotonic()
    for key in keys:
        store[key].append(now)
    return True


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if not email_enabled():
        abort(404)
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        ip = request.remote_addr or "unknown"
        if EMAIL_RE.match(email) and _reset_allowed(ip, email):
            row = get_db().execute("SELECT * FROM accounts WHERE email = ?", (email,)).fetchone()
            if row is not None:
                link = url_for("auth.reset_password", token=make_reset_token(row), _external=True)
                mailer.send_mail(
                    current_app.config["MAIL"], row["email"], "Reset your ClientStart password",
                    "Hi,\n\nSomeone asked to reset the password for your ClientStart account.\n"
                    f"To choose a new password, open this link (it works for 1 hour):\n\n{link}\n\n"
                    "If you did not ask for this, you can ignore this email. Your password stays the same.\n")
        # Always the same answer, so this page does not show which emails have accounts.
        flash(RESET_SENT_MESSAGE, "success")
        return redirect(url_for("auth.login"))
    return render_template("auth/forgot_password.html")


@bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    if not email_enabled():
        abort(404)
    row = _account_from_reset_token(token)
    if row is None:
        flash("This reset link does not work any more. Please ask for a new one.", "error")
        return redirect(url_for("auth.forgot_password"))
    if request.method == "POST":
        if request.form.get("password") != request.form.get("confirm"):
            flash("The new passwords don't match.", "error")
        else:
            try:
                set_account_password(row["id"], request.form.get("password", ""))
            except ValueError as e:
                flash(str(e), "error")
            else:
                session.clear()
                _failures().pop(("email", row["email"]), None)
                flash("Your password has been changed. You can log in now.", "success")
                return redirect(url_for("auth.login"))
    return render_template("auth/reset_password.html", token=token, min_length=MIN_PASSWORD_LENGTH)


@click.command("set-password")
@click.argument("email")
@click.password_option("--password", prompt="New password", confirmation_prompt=True)
@with_appcontext
def set_password_command(email, password):
    """Set or reset an account's password (for support, or if the owner is locked out)."""
    row = get_db().execute("SELECT id FROM accounts WHERE email = ?", (email.strip().lower(),)).fetchone()
    if row is None:
        raise click.ClickException(f"No account with the email {email}.")
    try:
        set_account_password(row["id"], password)
    except ValueError as e:
        raise click.ClickException(str(e))
    click.echo(f"Password set for {email}. Existing sessions are signed out.")
