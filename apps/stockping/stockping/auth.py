"""Sign-up, log in / out, the Account page (change password, export all data, delete account),
the optional "forgot password" email and the set-login admin command."""
import hashlib
import re
import sqlite3
import time
from datetime import date

import click
from flask import (Blueprint, Response, abort, current_app, flash, g, redirect, render_template,
                   request, session, url_for)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

from . import db as dbmod
from . import mailer
from . import services as svc
from .db import LEGACY_EMAIL, AccountDB, get_db, transaction
from .security import login_required, safe_next

bp = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 8
MAX_PASSWORD = 200
# Checked against when the email is unknown, so response time doesn't reveal which emails exist.
DUMMY_HASH = generate_password_hash("not-a-real-password")

# Simple in-memory brute-force protection (per app process).
LOGIN_WINDOW_SECONDS = 15 * 60
MAX_FAILURES_PER_EMAIL = 10
MAX_FAILURES_PER_IP = 30
_failed_logins = {}


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


def email_error(email):
    if not EMAIL_RE.match(email) or len(email) > 254:
        return "Enter a valid email address."
    if email.endswith(".invalid"):
        return "Enter a real email address."
    return None


def password_error(password):
    if len(password) < MIN_PASSWORD:
        return f"Password must be at least {MIN_PASSWORD} characters."
    if len(password) > MAX_PASSWORD:
        return f"Password must be at most {MAX_PASSWORD} characters."
    return None


def shop_name_error(name):
    if not name:
        return "Shop name is required."
    if len(name) > 100:
        return "Shop name must be at most 100 characters."
    return None


def start_session(account_id):
    session.clear()   # also drops any old CSRF token and import preview
    session.permanent = True   # stay logged in on the shop's phone for 30 days
    session["account_id"] = account_id


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if g.account:
        return redirect(url_for("main.dashboard"))
    form, errors = {}, {}
    if request.method == "POST":
        form = {"shop_name": " ".join(request.form.get("shop_name", "").split()),
                "email": request.form.get("email", "").strip().lower()}
        password = request.form.get("password", "")
        for field, error in (("shop_name", shop_name_error(form["shop_name"])),
                             ("email", email_error(form["email"])),
                             ("password", password_error(password))):
            if error:
                errors[field] = error
        if not errors:
            db = get_db()
            try:
                with transaction(db):
                    account_id = db.execute(
                        "INSERT INTO accounts (shop_name, email, password_hash) VALUES (?, ?, ?)",
                        (form["shop_name"], form["email"], generate_password_hash(password))).lastrowid
            except sqlite3.IntegrityError:
                errors["email"] = "An account with this email already exists. Try logging in instead."
            else:
                start_session(account_id)
                flash(f"Welcome to StockPing, {form['shop_name']}! Add your first product "
                      "or import a CSV to get started.", "success")
                return redirect(url_for("main.dashboard"))
    return render_template("auth/signup.html", form=form, errors=errors), (422 if errors else 200)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.account:
        return redirect(url_for("main.dashboard"))
    email, error = "", None
    next_url = request.args.get("next", "")
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        ip = request.remote_addr or "unknown"
        if login_blocked(ip, email):
            error = "Too many failed attempts. Please wait 15 minutes and try again."
            return render_template("auth/login.html", email=email, error=error,
                                   next_url=next_url), 429
        row = get_db().execute("SELECT id, password_hash FROM accounts WHERE email = ?",
                               (email,)).fetchone()
        valid = check_password_hash(row["password_hash"] if row else DUMMY_HASH, password)
        if row and valid:
            _failed_logins.pop(("email", email), None)
            start_session(row["id"])
            return redirect(safe_next(next_url))
        record_failed_login(ip, email)
        error = "That email and password don't match. Please try again."
    return render_template("auth/login.html", email=email, error=error, next_url=next_url), \
        (401 if error else 200)


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))


@bp.route("/account", methods=["GET", "POST"])
@login_required
def account():
    db = get_db()
    form, errors, status = {"shop_name": g.account["shop_name"]}, {}, 200
    if request.method == "POST":
        action = request.form.get("action")
        if action == "profile":
            form["shop_name"] = " ".join(request.form.get("shop_name", "").split())
            if err := shop_name_error(form["shop_name"]):
                errors["shop_name"] = err
            else:
                with transaction(db):
                    db.execute("UPDATE accounts SET shop_name = ? WHERE id = ?",
                               (form["shop_name"], g.account["id"]))
                flash("Shop name saved.", "success")
                return redirect(url_for("auth.account"))
        elif action == "password":
            current = request.form.get("current_password", "")
            new = request.form.get("new_password", "")
            row = db.execute("SELECT password_hash FROM accounts WHERE id = ?",
                             (g.account["id"],)).fetchone()
            if not check_password_hash(row["password_hash"], current):
                errors["current_password"] = "Your current password is not correct."
            elif err := password_error(new):
                errors["new_password"] = err
            else:
                with transaction(db):
                    db.execute("UPDATE accounts SET password_hash = ? WHERE id = ?",
                               (generate_password_hash(new), g.account["id"]))
                flash("Password changed.", "success")
                return redirect(url_for("auth.account"))
        status = 422
    return render_template("auth/account.html", form=form, errors=errors), status


@bp.get("/account/export.zip")
@login_required
def export_data():
    """All of this shop's data as a ZIP of CSV files. Works even after the trial ends."""
    data = svc.export_all_zip(AccountDB(get_db(), g.account["id"]), g.account)
    return Response(data, mimetype="application/zip", headers={
        "Content-Disposition": f'attachment; filename="stockping-all-data-{date.today().isoformat()}.zip"'})


@bp.route("/account/delete", methods=["GET", "POST"])
@login_required
def delete_account():
    db = get_db()
    account_id = g.account["id"]
    counts = {table: db.execute(f"SELECT COUNT(*) FROM {table} WHERE account_id = ?",
                                (account_id,)).fetchone()[0]
              for table in ("products", "stock_movements", "suppliers")}
    error, status = None, 200
    if request.method == "POST":
        email = g.account["email"].lower()
        typed = request.form.get("confirm", "").strip()
        ip = request.remote_addr or "unknown"
        if typed != "DELETE" and typed.lower() != email:
            error, status = "Type DELETE (or your email) to confirm.", 400
        elif login_blocked(ip, email):
            error, status = "Too many wrong passwords. Please wait 15 minutes and try again.", 429
        else:
            row = db.execute("SELECT password_hash FROM accounts WHERE id = ?", (account_id,)).fetchone()
            if not check_password_hash(row["password_hash"], request.form.get("password", "")):
                record_failed_login(ip, email)
                error, status = "That password isn't right. Your account has not been deleted.", 400
            else:
                dbmod.delete_account(db, account_id)
                session.clear()
                flash("Your account and all its data have been deleted.", "info")
                return redirect(url_for("main.index"))
    return render_template("auth/delete_account.html", counts=counts, error=error), status


# --------------------------------------------------------------------------
# Optional "Forgot password?" by email (only when email is set up; see mailer.py)
# --------------------------------------------------------------------------

RESET_SALT = "pw-reset"
RESET_MAX_AGE = 60 * 60   # the emailed link works for 1 hour
RESET_WINDOW_SECONDS = 60 * 60
MAX_RESETS_PER_EMAIL = 3
MAX_RESETS_PER_IP = 10
_reset_requests = {}
RESET_SENT_MESSAGE = "If an account exists, we sent a link to reset your password. It works for 1 hour."


def mail_enabled():
    return mailer.mail_enabled(current_app.config.get("MAIL"))


def _reset_serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt=RESET_SALT)


def _password_fingerprint(password_hash):
    # Changes whenever the password changes, so a used link stops working.
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]


def make_reset_token(email, password_hash):
    return _reset_serializer().dumps({"e": email, "p": _password_fingerprint(password_hash)})


def _reset_throttled(ip, email):
    cutoff = time.monotonic() - RESET_WINDOW_SECONDS
    blocked = False
    for key, limit in ((("ip", ip), MAX_RESETS_PER_IP), (("email", email), MAX_RESETS_PER_EMAIL)):
        recent = [t for t in _reset_requests.get(key, []) if t > cutoff]
        _reset_requests[key] = recent
        blocked = blocked or len(recent) >= limit
    if not blocked:
        for key in (("ip", ip), ("email", email)):
            _reset_requests[key].append(time.monotonic())
    return blocked


@bp.app_context_processor
def _app_mail_flag():
    return {"mail_enabled": mail_enabled()}


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if not mail_enabled():
        abort(404)
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()[:254]
        ip = request.remote_addr or "unknown"
        if email and not _reset_throttled(ip, email):
            row = get_db().execute("SELECT email, password_hash, is_demo FROM accounts WHERE email = ?",
                                   (email,)).fetchone()
            if row and not row["is_demo"]:
                link = url_for("auth.reset_password", token=make_reset_token(row["email"], row["password_hash"]),
                               _external=True)
                mailer.send_mail(current_app.config["MAIL"], row["email"], "Reset your StockPing password",
                                 "Someone asked to reset the password for your StockPing account.\n\n"
                                 f"To choose a new password, open this link (it works for 1 hour):\n{link}\n\n"
                                 "If you did not ask for this, you can ignore this email. "
                                 "Your password has not changed.")
        # Always the same answer, so nobody can find out which emails have an account.
        flash(RESET_SENT_MESSAGE, "info")
        return redirect(url_for("auth.login"))
    return render_template("auth/forgot_password.html")


@bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    if not mail_enabled():
        abort(404)
    invalid = "This reset link is not valid any more. Please ask for a new one."
    try:
        data = _reset_serializer().loads(token, max_age=RESET_MAX_AGE)
    except (SignatureExpired, BadSignature):   # expired, changed or made up
        data = None
    db = get_db()
    row = None
    if isinstance(data, dict):
        row = db.execute("SELECT id, password_hash FROM accounts WHERE email = ?",
                         (str(data.get("e", "")),)).fetchone()
        if row and data.get("p") != _password_fingerprint(row["password_hash"]):
            row = None
    if row is None:
        flash(invalid, "error")
        return redirect(url_for("auth.forgot_password"))
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        if password != request.form.get("password2", ""):
            error = "The two passwords don't match."
        else:
            error = password_error(password)
        if not error:
            with transaction(db):
                db.execute("UPDATE accounts SET password_hash = ? WHERE id = ?",
                           (generate_password_hash(password), row["id"]))
            session.clear()
            flash("Your password has been changed. You can log in now.", "success")
            return redirect(url_for("auth.login"))
    return render_template("auth/reset_password.html", token=token, error=error), (422 if error else 200)


def register_cli(app):
    @app.cli.command("set-login")
    @click.argument("email")
    @click.option("--new-email", default=None, help="Change the account's login email too.")
    @click.password_option(prompt="New password", help="Prompted for if not given.")
    def set_login(email, new_email, password):
        """Set a new password (and optionally a new email) for an account.

        Use it when a shop forgets its password, or to claim the data that existed before
        accounts were added: flask --app stockping set-login existing-shop@stockping.invalid
        --new-email you@shop.com
        """
        db = get_db()
        row = db.execute("SELECT id FROM accounts WHERE email = ?", (email.strip().lower(),)).fetchone()
        if row is None:
            raise click.ClickException(f"No account with the email {email}.")
        if err := password_error(password):
            raise click.ClickException(err)
        target = (new_email or email).strip().lower()
        if new_email and (err := email_error(target)):
            raise click.ClickException(err)
        try:
            with transaction(db):
                db.execute("UPDATE accounts SET email = ?, password_hash = ? WHERE id = ?",
                           (target, generate_password_hash(password), row["id"]))
        except sqlite3.IntegrityError:
            raise click.ClickException(f"Another account already uses {target}.") from None
        click.echo(f"Login updated: {target} can now log in with the new password.")
        if email.strip().lower() == LEGACY_EMAIL:
            click.echo("This account holds the products and history from before accounts were added.")
