"""Account self-service: export all data, delete the business account, and optional password reset by email.

The routes live on the auth blueprint (endpoints auth.export_data, auth.delete_account, auth.forgot_password,
auth.reset_password) so they sit next to login and change password.
"""
import csv
import hashlib
import io
import time
import zipfile
from datetime import date

from flask import abort, current_app, flash, g, redirect, render_template, request, send_file, session, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer
from werkzeug.security import check_password_hash

from . import auth, mailer, storage
from .auth import bp, login_required, manager_required, validate_password
from .db import get_db
from .utils import EMAIL_RE, csv_safe

# Every table that holds one business's data, children first so deleting in this order never breaks a reference.
ACCOUNT_TABLES = ("job_events", "share_links", "photos", "job_items", "jobs", "template_items", "templates",
                  "properties", "customers", "users")
# Never exported: secrets that are of no use to the customer.
SECRET_COLUMNS = {"password_hash", "token_hash"}


def _check_current_password(password):
    row = get_db().execute("SELECT password_hash FROM users WHERE id = ?", (g.user["id"],)).fetchone()
    return row is not None and check_password_hash(row["password_hash"], password or "")


# ---------------------------------------------------------------- export

def build_export(account_id):
    """A ZIP (bytes buffer) of CSV files with all of one business's data, plus its photo files."""
    db = get_db()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        def write_csv(name, cursor):
            columns = [d[0] for d in cursor.description]
            keep = [i for i, c in enumerate(columns) if c not in SECRET_COLUMNS]
            out = io.StringIO()
            writer = csv.writer(out)
            writer.writerow([columns[i] for i in keep])
            for row in cursor.fetchall():
                writer.writerow([csv_safe(row[i]) for i in keep])
            zf.writestr(name, "﻿" + out.getvalue())

        write_csv("account.csv", db.execute(
            "SELECT id, name, email, paid_until, created_at FROM accounts WHERE id = ?", (account_id,)))
        for table in reversed(ACCOUNT_TABLES):  # table names are constants above
            write_csv(f"{table}.csv",
                      db.execute(f"SELECT * FROM {table} WHERE account_id = ? ORDER BY id", (account_id,)))
        for (stored_name,) in db.execute("SELECT stored_name FROM photos WHERE account_id = ?",
                                         (account_id,)).fetchall():
            try:
                zf.write(storage.stored_path(stored_name), f"photos/{stored_name}")
            except (ValueError, OSError):
                continue  # a missing photo file is still listed in photos.csv
    buf.seek(0)
    return buf


@bp.route("/account/export.zip")
@manager_required
def export_data():
    """Download all of this business's data. Works even after the trial ends."""
    return send_file(build_export(g.user["account_id"]), mimetype="application/zip", as_attachment=True,
                     download_name=f"proofofwork-export-{date.today().strftime('%Y%m%d')}.zip")


# ---------------------------------------------------------------- delete

def delete_account_data(account_id):
    """Delete a business account with ALL its rows (its managers and workers too) and its photo files.

    Rows are deleted table by table (not only through ON DELETE CASCADE) so databases upgraded from the
    single-business version, whose added account_id columns have no cascade, are cleaned up too."""
    db = get_db()
    stored = [r[0] for r in db.execute("SELECT stored_name FROM photos WHERE account_id = ?", (account_id,))]
    with db:
        for table in ACCOUNT_TABLES:  # table names are constants above
            db.execute(f"DELETE FROM {table} WHERE account_id = ?", (account_id,))
        db.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    for name in stored:
        storage.delete_stored(name)


@bp.route("/account/delete", methods=["GET", "POST"])
@login_required
def delete_account():
    """Managers can delete their whole business account. Workers are told to ask their manager."""
    if g.user["role"] != "manager":
        abort(403)
    if request.method == "POST":
        typed = (request.form.get("confirm") or "").strip()
        errors = []
        if typed != "DELETE" and typed.lower() != (g.user["email"] or "").lower():
            errors.append("Type DELETE (or your email address) to confirm.")
        if not _check_current_password(request.form.get("password")):
            errors.append("Your password is incorrect.")
        if errors:
            for error in errors:
                flash(error, "error")
            return render_template("auth/delete.html"), 400
        delete_account_data(g.user["account_id"])
        session.clear()
        flash("Your business account and all its data have been deleted.", "info")
        return redirect(url_for("main.index"))
    return render_template("auth/delete.html")


# ---------------------------------------------------------------- optional password reset by email

RESET_MAX_AGE = 3600
RESET_LIMIT_PER_HOUR = 5
_reset_requests = {}


def mail_on():
    return mailer.mail_enabled(current_app.config.get("MAIL"))


def _serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="pw-reset")


def _fingerprint(password_hash):
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()[:16]


def _reset_allowed(key):
    now = time.monotonic()
    recent = [t for t in _reset_requests.get(key, []) if now - t < 3600]
    allowed = len(recent) < RESET_LIMIT_PER_HOUR
    if allowed:
        recent.append(now)
    _reset_requests[key] = recent
    return allowed


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if not mail_on():
        abort(404)
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()[:254]
        allowed = _reset_allowed(("ip", request.remote_addr or "")) and _reset_allowed(("email", email.lower()))
        user = None
        if allowed and EMAIL_RE.match(email):
            user = get_db().execute("SELECT id, email, name, password_hash FROM users WHERE email = ? AND active = 1",
                                    (email,)).fetchone()
        if user is not None:
            token = _serializer().dumps({"e": user["email"].lower(), "h": _fingerprint(user["password_hash"])})
            base = current_app.config.get("BASE_URL")
            link = (base + url_for("auth.reset_password", token=token) if base
                    else url_for("auth.reset_password", token=token, _external=True))
            mailer.send_mail(current_app.config["MAIL"], user["email"], "Reset your ProofOfWork password",
                             f"Hi {user['name']},\n\nSomeone asked to reset your ProofOfWork password. To choose a new "
                             f"password, open this link within 1 hour:\n\n{link}\n\nIf this was not you, ignore this "
                             "email. Your password stays the same.\n")
        # Always the same answer, so nobody can find out which emails have an account.
        flash("If an account exists, we sent a link to reset your password. Check your email.", "info")
        return redirect(url_for("auth.login"))
    return render_template("auth/forgot.html")


def _user_for_token(token):
    try:
        data = _serializer().loads(token, max_age=RESET_MAX_AGE)
        email, fingerprint = data["e"], data["h"]
    except (BadSignature, KeyError, TypeError):
        return None
    user = get_db().execute("SELECT id, password_hash FROM users WHERE email = ? AND active = 1", (email,)).fetchone()
    if user is None or _fingerprint(user["password_hash"]) != fingerprint:
        return None  # unknown user, or the password changed since (the link was already used)
    return user


@bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    if not mail_on():
        abort(404)
    user = _user_for_token(token)
    if user is None:
        flash("This reset link is invalid, has expired or was already used. Ask for a new one.", "error")
        return redirect(url_for("auth.forgot_password"))
    if request.method == "POST":
        password = request.form.get("password") or ""
        errors = validate_password(password, request.form.get("confirm") or "")
        if not errors:
            db = get_db()
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                       (auth.generate_password_hash(password), user["id"]))
            db.commit()
            flash("Your password was changed. You can sign in now.", "success")
            return redirect(url_for("auth.login"))
        for error in errors:
            flash(error, "error")
        return render_template("auth/reset.html", token=token), 400
    return render_template("auth/reset.html", token=token)
