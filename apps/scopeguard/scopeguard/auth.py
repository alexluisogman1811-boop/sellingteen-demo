"""Accounts: public sign-up, login, logout, optional email password reset, and the login_required guard.

Each freelancer or studio signs up for its own account and only ever sees its own data.
"""
import functools
import hashlib
import time

import click
from flask import (
    Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for,
)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

from . import mailer
from .db import get_db, seed_templates
from .services import EMAIL_RE, Form, ValidationError, now

bp = Blueprint("auth", __name__)

MAX_FAILURES = 5
WINDOW_SECONDS = 15 * 60
MAX_SIGNUPS_PER_HOUR = 10
MIN_PASSWORD = 10
RESET_MAX_AGE_SECONDS = 3600
MAX_RESETS_PER_EMAIL = 3
MAX_RESETS_PER_IP = 10
RESET_SENT_MESSAGE = "If an account exists, we sent a link to reset your password. It works for 1 hour."


def _limits():
    """In-memory attempt counters, kept per app (per process) so separate apps never share them."""
    return current_app.extensions.setdefault(
        "scopeguard_limits", {"failures": {}, "signups": {}, "resets": {}})


def _recent(store, key, window):
    recent = [t for t in store.get(key, []) if time.time() - t < window]
    store[key] = recent
    return recent


def _too_many_failures(ip):
    return len(_recent(_limits()["failures"], ip, WINDOW_SECONDS)) >= MAX_FAILURES


def record_failure(ip):
    _limits()["failures"].setdefault(ip, []).append(time.time())


@bp.before_app_request
def load_account():
    account_id = session.get("account_id")
    g.account = None
    if account_id is not None:
        g.account = get_db().execute(
            "SELECT id, name, email, is_demo, created_at, paid_until FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if g.account is None:
            session.clear()
    g.user = g.account  # templates and the client portal check "is someone signed in?" through g.user


def current_path():
    """The page being asked for, including the mount prefix (e.g. /scopeguard) and the query string."""
    return (request.script_root + request.full_path).rstrip("?")


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.account is None:
            return redirect(url_for("auth.login", next=current_path()))
        return view(*args, **kwargs)

    return wrapped


def _safe_next(target):
    # Only allow local paths on this site, never another host.
    if (target and target.startswith("/") and not target.startswith("//") and "\\" not in target
            and not any(ord(ch) < 32 for ch in target)):
        return target
    return url_for("public.index")


def _start_session(account_id):
    session.clear()
    session["account_id"] = account_id


def validate_password(form, password, label="Password"):
    if password and len(password) < MIN_PASSWORD:
        form.errors.append(f"{label} must be at least {MIN_PASSWORD} characters.")


def mail_on():
    return mailer.mail_enabled(current_app.config.get("MAIL"))


@bp.app_context_processor
def inject_mail_flag():
    return {"mail_on": mail_on()}


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if g.account is not None:
        return redirect(url_for("main.dashboard"))
    errors = []
    if request.method == "POST":
        ip = request.remote_addr or "?"
        signups = _limits()["signups"]
        if len(_recent(signups, ip, 3600)) >= MAX_SIGNUPS_PER_HOUR:
            abort(429, "Too many sign-ups from this network. Please try again in an hour.")
        form = Form(request.form)
        name = form.text("name", "Your name or business name", max_len=100)
        email = (form.email("email", "Email", required=True) or "").lower()
        password = form.text("password", "Password", max_len=200)
        validate_password(form, password)
        db = get_db()
        if email and db.execute("SELECT 1 FROM accounts WHERE email = ?", (email,)).fetchone():
            form.errors.append("An account with this email already exists. Log in instead.")
        try:
            form.check()
        except ValidationError as exc:
            errors = exc.errors
        else:
            account_id = db.execute(
                "INSERT INTO accounts (name, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (name, email, generate_password_hash(password), now()),
            ).lastrowid
            seed_templates(db, account_id)
            db.commit()
            signups.setdefault(ip, []).append(time.time())
            _start_session(account_id)
            flash("Account created. Your free trial has started."
                  if current_app.config["BILLING_ENABLED"] else "Account created.", "success")
            return redirect(url_for("main.dashboard"))
    return render_template("auth/signup.html", errors=errors, form=request.form), (400 if errors else 200)


@bp.route("/setup")
def setup():
    # The old one-time "first owner" page; everyone signs up for their own account now.
    return redirect(url_for("auth.signup"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        ip = request.remote_addr or "?"
        if _too_many_failures(ip):
            abort(429, "Too many failed sign-in attempts. Try again in 15 minutes.")
        login_id = request.form.get("email", "").strip()
        db = get_db()
        account = db.execute("SELECT * FROM accounts WHERE email = ?", (login_id.lower(),)).fetchone()
        if account is None and login_id:
            # An owner upgraded from the single-owner version can still use their old username.
            account = db.execute("SELECT * FROM accounts WHERE legacy_username = ?", (login_id,)).fetchone()
        if account and check_password_hash(account["password_hash"], request.form.get("password", "")):
            _limits()["failures"].pop(ip, None)
            _start_session(account["id"])
            return redirect(_safe_next(request.args.get("next")))
        record_failure(ip)
        error = "Incorrect email or password."
    return render_template("login.html", error=error, next=request.args.get("next")), (401 if error else 200)


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("Signed out.", "success")
    return redirect(url_for("auth.login"))


# ---------------------------------------------------------------- forgot password (only when email is on)


def _reset_serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="pw-reset")


def _hash_tag(password_hash):
    # Short fingerprint of the current password hash: the link stops working once the password changes.
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]


def _reset_allowed(ip, email):
    store = _limits()["resets"]
    keys = (("email", email), ("ip", ip))
    for key in keys:
        _recent(store, key, 3600)
    if len(store[keys[0]]) >= MAX_RESETS_PER_EMAIL or len(store[keys[1]]) >= MAX_RESETS_PER_IP:
        return False
    for key in keys:
        store[key].append(time.time())
    return True


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if not mail_on():
        abort(404)
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        ip = request.remote_addr or "?"
        if EMAIL_RE.match(email) and _reset_allowed(ip, email):
            row = get_db().execute("SELECT email, password_hash, is_demo FROM accounts WHERE email = ?",
                                   (email,)).fetchone()
            if row and not row["is_demo"]:
                token = _reset_serializer().dumps({"e": row["email"], "h": _hash_tag(row["password_hash"])})
                link = url_for("auth.reset_password", token=token, _external=True)
                mailer.send_mail(current_app.config["MAIL"], row["email"], "Reset your ScopeGuard password",
                                 "Someone asked to reset the password for your ScopeGuard account.\n\n"
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
    db = get_db()
    row = None
    if isinstance(data, dict):
        row = db.execute("SELECT id, password_hash FROM accounts WHERE email = ?",
                         (str(data.get("e", "")),)).fetchone()
    if row is None or data.get("h") != _hash_tag(row["password_hash"]):
        flash("This reset link is not valid any more. Please ask for a new one.", "error")
        return redirect(url_for("auth.forgot_password"))
    errors = []
    if request.method == "POST":
        new = request.form.get("new_password", "")
        if len(new) < MIN_PASSWORD:
            errors.append(f"Password must be at least {MIN_PASSWORD} characters.")
        elif new != request.form.get("confirm_password", ""):
            errors.append("The two passwords don't match.")
        if not errors:
            db.execute("UPDATE accounts SET password_hash = ? WHERE id = ?", (generate_password_hash(new), row["id"]))
            db.commit()
            session.clear()
            flash("Your password has been changed. Please log in.", "success")
            return redirect(url_for("auth.login"))
    return render_template("auth/reset_password.html", token=token, errors=errors), (400 if errors else 200)


def register_cli(app):
    @app.cli.command("set-password")
    @click.argument("email")
    @click.password_option()
    def set_password(email, password):
        """Set a new password for an account (for owners who are locked out)."""
        if len(password) < MIN_PASSWORD:
            raise click.ClickException(f"Password must be at least {MIN_PASSWORD} characters.")
        db = get_db()
        cur = db.execute("UPDATE accounts SET password_hash = ? WHERE email = ?",
                         (generate_password_hash(password), email.strip().lower()))
        if cur.rowcount != 1:
            raise click.ClickException(f"No account with the email {email}.")
        db.commit()
        click.echo(f"{email}: password updated")
