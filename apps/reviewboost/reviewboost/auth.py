import csv
import hashlib
import io
import re
import sqlite3
import time
import zipfile
from datetime import date

from flask import (Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request,
                   session, url_for)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

from . import mailer
from .db import get_db
from .security import is_safe_local_path, login_required

bp = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Checked against when the email is unknown, so response time doesn't reveal which emails exist.
DUMMY_HASH = generate_password_hash("not-a-real-password")

# Simple in-memory brute-force protection (per app process).
LOGIN_WINDOW_SECONDS = 15 * 60
MAX_FAILURES_PER_EMAIL = 10
MAX_FAILURES_PER_IP = 30
_failed_logins = {}
MIN_PASSWORD_LENGTH = 8

# "Forgot password?" emails: links last 1 hour; a few requests per email / IP per 15 minutes.
RESET_MAX_AGE_SECONDS = 60 * 60
MAX_RESETS_PER_EMAIL = 3
MAX_RESETS_PER_IP = 10
_reset_requests = {}


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

DEFAULT_TEMPLATES = [
    (
        "Friendly thank-you",
        "Hi {first_name}, thanks for choosing {business_name} for your {service}. "
        "If you're happy with the work, would you mind leaving us a quick review? "
        "It really helps a small local business: {review_link}",
    ),
    (
        "Short and simple",
        "Hi {first_name}, it's {business_name}. Thanks again for having us out for your "
        "{service}. A quick review would mean a lot: {review_link}",
    ),
]


def validate_review_link(link):
    if link and (not re.match(r"^https?://\S+$", link) or len(link) > 300):
        return "Paste the full web address of your review page, starting with https://"
    return None


def safe_next(target):
    # Only allow local paths to avoid open redirects.
    return target if is_safe_local_path(target) else url_for("customers.dashboard")


def start_session(business_id):
    session.clear()
    session.permanent = True  # stay logged in on the owner's phone for 30 days
    session["business_id"] = business_id


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if g.business:
        return redirect(url_for("customers.dashboard"))
    form, errors = {}, {}
    if request.method == "POST":
        form = {k: request.form.get(k, "").strip() for k in ("name", "email", "review_link")}
        password = request.form.get("password", "")
        form["email"] = form["email"].lower()
        if not form["name"]:
            errors["name"] = "Business name is required."
        elif len(form["name"]) > 100:
            errors["name"] = "Business name is too long."
        if not EMAIL_RE.match(form["email"]) or len(form["email"]) > 254:
            errors["email"] = "Enter a valid email address."
        if len(password) < MIN_PASSWORD_LENGTH:
            errors["password"] = f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        if link_error := validate_review_link(form["review_link"]):
            errors["review_link"] = link_error

        if not errors:
            db = get_db()
            try:
                cur = db.execute(
                    "INSERT INTO businesses (name, email, password_hash, review_link) VALUES (?, ?, ?, ?)",
                    (form["name"], form["email"], generate_password_hash(password), form["review_link"]),
                )
            except sqlite3.IntegrityError:
                errors["email"] = "An account with this email already exists. Try logging in instead."
            else:
                db.executemany(
                    "INSERT INTO message_templates (business_id, name, body) VALUES (?, ?, ?)",
                    [(cur.lastrowid, n, b) for n, b in DEFAULT_TEMPLATES],
                )
                db.commit()
                start_session(cur.lastrowid)
                flash("Welcome to ReviewBoost! Add your first customer to get started.", "success")
                return redirect(url_for("customers.dashboard"))
    return render_template("auth/signup.html", form=form, errors=errors), (400 if errors else 200)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.business:
        return redirect(url_for("customers.dashboard"))
    email, error = "", None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        ip = request.remote_addr or "unknown"
        if login_blocked(ip, email):
            error = "Too many failed attempts. Please wait 15 minutes and try again."
            return render_template("auth/login.html", email=email, error=error), 429
        row = get_db().execute(
            "SELECT id, password_hash FROM businesses WHERE email = ?", (email,)
        ).fetchone()
        valid = check_password_hash(row["password_hash"] if row else DUMMY_HASH, password)
        if row and valid:
            _failed_logins.pop(("email", email), None)
            start_session(row["id"])
            return redirect(safe_next(request.args.get("next")))
        record_failed_login(ip, email)
        error = "That email and password don't match. Please try again."
    return render_template("auth/login.html", email=email, error=error), (401 if error else 200)


@bp.route("/logout", methods=["POST"])
def logout():
    was_demo = bool(g.business and g.business["is_demo"] and g.business["email"].endswith("@demo.invalid"))
    session.clear()
    if request.form.get("next") == "signup":
        return redirect(url_for("auth.signup"))
    if was_demo:
        flash("You've left the demo. Create your own account whenever you're ready.", "info")
        return redirect(url_for("customers.index"))
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))


@bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    form = {"name": g.business["name"], "review_link": g.business["review_link"]}
    errors = {}
    if request.method == "POST":
        form = {k: request.form.get(k, "").strip() for k in ("name", "review_link")}
        if not form["name"]:
            errors["name"] = "Business name is required."
        elif len(form["name"]) > 100:
            errors["name"] = "Business name is too long."
        if link_error := validate_review_link(form["review_link"]):
            errors["review_link"] = link_error
        if not errors:
            db = get_db()
            db.execute(
                "UPDATE businesses SET name = ?, review_link = ? WHERE id = ?",
                (form["name"], form["review_link"], g.business["id"]),
            )
            if (form["name"], form["review_link"]) != (g.business["name"], g.business["review_link"]):
                db.execute(
                    "UPDATE customers SET message_stale = 1 WHERE business_id = ? AND message IS NOT NULL",
                    (g.business["id"],),
                )
            db.commit()
            flash("Settings saved.", "success")
            return redirect(url_for("auth.settings"))
    return render_template("auth/settings.html", form=form, errors=errors), (400 if errors else 200)


def is_temporary_demo(business):
    # One-click demo accounts have a random password nobody knows, so they confirm by typing DELETE.
    return bool(business["is_demo"]) and business["email"].endswith("@demo.invalid")


@bp.route("/account/delete", methods=["GET", "POST"])
@login_required
def delete_account():
    business = g.business
    temp_demo = is_temporary_demo(business)
    db = get_db()
    counts = {
        "customers": db.execute("SELECT COUNT(*) FROM customers WHERE business_id = ?",
                                (business["id"],)).fetchone()[0],
        "templates": db.execute("SELECT COUNT(*) FROM message_templates WHERE business_id = ?",
                                (business["id"],)).fetchone()[0],
    }
    error, status = None, 200
    if request.method == "POST":
        if temp_demo:
            confirmed = request.form.get("confirm", "").strip().upper() == "DELETE"
            if not confirmed:
                error = "Type DELETE to confirm."
        elif request.form.get("confirm", "").strip() != "DELETE" \
                and request.form.get("confirm", "").strip().lower() != business["email"]:
            error, confirmed = "Type DELETE (or your email) in the box to confirm.", False
        else:
            ip = request.remote_addr or "unknown"
            if login_blocked(ip, business["email"]):
                error, status = "Too many wrong passwords. Please wait 15 minutes and try again.", 429
                confirmed = False
            else:
                row = db.execute("SELECT password_hash FROM businesses WHERE id = ?", (business["id"],)).fetchone()
                confirmed = check_password_hash(row["password_hash"], request.form.get("password", ""))
                if not confirmed:
                    record_failed_login(ip, business["email"])
                    error = "That password isn't right. Your account has not been deleted."
        if confirmed:
            # Explicit deletes as well as ON DELETE CASCADE, in case foreign keys are off on an old database.
            db.execute("DELETE FROM customers WHERE business_id = ?", (business["id"],))
            db.execute("DELETE FROM message_templates WHERE business_id = ?", (business["id"],))
            db.execute("DELETE FROM businesses WHERE id = ?", (business["id"],))
            db.commit()
            session.clear()
            flash("Your account and all its data have been deleted.", "info")
            return redirect(url_for("customers.index"))
        status = status if status != 200 else 400
    return render_template("auth/delete_account.html", counts=counts, temp_demo=temp_demo,
                           error=error), status


@bp.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    errors, status = {}, 200
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        ip = request.remote_addr or "unknown"
        db = get_db()
        if login_blocked(ip, g.business["email"]):
            errors["current_password"] = "Too many wrong passwords. Please wait 15 minutes and try again."
            status = 429
        else:
            row = db.execute("SELECT password_hash FROM businesses WHERE id = ?", (g.business["id"],)).fetchone()
            if not check_password_hash(row["password_hash"], current):
                record_failed_login(ip, g.business["email"])
                errors["current_password"] = "That isn't your current password."
            if len(new) < MIN_PASSWORD_LENGTH:
                errors["new_password"] = f"New password must be at least {MIN_PASSWORD_LENGTH} characters."
            elif new != confirm:
                errors["confirm_password"] = "The two new passwords don't match."
        if not errors:
            db.execute("UPDATE businesses SET password_hash = ? WHERE id = ?",
                       (generate_password_hash(new), g.business["id"]))
            db.commit()
            flash("Your password has been changed.", "success")
            return redirect(url_for("auth.settings"))
        status = status if status != 200 else 400
    return render_template("auth/change_password.html", errors=errors), status


def _csv_bytes(rows, columns):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(columns)
    for r in rows:
        writer.writerow([r[c] for c in columns])
    return out.getvalue().encode("utf-8-sig")  # BOM so Excel opens it correctly


@bp.route("/account/export")
@login_required
def export_data():
    """Download everything this account has stored, as a ZIP of CSV files (never the password)."""
    db = get_db()
    bid = g.business["id"]
    business_cols = ["name", "email", "review_link", "created_at", "paid_until"]
    customer_cols = ["id", "name", "phone", "email", "service", "job_date", "status", "message",
                     "message_generated_at", "created_at", "updated_at"]
    template_cols = ["id", "name", "body", "created_at"]
    files = {
        "account.csv": _csv_bytes(db.execute(f"SELECT {', '.join(business_cols)} FROM businesses WHERE id = ?",
                                             (bid,)).fetchall(), business_cols),
        "customers.csv": _csv_bytes(db.execute(f"SELECT {', '.join(customer_cols)} FROM customers "
                                               "WHERE business_id = ? ORDER BY id", (bid,)).fetchall(),
                                    customer_cols),
        "message_templates.csv": _csv_bytes(db.execute(f"SELECT {', '.join(template_cols)} FROM message_templates "
                                                       "WHERE business_id = ? ORDER BY id", (bid,)).fetchall(),
                                            template_cols),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    filename = f"reviewboost-export-{date.today().isoformat()}.zip"
    return Response(buf.getvalue(), mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# Forgot password (only when the optional email is switched on) ---------------------------

def _mail_on():
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


RESET_SENT_MESSAGE = "If an account exists, we sent a link to reset your password. It works for 1 hour."


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if not _mail_on():
        abort(404)
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        ip = request.remote_addr or "unknown"
        if EMAIL_RE.match(email) and _reset_allowed(ip, email):
            row = get_db().execute("SELECT id, email, password_hash, is_demo FROM businesses WHERE email = ?",
                                   (email,)).fetchone()
            if row and not row["is_demo"]:
                token = _reset_serializer().dumps({"e": row["email"], "h": _hash_tag(row["password_hash"])})
                link = url_for("auth.reset_password", token=token, _external=True)
                mailer.send_mail(current_app.config["MAIL"], row["email"], "Reset your ReviewBoost password",
                                 "Someone asked to reset the password for your ReviewBoost account.\n\n"
                                 f"To choose a new password, open this link (it works for 1 hour):\n{link}\n\n"
                                 "If this wasn't you, you can ignore this email. Your password has not changed.")
        # Always the same answer, so nobody can find out which emails have an account.
        flash(RESET_SENT_MESSAGE, "info")
        return redirect(url_for("auth.login"))
    return render_template("auth/forgot_password.html")


@bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    if not _mail_on():
        abort(404)
    invalid = "This reset link is not valid any more. Please ask for a new one."
    try:
        data = _reset_serializer().loads(token, max_age=RESET_MAX_AGE_SECONDS)
    except (SignatureExpired, BadSignature):
        data = None
    db = get_db()
    row = None
    if isinstance(data, dict):
        row = db.execute("SELECT id, password_hash FROM businesses WHERE email = ?",
                         (str(data.get("e", "")),)).fetchone()
    if row is None or data.get("h") != _hash_tag(row["password_hash"]):
        flash(invalid, "error")
        return redirect(url_for("auth.forgot_password"))
    errors = {}
    if request.method == "POST":
        new = request.form.get("new_password", "")
        if len(new) < MIN_PASSWORD_LENGTH:
            errors["new_password"] = f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        elif new != request.form.get("confirm_password", ""):
            errors["confirm_password"] = "The two passwords don't match."
        if not errors:
            db.execute("UPDATE businesses SET password_hash = ? WHERE id = ?", (generate_password_hash(new), row["id"]))
            db.commit()
            _failed_logins.pop(("email", data["e"]), None)
            session.clear()
            flash("Your password has been changed. Please log in.", "success")
            return redirect(url_for("auth.login"))
    return render_template("auth/reset_password.html", token=token, errors=errors), (400 if errors else 200)
