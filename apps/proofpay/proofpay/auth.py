"""Sign up, log in, log out, business settings."""
import csv
import hashlib
import hmac
import io
import os
import re
import shutil
import threading
import zipfile
import time
from urllib.parse import urlparse

from flask import (Blueprint, abort, current_app, flash, redirect, render_template, request,
                   send_file, session, url_for)
from flask_login import current_user, login_required, login_user, logout_user
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import mailer
from .helpers import absolute_url, current_business
from .models import Business, Customer, Invoice, Job, JobPhoto, User, db, utcnow

bp = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 8


def _safe_next(target: str | None) -> str:
    """Only allow same-site relative redirects after login."""
    if target and target.startswith("/") and not target.startswith("//") \
            and not urlparse(target).netloc and "\\" not in target:
        return target
    return url_for("jobs.dashboard")


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for("jobs.dashboard"))
    form = {"business_name": "", "email": ""}
    errors = {}
    if request.method == "POST":
        form["business_name"] = request.form.get("business_name", "").strip()
        form["email"] = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not form["business_name"]:
            errors["business_name"] = "Enter your business name."
        elif len(form["business_name"]) > 120:
            errors["business_name"] = "Keep the business name under 120 characters."
        if not EMAIL_RE.match(form["email"]) or len(form["email"]) > 255:
            errors["email"] = "Enter a valid email address."
        if len(password) < MIN_PASSWORD:
            errors["password"] = f"Use at least {MIN_PASSWORD} characters."
        elif len(password) > 200:
            errors["password"] = "That password is too long."
        if not errors and db.session.scalar(db.select(User).filter_by(email=form["email"])):
            errors["email"] = "An account with this email already exists. Try logging in."

        if not errors:
            user = User(email=form["email"])
            user.set_password(password)
            user.business = Business(name=form["business_name"], email=form["email"])
            db.session.add(user)
            db.session.commit()
            session.clear()
            login_user(user)
            flash("Welcome to ProofPay! Create your first job below.", "success")
            return redirect(url_for("jobs.dashboard"))
    return render_template("auth/signup.html", form=form, errors=errors), 400 if errors else 200


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("jobs.dashboard"))
    email = ""
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()[:255]
        password = request.form.get("password", "")[:200]
        key = f"{request.remote_addr}|{email}"
        if _too_many_failures(key):
            error = "Too many failed attempts. Please wait 15 minutes and try again."
            return render_template("auth/login.html", email=email, error=error), 429
        user = db.session.scalar(db.select(User).filter_by(email=email)) if email else None
        if user and user.check_password(password) and not user.business.is_demo:
            _failures.pop(key, None)
            session.clear()  # prevent session fixation
            login_user(user, remember=bool(request.form.get("remember")))
            return redirect(_safe_next(request.args.get("next")))
        _record_failure(key)
        # Same message whether the email exists or not.
        error = "Email or password is incorrect."
    return render_template("auth/login.html", email=email, error=error), 401 if error else 200


# Failed-login timestamps per "ip|email". In-process only: fine for a single server; use a
# shared store (e.g. Redis via Flask-Limiter) when running several workers.
_failures: dict[str, list[float]] = {}
_failures_lock = threading.Lock()


def _recent(key: str) -> list[float]:
    window = current_app.config["LOGIN_LOCKOUT_SECONDS"]
    now = time.monotonic()
    times = [t for t in _failures.get(key, []) if now - t < window]
    if times:
        _failures[key] = times
    else:
        _failures.pop(key, None)
    return times


def _too_many_failures(key: str) -> bool:
    with _failures_lock:
        return len(_recent(key)) >= current_app.config["LOGIN_MAX_FAILURES"]


def _record_failure(key: str) -> None:
    with _failures_lock:
        _failures[key] = _recent(key) + [time.monotonic()]
        if len(_failures) > 10_000:  # keep memory bounded under a spray attack
            _failures.pop(next(iter(_failures)))


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    # Invalidate every existing session/remember cookie for this user, not just this one.
    current_user.session_version += 1
    db.session.commit()
    logout_user()
    session.clear()
    flash("You've been logged out.", "info")
    return redirect(url_for("public.home"))


@bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    business = current_business()
    errors = {}
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()[:255]
        if not name or len(name) > 120:
            errors["name"] = "Enter a business name (under 120 characters)."
        if email and not EMAIL_RE.match(email):
            errors["email"] = "Enter a valid email address."
        if not errors:
            business.name = name
            business.email = email or None
            business.trade = request.form.get("trade", "").strip()[:60] or None
            business.phone = request.form.get("phone", "").strip()[:40] or None
            business.payment_instructions = request.form.get("payment_instructions", "").strip()[:2000] or None
            db.session.commit()
            flash("Business details saved.", "success")
            return redirect(url_for("auth.settings"))
    return render_template("auth/settings.html", business=business, errors=errors)


# --- Account: change password, export, delete -----------------------------------------------

def _no_demo_changes():
    if current_user.business is not None and current_user.business.is_demo:
        flash("The demo account can't be changed. Sign up to get your own account.", "info")
        return redirect(url_for("auth.settings"))
    return None


@bp.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    blocked = _no_demo_changes()
    if blocked:
        return blocked
    errors = {}
    if request.method == "POST":
        current = request.form.get("current_password", "")[:200]
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if not current_user.check_password(current):
            errors["current_password"] = "Your current password is not right."
        if len(new) < MIN_PASSWORD:
            errors["new_password"] = f"Use at least {MIN_PASSWORD} characters."
        elif len(new) > 200:
            errors["new_password"] = "That password is too long."
        elif new != confirm:
            errors["confirm_password"] = "The two new passwords don't match."
        if not errors:
            user = current_user._get_current_object()
            user.set_password(new)
            user.session_version += 1  # log out every other session
            db.session.commit()
            session.clear()
            login_user(user)  # keep this browser logged in
            flash("Your password was changed.", "success")
            return redirect(url_for("auth.settings"))
    return render_template("auth/change_password.html", errors=errors), 400 if errors else 200


def _csv_cell(value):
    """Stop spreadsheet formula injection: cells starting with = + - @ are prefixed with '."""
    if value is None:
        return ""
    text = str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def _csv_bytes(header, rows) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for row in rows:
        writer.writerow([_csv_cell(v) for v in row])
    return buf.getvalue().encode("utf-8-sig")  # BOM so Excel opens it as UTF-8


@bp.route("/account/export")
@login_required
def export_data():
    """Download everything this account has stored, as a ZIP of CSV files. Always available."""
    business = current_business()
    bid = business.id
    customers = db.session.scalars(db.select(Customer).filter_by(business_id=bid).order_by(Customer.id)).all()
    jobs = db.session.scalars(db.select(Job).filter_by(business_id=bid).order_by(Job.id)).all()
    photos = db.session.scalars(db.select(JobPhoto).filter_by(business_id=bid).order_by(JobPhoto.id)).all()
    invoices = db.session.scalars(db.select(Invoice).filter_by(business_id=bid).order_by(Invoice.id)).all()
    job_ref = {j.id: j.ref for j in jobs}
    cust_ref = {c.id: c.ref for c in customers}

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("account.csv", _csv_bytes(
            ["login_email", "business_name", "trade", "phone", "business_email", "payment_instructions",
             "created_at", "paid_until"],
            [[current_user.email, business.name, business.trade, business.phone, business.email,
              business.payment_instructions, business.created_at, business.paid_until]]))
        zf.writestr("customers.csv", _csv_bytes(
            ["customer_ref", "name", "email", "phone", "created_at"],
            [[c.ref, c.name, c.email, c.phone, c.created_at] for c in customers]))
        zf.writestr("jobs.csv", _csv_bytes(
            ["job_ref", "customer_ref", "title", "description", "address", "price", "due_date", "status",
             "work_notes", "summary", "completed_at", "created_at", "updated_at"],
            [[j.ref, cust_ref.get(j.customer_id), j.title, j.description, j.address, f"{j.price:.2f}",
              j.due_date, j.status, j.work_notes, j.summary, j.completed_at, j.created_at, j.updated_at]
             for j in jobs]))
        zf.writestr("invoices.csv", _csv_bytes(
            ["job_ref", "amount", "currency", "status", "paid_via", "provider_reference", "paid_at",
             "created_at"],
            [[job_ref.get(i.job_id), f"{i.amount_cents / 100:.2f}", i.currency, i.status, i.paid_via,
              i.provider_reference, i.paid_at, i.created_at] for i in invoices]))
        zf.writestr("photos.csv", _csv_bytes(
            ["photo_ref", "job_ref", "kind", "file_name", "content_type", "size_bytes", "created_at"],
            [[p.ref, job_ref.get(p.job_id), p.kind, p.stored_name, p.content_type, p.size_bytes,
              p.created_at] for p in photos]))
    out.seek(0)
    resp = send_file(out, mimetype="application/zip", as_attachment=True,
                     download_name=f"proofpay-export-{utcnow().date().isoformat()}.zip")
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


@bp.route("/account/delete", methods=["GET", "POST"])
@login_required
def delete_account():
    """Delete the account, every row it owns and its uploaded photos, then log out."""
    blocked = _no_demo_changes()
    if blocked:
        return blocked
    errors = {}
    if request.method == "POST":
        typed = request.form.get("confirm", "").strip()
        password = request.form.get("password", "")[:200]
        if typed != "DELETE" and typed.lower() != current_user.email.lower():
            errors["confirm"] = "Type DELETE (or your email) to confirm."
        if not current_user.check_password(password):
            errors["password"] = "Your password is not right."
        if not errors:
            _delete_everything(current_user._get_current_object())
            logout_user()
            session.clear()
            flash("Your account and all its data were deleted.", "info")
            return redirect(url_for("public.home"))
    return render_template("auth/delete_account.html", errors=errors), 400 if errors else 200


def _delete_everything(user) -> None:
    business = user.business
    ref = business.ref if business is not None else None
    if business is not None:
        bid = business.id
        # Children first, so foreign keys (RESTRICT on job -> customer) never block the delete.
        for model in (Invoice, JobPhoto, Job, Customer):
            db.session.execute(db.delete(model).where(model.business_id == bid))
        db.session.execute(db.delete(Business).where(Business.id == bid))
    db.session.execute(db.delete(User).where(User.id == user.id))
    db.session.commit()
    db.session.expunge_all()
    if ref:
        folder = os.path.join(current_app.config["UPLOAD_FOLDER"], ref)
        if os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)


# --- Optional "forgot password" by email (only when email is set up) ------------------------

RESET_SALT = "pw-reset"
RESET_MAX_AGE = 60 * 60  # 1 hour
RESET_LIMIT = 5  # requests per IP and per email within LOGIN_LOCKOUT_SECONDS
RESET_MESSAGE = ("If an account exists, we sent a link to reset your password. "
                 "The link works for 1 hour.")


def mail_on() -> bool:
    return mailer.mail_enabled(current_app.config.get("MAIL"))


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.secret_key, salt=RESET_SALT)


def _pw_fingerprint(user) -> str:
    # Changes whenever the password changes, so a used link stops working.
    return hashlib.sha256(user.password_hash.encode()).hexdigest()[:16]


def _reset_user(token: str):
    try:
        data = _serializer().loads(token, max_age=RESET_MAX_AGE)
    except (SignatureExpired, BadSignature):
        return None
    if not isinstance(data, dict):
        return None
    user = db.session.scalar(db.select(User).filter_by(email=str(data.get("e", "")).lower()))
    if user is None or user.business is None or user.business.is_demo:
        return None
    if not hmac.compare_digest(str(data.get("h", "")), _pw_fingerprint(user)):
        return None
    return user


def _reset_throttled(keys) -> bool:
    with _failures_lock:
        if any(len(_recent(k)) >= RESET_LIMIT for k in keys):
            return True
    for k in keys:
        _record_failure(k)
    return False


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if not mail_on():
        abort(404)
    if current_user.is_authenticated:
        return redirect(url_for("jobs.dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()[:255]
        keys = [f"reset-ip|{request.remote_addr}", f"reset-email|{email}"]
        if not _reset_throttled(keys) and EMAIL_RE.match(email):
            user = db.session.scalar(db.select(User).filter_by(email=email))
            if user is not None and user.business is not None and not user.business.is_demo:
                token = _serializer().dumps({"e": user.email, "h": _pw_fingerprint(user)})
                link = absolute_url("auth.reset_password", token=token)
                mailer.send_mail(
                    current_app.config["MAIL"], user.email, "Reset your ProofPay password",
                    "Someone asked to reset the password for your ProofPay account.\n\n"
                    "To choose a new password, open this link. It works once, for 1 hour:\n"
                    f"{link}\n\n"
                    "If you did not ask for this, you can ignore this email. "
                    "Your password stays the same.\n")
        # Always the same answer, so nobody can find out which emails have an account.
        flash(RESET_MESSAGE, "info")
        return redirect(url_for("auth.login"))
    return render_template("auth/forgot_password.html")


@bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    if not mail_on():
        abort(404)
    user = _reset_user(token)
    if user is None:
        flash("That reset link is not valid or has expired. Please ask for a new one.", "error")
        return redirect(url_for("auth.forgot_password"))
    errors = {}
    if request.method == "POST":
        new = request.form.get("new_password", "")
        if len(new) < MIN_PASSWORD:
            errors["new_password"] = f"Use at least {MIN_PASSWORD} characters."
        elif len(new) > 200:
            errors["new_password"] = "That password is too long."
        elif new != request.form.get("confirm_password", ""):
            errors["confirm_password"] = "The two passwords don't match."
        if not errors:
            user.set_password(new)
            user.session_version += 1  # log out every old session
            db.session.commit()
            flash("Your password was changed. Please log in.", "success")
            return redirect(url_for("auth.login"))
    return render_template("auth/reset_password.html", errors=errors), 400 if errors else 200
