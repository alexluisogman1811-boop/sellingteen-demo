"""Account page: change password; Owner: export all company data, permanently delete the company."""
import csv
import io
import shutil
import tempfile
import zipfile
from pathlib import Path

from flask import (Blueprint, current_app, flash, g, redirect, render_template, request, send_file,
                   url_for)
from werkzeug.security import check_password_hash

from ..db import get_db
from ..passwords import set_password
from ..security import (audit, clear_login_failures, login_blocked, login_required, login_user, logout_user,
                        record_login_failure, roles_required)
from ..util import now_iso
from ..validation import Form

bp = Blueprint("account", __name__, url_prefix="/account")


@bp.route("/")
@login_required
def overview():
    return render_template("account/overview.html")


def _check_current_password(form, field="current_password"):
    """Validates the current password with the same throttling as login. Returns True when correct."""
    email = g.user["email"]
    if login_blocked(email):
        form.errors[field] = "Too many wrong attempts. Wait 15 minutes and try again."
        return False
    if not check_password_hash(g.user["password_hash"] or "", request.form.get(field) or ""):
        record_login_failure(email)
        form.errors[field] = "Your current password is incorrect."
        return False
    clear_login_failures(email)
    return True


@bp.route("/password", methods=["GET", "POST"])
@login_required
def change_password():
    form = Form(request.form)
    if request.method == "POST":
        new = form.password("password", "New password")
        if new != (request.form.get("password_confirm") or ""):
            form.errors["password_confirm"] = "New passwords don't match."
        if form.ok and _check_current_password(form):
            db = get_db()
            set_password(g.user, new)  # ends every other session of this user
            audit(g.company["id"], "user.password_changed")
            db.commit()
            login_user(db.execute("SELECT * FROM users WHERE id = ?", (g.user["id"],)).fetchone())
            flash("Password changed. You were signed out on your other devices.", "success")
            return redirect(url_for("account.overview"))
    return render_template("account/password.html", form=form), (422 if form.errors else 200)


# --- export ------------------------------------------------------------------

# (table, SQL selecting only this company's rows, columns never exported)
EXPORT_TABLES = [
    ("companies", "SELECT * FROM companies WHERE id = :cid", ()),
    ("users", "SELECT * FROM users WHERE company_id = :cid", ("password_hash", "session_epoch")),
    ("invites", "SELECT * FROM invites WHERE company_id = :cid", ("token_hash",)),
    ("password_resets", "SELECT * FROM password_resets WHERE company_id = :cid", ("token_hash",)),
    ("labor_rates", "SELECT * FROM labor_rates WHERE company_id = :cid", ()),
    ("customers", "SELECT * FROM customers WHERE company_id = :cid", ()),
    ("customer_contacts", "SELECT * FROM customer_contacts WHERE company_id = :cid", ()),
    ("jobs", "SELECT * FROM jobs WHERE company_id = :cid", ()),
    ("job_members", "SELECT m.* FROM job_members m JOIN jobs j ON j.id = m.job_id WHERE j.company_id = :cid", ()),
    ("tickets", "SELECT * FROM tickets WHERE company_id = :cid", ()),
    ("ticket_lines", "SELECT l.* FROM ticket_lines l JOIN tickets t ON t.id = l.ticket_id WHERE t.company_id = :cid", ()),
    ("ticket_versions", "SELECT v.* FROM ticket_versions v JOIN tickets t ON t.id = v.ticket_id WHERE t.company_id = :cid", ()),
    ("attachments", "SELECT * FROM attachments WHERE company_id = :cid", ()),
    ("signature_requests", "SELECT * FROM signature_requests WHERE company_id = :cid", ("token_hash",)),
    ("signatures", "SELECT * FROM signatures WHERE company_id = :cid", ("signature_png",)),
    ("notices", "SELECT * FROM notices WHERE company_id = :cid", ()),
    ("invoices", "SELECT * FROM invoices WHERE company_id = :cid", ()),
    ("audit_events", "SELECT * FROM audit_events WHERE company_id = :cid ORDER BY id", ()),
]

EXPORT_README = """SignedExtra data export
=======================
Company: {name}
Exported: {when} (UTC)

data/<table>.csv   every record of this company, one file per database table
photos/            original uploaded photos (file names match attachments.csv stored_name)
signatures/        signature images, signature-<id>.png (id matches signatures.csv)

Not included: password hashes, one-time link tokens.
Content fingerprints (SHA-256) are in tickets.csv, ticket_versions.csv, attachments.csv,
signatures.csv and notices.csv so you can check files haven't changed.
"""


def _csv_text(rows, skip):
    buf = io.StringIO()
    writer = csv.writer(buf)
    if rows:
        cols = [c for c in rows[0].keys() if c not in skip]
        writer.writerow(cols)
        for r in rows:
            writer.writerow(["" if r[c] is None else r[c] for c in cols])
    return buf.getvalue()


@bp.route("/export.zip")
@roles_required("owner")
def export_company():
    db = get_db()
    cid = g.company["id"]
    tmp = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", EXPORT_README.format(name=g.company["name"], when=now_iso()))
        for table, sql, skip in EXPORT_TABLES:
            z.writestr(f"data/{table}.csv", _csv_text(db.execute(sql, {"cid": cid}).fetchall(), skip))
        folder = Path(current_app.config["UPLOAD_DIR"]) / str(cid)
        for a in db.execute("SELECT stored_name FROM attachments WHERE company_id = ?", (cid,)):
            path = folder / a["stored_name"]
            if path.is_file():
                z.write(path, f"photos/{a['stored_name']}")
        for s in db.execute("SELECT id, signature_png FROM signatures WHERE company_id = ? AND signature_png IS NOT NULL",
                            (cid,)):
            z.writestr(f"signatures/signature-{s['id']}.png", s["signature_png"])
    audit(cid, "company.exported")
    db.commit()
    tmp.seek(0)
    return send_file(tmp, mimetype="application/zip", as_attachment=True,
                     download_name=f"signedextra-export-{cid}.zip")


# --- delete ------------------------------------------------------------------

@bp.route("/delete-company", methods=["GET", "POST"])
@roles_required("owner")
def delete_company():
    db = get_db()
    cid = g.company["id"]
    form = Form(request.form)
    counts = {t: db.execute(f"SELECT COUNT(*) FROM {t} WHERE company_id = ?", (cid,)).fetchone()[0]
              for t in ("users", "tickets", "attachments", "signatures", "notices", "invoices")}
    if request.method == "POST":
        typed = (request.form.get("confirm_name") or "").strip()
        # Accepted: the word DELETE, your login email, or the company name exactly as shown.
        if typed != "DELETE" and typed.lower() != (g.user["email"] or "").lower() and typed != g.company["name"]:
            form.errors["confirm_name"] = "Type DELETE (or your email) exactly as shown to confirm."
        if not form.errors and _check_current_password(form):
            folder = Path(current_app.config["UPLOAD_DIR"]) / str(cid)
            # One statement: ON DELETE CASCADE removes users, tickets, photos records, signatures, notices,
            # invoices and everything else that belongs to the company.
            db.execute("DELETE FROM companies WHERE id = ?", (cid,))
            # The audit log is append-only and keeps its rows. Record who deleted the company and when.
            audit(cid, "company.deleted", details=counts, actor=g.user["name"], user_id=g.user["id"])
            db.commit()
            shutil.rmtree(folder, ignore_errors=True)
            logout_user()
            flash("Your company and its data were permanently deleted.", "success")
            return redirect(url_for("core.dashboard"))
    return render_template("account/delete.html", form=form, counts=counts), (422 if form.errors else 200)
