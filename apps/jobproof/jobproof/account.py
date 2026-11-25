"""Account page: change password, export all my data, delete my account.

All of these stay reachable after the free trial ends (see billing.ALWAYS_OPEN).
"""
import csv
import io
import zipfile
from datetime import date

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, send_file, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from . import images
from .auth import MIN_PASSWORD, _clear_failures, _record_failure, _too_many_failures
from .db import get_db
from .security import end_session, login_required, start_session

bp = Blueprint("account", __name__, url_prefix="/account")


def _counts(business_id):
    db = get_db()
    return {
        "jobs": db.execute("SELECT COUNT(*) FROM jobs WHERE business_id = ?", (business_id,)).fetchone()[0],
        "photos": db.execute("SELECT COUNT(*) FROM photos p JOIN jobs j ON j.id = p.job_id "
                             "WHERE j.business_id = ?", (business_id,)).fetchone()[0],
        "reports": db.execute("SELECT COUNT(*) FROM reports r JOIN jobs j ON j.id = r.job_id "
                              "WHERE j.business_id = ?", (business_id,)).fetchone()[0],
    }


def _password_ok(password):
    """Check the signed-in user's password, with the same wrong-password limit as the login page."""
    email = g.user["email"]
    if _too_many_failures(email):
        return None
    row = get_db().execute("SELECT password_hash FROM users WHERE id = ?", (g.user["id"],)).fetchone()
    if row and check_password_hash(row["password_hash"], password or ""):
        _clear_failures(email)
        return True
    _record_failure(email)
    return False


@bp.route("")
@login_required
def settings():
    return render_template("account/settings.html", counts=_counts(g.business["id"]))


@bp.route("/password", methods=["POST"])
@login_required
def password():
    current = request.form.get("current_password", "")
    new = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")
    errors = []
    ok = _password_ok(current)
    if ok is None:
        errors.append("Too many wrong passwords. Please wait 15 minutes and try again.")
    elif not ok:
        errors.append("Your current password isn't right.")
    if len(new) < MIN_PASSWORD:
        errors.append(f"New password must be at least {MIN_PASSWORD} characters.")
    elif len(new) > 128:
        errors.append("New password must be 128 characters or fewer.")
    if new != confirm:
        errors.append("The new passwords do not match.")
    if errors:
        for e in errors:
            flash(e, "error")
        return render_template("account/settings.html", counts=_counts(g.business["id"])), 400
    db = get_db()
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(new), g.user["id"]))
    # Sign out every other device, then start a fresh session here.
    db.execute("DELETE FROM sessions WHERE user_id = ?", (g.user["id"],))
    db.commit()
    start_session(g.user["id"])
    flash("Your password has been changed.", "success")
    return redirect(url_for("account.settings"))


def _safe_cell(value):
    # Stop spreadsheet formulas from running when the CSV is opened in Excel.
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _csv(rows, columns):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(columns)
    for r in rows:
        writer.writerow([_safe_cell(r[c]) for c in columns])
    return out.getvalue()


@bp.route("/export")
@login_required
def export():
    """A ZIP of CSV files with everything stored for this account (never other accounts)."""
    db = get_db()
    uid, bid = g.user["id"], g.business["id"]
    tables = {
        "account.csv": (db.execute("SELECT email, created_at, paid_until FROM users WHERE id = ?", (uid,)),
                        ["email", "created_at", "paid_until"]),
        "business.csv": (db.execute("SELECT name, contact_name, phone, email, address, logo_file, updated_at "
                                    "FROM businesses WHERE id = ?", (bid,)),
                         ["name", "contact_name", "phone", "email", "address", "logo_file", "updated_at"]),
        "jobs.csv": (db.execute("SELECT * FROM jobs WHERE business_id = ? ORDER BY id", (bid,)),
                     ["id", "customer_name", "customer_phone", "customer_email", "title", "address", "service_type",
                      "job_date", "technician", "notes", "materials", "status", "created_at", "updated_at"]),
        "photos.csv": (db.execute("SELECT p.* FROM photos p JOIN jobs j ON j.id = p.job_id "
                                  "WHERE j.business_id = ? ORDER BY p.job_id, p.position", (bid,)),
                       ["job_id", "file_key", "ext", "stage", "position", "width", "height", "size_bytes",
                        "created_at"]),
        "reports.csv": (db.execute("SELECT r.* FROM reports r JOIN jobs j ON j.id = r.job_id "
                                   "WHERE j.business_id = ? ORDER BY r.job_id", (bid,)),
                        ["job_id", "content_json", "provider", "generation_count", "generated_at"]),
        "share_links.csv": (db.execute("SELECT r.job_id, s.created_at, s.revoked_at FROM share_links s "
                                       "JOIN reports r ON r.id = s.report_id JOIN jobs j ON j.id = r.job_id "
                                       "WHERE j.business_id = ? ORDER BY s.id", (bid,)),
                            ["job_id", "created_at", "revoked_at"]),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, (cursor, columns) in tables.items():
            zf.writestr(name, _csv(cursor.fetchall(), columns))
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"jobproof-export-{date.today().isoformat()}.zip", max_age=0)


@bp.route("/delete", methods=["GET", "POST"])
@login_required
def delete():
    counts = _counts(g.business["id"])
    if request.method == "GET":
        return render_template("account/delete.html", counts=counts)
    email = g.user["email"]
    typed = request.form.get("confirm", "").strip()
    error = None
    if typed != "DELETE" and typed.lower() != email.lower():
        error = "Type DELETE (or your email address) to confirm."
    else:
        ok = _password_ok(request.form.get("password", ""))
        if ok is None:
            error = "Too many wrong passwords. Please wait 15 minutes and try again."
        elif not ok:
            error = "That password isn't right. Your account has not been deleted."
    if error:
        flash(error, "error")
        return render_template("account/delete.html", counts=counts), 400

    db = get_db()
    uid, bid = g.user["id"], g.business["id"]
    photos = db.execute("SELECT p.file_key, p.ext FROM photos p JOIN jobs j ON j.id = p.job_id "
                        "WHERE j.business_id = ?", (bid,)).fetchall()
    logo = g.business["logo_file"]
    # Explicit deletes as well as ON DELETE CASCADE, in case foreign keys are off on an old database.
    job_ids = "SELECT id FROM jobs WHERE business_id = ?"
    db.execute(f"DELETE FROM share_links WHERE report_id IN (SELECT id FROM reports WHERE job_id IN ({job_ids}))",
               (bid,))
    db.execute(f"DELETE FROM reports WHERE job_id IN ({job_ids})", (bid,))
    db.execute(f"DELETE FROM photos WHERE job_id IN ({job_ids})", (bid,))
    db.execute("DELETE FROM jobs WHERE business_id = ?", (bid,))
    db.execute("DELETE FROM businesses WHERE id = ?", (bid,))
    db.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
    db.execute("DELETE FROM users WHERE id = ?", (uid,))
    db.commit()
    # Files go only after the database delete succeeded.
    root = current_app.config["UPLOAD_FOLDER"]
    for p in photos:
        images.remove_file(root, "photos", p["file_key"], p["ext"])
    if logo and "." in logo:
        key, ext = logo.split(".", 1)
        images.remove_file(root, "logos", key, ext)
    end_session()
    flash("Your account and all its data have been deleted.", "success")
    return redirect(url_for("dashboard.landing"))
