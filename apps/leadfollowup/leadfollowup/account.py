"""Account settings: change password, export all data, and delete account (with a confirmation step)."""
import csv
import io
import zipfile
from datetime import datetime, timezone

from flask import Blueprint, Response, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .auth import MAX_PASSWORD, MIN_PASSWORD
from .db import get_db
from .security import login_required

bp = Blueprint("account", __name__)


def _password_ok(password: str) -> bool:
    row = get_db().execute("SELECT password_hash FROM users WHERE id = ?", (g.user["id"],)).fetchone()
    return row is not None and check_password_hash(row["password_hash"], password[:MAX_PASSWORD])


def _lead_count() -> int:
    return get_db().execute("SELECT COUNT(*) FROM leads WHERE user_id = ?", (g.user["id"],)).fetchone()[0]


@bp.route("/account")
@login_required
def settings():
    return render_template("account/settings.html", errors={})


@bp.route("/account/password", methods=["POST"])
@login_required
def change_password():
    current = request.form.get("current_password", "")
    new = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")
    errors = {}
    if not _password_ok(current):
        errors["current_password"] = "Your current password is incorrect."
    if len(new) < MIN_PASSWORD:
        errors["new_password"] = f"New password must be at least {MIN_PASSWORD} characters."
    elif len(new) > MAX_PASSWORD:
        errors["new_password"] = f"New password must be {MAX_PASSWORD} characters or fewer."
    elif new == current:
        errors["new_password"] = "Choose a password that's different from your current one."
    elif new != confirm:
        errors["confirm_password"] = "New passwords don't match."
    if errors:
        return render_template("account/settings.html", errors=errors), 400

    db = get_db()
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(new), g.user["id"]))
    db.commit()
    user_id = g.user["id"]
    session.clear()  # fresh session after a credential change
    session["user_id"] = user_id
    session.permanent = True
    flash("Your password has been changed.", "success")
    return redirect(url_for("account.settings"))


@bp.route("/account/delete", methods=["GET", "POST"])
@login_required
def delete():
    """Step 1 (GET) explains what will be deleted; step 2 (POST) needs the password again."""
    if request.method == "GET":
        return render_template("account/delete.html", lead_count=_lead_count(), error=None)

    typed = request.form.get("confirm", "").strip().lower()
    confirmed = typed in ("delete", (g.user["email"] or "").strip().lower())
    if not confirmed or not _password_ok(request.form.get("password", "")):
        error = ("Type DELETE (or your email) in the box to confirm." if not confirmed
                 else "That password is incorrect. Your account has not been deleted.")
        return render_template("account/delete.html", lead_count=_lead_count(), error=error), 400

    db = get_db()
    # Leads and their contact history are removed too (ON DELETE CASCADE).
    db.execute("DELETE FROM users WHERE id = ?", (g.user["id"],))
    db.commit()
    session.clear()
    flash("Your account and all its leads have been permanently deleted.", "info")
    return redirect(url_for("leads.dashboard"))


def _cell(value):
    """Stop spreadsheet apps from running cell text as a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _csv(header, rows) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    for row in rows:
        writer.writerow([_cell(row[col]) for col in header])
    return out.getvalue()


@bp.route("/account/export", methods=["POST"])
@login_required
def export():
    """Download everything this account has stored, as a ZIP of CSV files. Works after the trial too."""
    db = get_db()
    uid = g.user["id"]
    account = db.execute("SELECT business_name, email, created_at, paid_until FROM users WHERE id = ?",
                         (uid,)).fetchall()
    leads = db.execute("SELECT id, name, phone, email, source, service, notes, status, next_follow_up, "
                       "created_at, updated_at FROM leads WHERE user_id = ? ORDER BY id", (uid,)).fetchall()
    notes = db.execute("SELECT n.id, n.lead_id, l.name AS lead_name, n.method, n.contact_date, n.body, n.created_at "
                       "FROM lead_notes n JOIN leads l ON l.id = n.lead_id WHERE l.user_id = ? ORDER BY n.id",
                       (uid,)).fetchall()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("account.csv", _csv(["business_name", "email", "created_at", "paid_until"], account))
        zf.writestr("leads.csv", _csv(["id", "name", "phone", "email", "source", "service", "notes", "status",
                                       "next_follow_up", "created_at", "updated_at"], leads))
        zf.writestr("contact_history.csv", _csv(["id", "lead_id", "lead_name", "method", "contact_date", "body",
                                                 "created_at"], notes))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return Response(buf.getvalue(), mimetype="application/zip", headers={
        "Content-Disposition": f'attachment; filename="leadfollowup-export-{stamp}.zip"',
        "Cache-Control": "no-store",
    })
