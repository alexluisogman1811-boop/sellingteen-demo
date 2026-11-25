"""Health check, data export, account deletion and the optional "Forgot password?" email.

All of these stay open after a free trial ends (QuoteFlow never locks accounts; see billing.py).
"""
import csv
import hashlib
import io
import zipfile
from datetime import datetime, timezone

from flask import (
    Response, abort, current_app, flash, g, redirect, render_template, request, session, url_for,
)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

from . import billing
from . import db as store
from . import mailer

RESET_SALT = "pw-reset"
RESET_MAX_AGE = 60 * 60  # reset links work for 1 hour
RESET_SENT_MESSAGE = "If an account exists for that email, we sent a link to reset your password."


# ---------- helpers ----------

def mail_on():
    return mailer.mail_enabled(current_app.config.get("MAIL"))


def _serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt=RESET_SALT)


def _pw_fingerprint(password_hash):
    # Changes whenever the password changes, so a used link stops working.
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]


def make_reset_token(user):
    return _serializer().dumps({"e": user["email"], "h": _pw_fingerprint(user["password_hash"])})


def user_from_reset_token(token):
    try:
        data = _serializer().loads(token, max_age=RESET_MAX_AGE)
    except (SignatureExpired, BadSignature):
        return None
    if not isinstance(data, dict):
        return None
    user = store.get_db().execute("SELECT * FROM users WHERE email = ?", (str(data.get("e", "")),)).fetchone()
    if user is None or data.get("h") != _pw_fingerprint(user["password_hash"]):
        return None
    return user


def _csv(rows, columns):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(columns)
    for r in rows:
        w.writerow(["" if r[c] is None else r[c] for c in columns])
    return out.getvalue()


def export_zip(user_id):
    """A ZIP of CSV files with everything stored for this one account."""
    db = store.get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    files = {
        "account.csv": _csv([user], ["email", "created_at", "paid_until", "subscription_status",
                                     "subscription_period_end", "cancel_at_period_end", "onboarded"]),
        "business_profile.csv": _csv(
            db.execute("SELECT * FROM business_profiles WHERE user_id = ?", (user_id,)).fetchall(),
            [*store.PROFILE_FIELDS, "updated_at"]),
        "quotes.csv": _csv(
            db.execute("SELECT * FROM quotes WHERE user_id = ? ORDER BY id", (user_id,)).fetchall(),
            ["id", "created_at", "updated_at", "status", "customer_request", "price", "duration", "services",
             "reply_text", "result_json"]),
        "usage.csv": _csv(
            db.execute("SELECT * FROM usage WHERE user_id = ? ORDER BY month", (user_id,)).fetchall(),
            ["month", "count"]),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


def delete_account_data(user_id):
    """Delete every row that belongs to this account (explicitly, as well as ON DELETE CASCADE)."""
    db = store.get_db()
    for table in ("quotes", "usage", "business_profiles", "events"):
        db.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()


# ---------- routes ----------

def register(app, login_required, limiter, max_password_chars):

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/account/export")
    @login_required
    def export_data():
        data = export_zip(g.user["id"])
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return Response(data, mimetype="application/zip", headers={
            "Content-Disposition": f'attachment; filename="quoteflow-export-{stamp}.zip"'})

    @app.route("/account/delete", methods=["GET", "POST"])
    @login_required
    def delete_account():
        db = store.get_db()
        quotes = db.execute("SELECT COUNT(*) FROM quotes WHERE user_id = ?", (g.user["id"],)).fetchone()[0]
        stripe_active = billing.has_stripe_pro(g.user)
        if request.method == "POST":
            confirm = request.form.get("confirm", "").strip()
            password = request.form.get("password", "")[:max_password_chars]
            key = (g.user["email"], request.remote_addr)
            error = None
            if confirm != "DELETE" and confirm.lower() != g.user["email"]:
                error = "Type DELETE (or your email) to confirm."
            elif not limiter("login").hit(key):
                error = "Too many wrong passwords. Please wait 15 minutes and try again."
            else:
                row = db.execute("SELECT password_hash FROM users WHERE id = ?", (g.user["id"],)).fetchone()
                if not check_password_hash(row["password_hash"], password):
                    error = "That password isn't right. Your account has not been deleted."
            if error is None:
                limiter("login").clear(key)
                delete_account_data(g.user["id"])
                session.clear()
                flash("Your account and all its data have been deleted.")
                return redirect(url_for("landing"))
            flash(error, "error")
            return render_template("delete_account.html", quotes=quotes, stripe_active=stripe_active), 400
        return render_template("delete_account.html", quotes=quotes, stripe_active=stripe_active)

    @app.route("/forgot-password", methods=["GET", "POST"])
    def forgot_password():
        if not mail_on():
            abort(404)
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()[:254]
            allowed = (limiter("reset_ip").hit(request.remote_addr)
                       and limiter("reset_email").hit(email))
            if allowed and email:
                user = store.get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
                if user is not None:
                    link = url_for("reset_password", token=make_reset_token(user), _external=True)
                    mailer.send_mail(
                        current_app.config["MAIL"], user["email"], "Reset your QuoteFlow AI password",
                        "Someone asked to reset the password for your QuoteFlow AI account.\n\n"
                        f"To choose a new password, open this link (it works for 1 hour):\n{link}\n\n"
                        "If this wasn't you, you can ignore this email. Your password stays the same.\n")
            # Always the same answer, so nobody can find out which emails have an account.
            flash(RESET_SENT_MESSAGE)
            return redirect(url_for("login"))
        return render_template("forgot_password.html")

    @app.route("/reset-password/<token>", methods=["GET", "POST"])
    def reset_password(token):
        if not mail_on():
            abort(404)
        user = user_from_reset_token(token)
        if user is None:
            flash("This reset link has expired or was already used. Please ask for a new one.", "error")
            return redirect(url_for("forgot_password"))
        if request.method == "POST":
            new = request.form.get("new_password", "")
            if not 8 <= len(new) <= max_password_chars:
                flash(f"New password must be 8–{max_password_chars} characters.", "error")
                return render_template("reset_password.html", token=token), 400
            db = store.get_db()
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(new), user["id"]))
            db.commit()
            session.clear()
            flash("Your password was changed. You can log in now.")
            return redirect(url_for("login"))
        return render_template("reset_password.html", token=token)
