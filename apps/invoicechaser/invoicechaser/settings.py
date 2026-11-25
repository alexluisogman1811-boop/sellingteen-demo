import csv
import io
import zipfile
from datetime import date

from flask import Blueprint, Response, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .auth import start_session, validate_password
from .db import get_db
from .util import CURRENCIES, login_required, timezones, valid_timezone

bp = Blueprint("settings", __name__, url_prefix="/settings")


def render(errors=None, form=None, status=200):
    return render_template(
        "settings.html", errors=errors or {}, form=form or dict(g.user),
        currencies=CURRENCIES, timezones=timezones(),
    ), status


@bp.get("/")
@login_required
def index():
    return render()


@bp.post("/profile")
@login_required
def profile():
    f = request.form
    data = {
        "business_name": " ".join(f.get("business_name", "").split()),
        "sender_name": " ".join(f.get("sender_name", "").split()),
        "currency": f.get("currency", ""),
        "timezone": f.get("timezone", ""),
    }
    errors = {}
    if not data["business_name"] or len(data["business_name"]) > 120:
        errors["business_name"] = "Enter your business name (max 120 characters)."
    if len(data["sender_name"]) > 120:
        errors["sender_name"] = "Max 120 characters."
    if data["currency"] not in CURRENCIES:
        errors["currency"] = "Choose a currency."
    if not valid_timezone(data["timezone"]):
        errors["timezone"] = "Choose a timezone."
    if errors:
        return render(errors, {**dict(g.user), **data}, 400)
    db = get_db()
    db.execute(
        "UPDATE users SET business_name = ?, sender_name = ?, currency = ?, timezone = ? WHERE id = ?",
        (data["business_name"], data["sender_name"], data["currency"], data["timezone"], g.user["id"]),
    )
    db.commit()
    flash("Settings saved.", "success")
    return redirect(url_for("settings.index"))


@bp.post("/password")
@login_required
def password():
    current = request.form.get("current_password", "")
    new = request.form.get("new_password", "")
    errors = {}
    if not check_password_hash(g.user["password_hash"], current):
        errors["current_password"] = "Your current password isn't correct."
    elif pw_error := validate_password(new):
        errors["new_password"] = pw_error
    if errors:
        return render(errors, status=400)
    db = get_db()
    db.execute(
        "UPDATE users SET password_hash = ?, session_version = session_version + 1 WHERE id = ?",
        (generate_password_hash(new), g.user["id"]),
    )
    db.commit()
    # Other devices are now signed out; keep this one signed in.
    start_session(db.execute("SELECT * FROM users WHERE id = ?", (g.user["id"],)).fetchone())
    flash("Password changed. Any other devices have been signed out.", "success")
    return redirect(url_for("settings.index"))


@bp.post("/delete")
@login_required
def delete_account():
    typed = request.form.get("confirm_text", "").strip()
    if typed != "DELETE" and typed.lower() != g.user["email"].lower():
        return render({"confirm_text": "Type DELETE (or your email) to confirm."}, status=400)
    if not check_password_hash(g.user["password_hash"], request.form.get("confirm_password", "")):
        return render({"confirm_password": "Enter your password to confirm."}, status=400)
    uid = g.user["id"]
    db = get_db()
    with db:  # one transaction: all or nothing
        for table in ("drafts", "followups", "invoices", "customers"):
            db.execute(f"DELETE FROM {table} WHERE user_id = ?", (uid,))
        db.execute("DELETE FROM users WHERE id = ?", (uid,))
    session.clear()
    flash("Your account and all of its data have been deleted.", "info")
    return redirect(url_for("dashboard.home"))


def _csv_bytes(rows, columns):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(columns)
    for r in rows:
        writer.writerow([r[c] for c in columns])
    return out.getvalue().encode("utf-8-sig")  # BOM so Excel opens it correctly


EXPORT_TABLES = {
    "customers.csv": ("customers", ["id", "name", "contact_name", "email", "phone", "created_at"]),
    "invoices.csv": ("invoices", None),
    "followups.csv": ("followups", None),
    "drafts.csv": ("drafts", None),
}


@bp.get("/export")
@login_required
def export_data():
    """Download everything this account has stored, as a ZIP of CSV files (never the password)."""
    db = get_db()
    uid = g.user["id"]
    account_cols = ["email", "business_name", "sender_name", "currency", "timezone", "created_at", "paid_until"]
    files = {"account.csv": _csv_bytes(
        db.execute(f"SELECT {', '.join(account_cols)} FROM users WHERE id = ?", (uid,)).fetchall(), account_cols)}
    for filename, (table, cols) in EXPORT_TABLES.items():
        if cols is None:
            cols = [c["name"] for c in db.execute(f"PRAGMA table_info({table})").fetchall() if c["name"] != "user_id"]
        rows = db.execute(f"SELECT {', '.join(cols)} FROM {table} WHERE user_id = ? ORDER BY rowid", (uid,)).fetchall()
        files[filename] = _csv_bytes(rows, cols)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    filename = f"invoicechaser-export-{date.today().isoformat()}.zip"
    return Response(buf.getvalue(), mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
