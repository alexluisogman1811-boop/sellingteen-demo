"""Sign up, log in, log out, accept a team invite."""
import sqlite3
from urllib.parse import urlparse

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

from .. import mailer
from ..db import get_db
from ..links import shareable_url
from ..passwords import set_password
from .. import ratelimit
from ..ratelimit import limit_or_abort
from ..security import (audit, clear_login_failures, hash_token, login_blocked, login_user,
                        logout_user, record_login_failure)
from ..util import now_iso, parse_iso, sha256_hex, utcnow
from ..validation import Form

bp = Blueprint("auth", __name__)

# Used to keep login timing similar whether or not the email exists.
_DUMMY_HASH = generate_password_hash("not-a-real-password")


def _safe_next(target):
    """Only allow paths inside this app (including its URL prefix, e.g. /signedextra)."""
    if not target or "\\" in target:
        return None
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or not target.startswith("/") or target.startswith("//"):
        return None
    root = request.script_root or ""
    if root and not (target == root or target.startswith(root + "/")):
        return None
    return target


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if g.user:
        return redirect(url_for("core.dashboard"))
    form = Form(request.form)
    if request.method == "POST":
        limit_or_abort("RATE_LIMIT_SIGNUP", "Too many sign-ups from your network. Please try again in an hour.")
        company_name = form.text("company_name", "Company name", max_len=120)
        name = form.text("name", "Your name", max_len=120)
        email = form.email("email")
        password = form.password("password")
        if form.ok:
            db = get_db()
            now = now_iso()
            try:
                cur = db.execute("INSERT INTO companies (name, email, created_at) VALUES (?, ?, ?)",
                                 (company_name, email, now))
                company_id = cur.lastrowid
                cur = db.execute(
                    "INSERT INTO users (company_id, name, email, password_hash, role, created_at)"
                    " VALUES (?, ?, ?, ?, 'owner', ?)",
                    (company_id, name, email, generate_password_hash(password), now))
                user_id = cur.lastrowid
                audit(company_id, "company.created", actor=name, user_id=user_id)
                db.commit()
            except sqlite3.IntegrityError:
                db.rollback()
                form.errors["email"] = "An account with this email already exists."
            else:
                user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
                login_user(user)
                flash("Account created. Add your labor rates next, then your first GC and job.", "success")
                return redirect(url_for("core.settings"))
    return render_template("auth/signup.html", form=form)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("core.dashboard"))
    form = Form(request.form)
    if request.method == "POST":
        email = form.email("email")
        password = request.form.get("password") or ""
        if form.ok:
            if login_blocked(email):
                form.errors["email"] = "Too many failed attempts. Wait 15 minutes and try again."
            else:
                user = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
                valid = check_password_hash(
                    user["password_hash"] if user and user["password_hash"] else _DUMMY_HASH, password)
                if user and user["active"] and user["password_hash"] and valid:
                    clear_login_failures(email)
                    login_user(user)
                    return redirect(_safe_next(request.args.get("next")) or url_for("core.dashboard"))
                record_login_failure(email)
                form.errors["email"] = "Email or password is incorrect."
    return render_template("auth/login.html", form=form)


@bp.route("/logout", methods=["POST"])
def logout():
    logout_user()
    flash("You're logged out.", "success")
    return redirect(url_for("auth.login"))


@bp.route("/invite/<token>", methods=["GET", "POST"])
def accept_invite(token):
    db = get_db()
    invite = db.execute("SELECT * FROM invites WHERE token_hash = ?", (hash_token(token),)).fetchone()
    if invite is None or invite["used_at"] or parse_iso(invite["expires_at"]) < utcnow():
        abort(404, description="This invite link is invalid or has expired. Ask your office for a new one.")
    user = db.execute("SELECT * FROM users WHERE id = ? AND company_id = ?",
                      (invite["user_id"], invite["company_id"])).fetchone()
    if user is None or not user["active"]:
        abort(404)
    company = db.execute("SELECT name FROM companies WHERE id = ?", (invite["company_id"],)).fetchone()
    form = Form(request.form)
    if request.method == "POST":
        password = form.password("password")
        if password != (request.form.get("password_confirm") or ""):
            form.errors["password_confirm"] = "Passwords don't match."
        if form.ok:
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                       (generate_password_hash(password), user["id"]))
            db.execute("UPDATE invites SET used_at = ? WHERE id = ?", (now_iso(), invite["id"]))
            audit(user["company_id"], "user.invite_accepted", actor=user["name"], user_id=user["id"])
            db.commit()
            login_user(db.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone())
            flash("Welcome! Your account is ready.", "success")
            return redirect(url_for("core.dashboard"))
    return render_template("auth/invite.html", form=form, invited=user, company=company)


@bp.route("/reset/<token>", methods=["GET", "POST"])
def reset_password(token):
    """One-time password reset from a link the Owner (or support) created and sent by hand."""
    db = get_db()
    reset = db.execute("SELECT * FROM password_resets WHERE token_hash = ?", (hash_token(token),)).fetchone()
    if reset is None or reset["used_at"] or parse_iso(reset["expires_at"]) < utcnow():
        abort(404, description="This password-reset link is invalid, already used, or older than 24 hours. "
                               "Ask your company's Owner for a new one.")
    user = db.execute("SELECT * FROM users WHERE id = ? AND company_id = ?",
                      (reset["user_id"], reset["company_id"])).fetchone()
    if user is None or not user["active"]:
        abort(404)
    form = Form(request.form)
    if request.method == "POST":
        password = form.password("password")
        if password != (request.form.get("password_confirm") or ""):
            form.errors["password_confirm"] = "Passwords don't match."
        if form.ok:
            set_password(user, password)
            db.execute("UPDATE password_resets SET used_at = ? WHERE id = ?", (now_iso(), reset["id"]))
            audit(user["company_id"], "user.password_reset", actor=user["name"], user_id=user["id"])
            db.commit()
            login_user(db.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone())
            flash("Your password was changed. Any other devices were signed out.", "success")
            return redirect(url_for("core.dashboard"))
    return render_template("auth/reset.html", form=form, user=user)


@bp.route("/forgot")
def forgot():
    """Manual reset help (always available): Owners create reset links for their team; support helps Owners."""
    return render_template("auth/forgot.html", support_email=current_app.config["SUPPORT_EMAIL"])


# --- optional self-service reset by email (only when email is configured) ---------

EMAIL_RESET_MAX_AGE = 60 * 60  # the emailed link works for 1 hour
EMAIL_RESET_SENT = "If an account exists, we sent a link. Check your email (it works for 1 hour)."


def _mail_on():
    return mailer.mail_enabled(current_app.config.get("MAIL"))


def _reset_serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="pw-reset")


def _password_fingerprint(user):
    # Changes whenever the password changes, so a used link stops working.
    return sha256_hex(user["password_hash"] or "")[:16]


@bp.route("/forgot/email", methods=["GET", "POST"])
def forgot_email():
    if not _mail_on():
        abort(404)
    form = Form(request.form)
    if request.method == "POST":
        limit_or_abort("RATE_LIMIT_RESET", "Too many reset requests from your network. Please try again in an hour.")
        email = form.email("email")
        if form.ok:
            key = "email:" + email
            if not ratelimit.blocked("RATE_LIMIT_RESET_EMAIL", key):
                ratelimit.record("RATE_LIMIT_RESET_EMAIL", key)
                user = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
                if user and user["active"] and user["password_hash"]:
                    token = _reset_serializer().dumps({"e": user["email"], "h": _password_fingerprint(user)})
                    link = shareable_url("auth.email_reset", token=token)
                    mailer.send_mail(current_app.config["MAIL"], user["email"], "Reset your SignedExtra password",
                                     f"Hi {user['name']},\n\nUse this link to choose a new SignedExtra password."
                                     f" It works once, for 1 hour:\n\n{link}\n\n"
                                     "If you didn't ask for this, you can ignore this email.")
            flash(EMAIL_RESET_SENT, "success")
            return redirect(url_for("auth.login"))
    return render_template("auth/forgot_email.html", form=form)


@bp.route("/forgot/email/<token>", methods=["GET", "POST"])
def email_reset(token):
    if not _mail_on():
        abort(404)
    invalid = "This reset link is invalid, already used, or older than 1 hour. Ask for a new one."
    try:
        data = _reset_serializer().loads(token, max_age=EMAIL_RESET_MAX_AGE)
    except (SignatureExpired, BadSignature):
        abort(404, description=invalid)
    db = get_db()
    user = None
    if isinstance(data, dict):
        user = db.execute("SELECT * FROM users WHERE email = ?", (str(data.get("e", "")),)).fetchone()
    if (user is None or not user["active"] or not user["password_hash"]
            or data.get("h") != _password_fingerprint(user)):
        abort(404, description=invalid)
    form = Form(request.form)
    if request.method == "POST":
        password = form.password("password")
        if password != (request.form.get("password_confirm") or ""):
            form.errors["password_confirm"] = "Passwords don't match."
        if form.ok:
            set_password(user, password)
            audit(user["company_id"], "user.password_reset_by_email", actor=user["name"], user_id=user["id"])
            db.commit()
            login_user(db.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone())
            flash("Your password was changed. Any other devices were signed out.", "success")
            return redirect(url_for("core.dashboard"))
    return render_template("auth/reset.html", form=form, user=user, email_link=True)
