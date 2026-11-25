"""Account page: change password, export all data, and delete the account (with a confirmation step)."""
import csv
import io
import zipfile
from datetime import date

from flask import (Blueprint, Response, flash, g, redirect, render_template, request, session,
                   url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from .auth import (MAX_PASSWORD, MIN_PASSWORD, locked_out, login_required, record_failure,
                   start_session)
from .db import get_db

bp = Blueprint("account", __name__)

LOCKED = "Too many wrong passwords. Please wait 5 minutes and try again."


def _password_ok(password):
    """Checks the logged-in owner's password, sharing the login lockout."""
    email = g.user["email"]
    if locked_out(email):
        return False, LOCKED
    row = get_db().execute("SELECT password_hash FROM users WHERE id = ?", (g.user["id"],)).fetchone()
    if row and check_password_hash(row["password_hash"], password[:MAX_PASSWORD]):
        return True, None
    record_failure(email)
    return False, "That isn't your current password."


def _counts():
    db, uid = get_db(), g.user["id"]
    return {
        "customers": db.execute("SELECT COUNT(*) FROM customers WHERE user_id = ?", (uid,)).fetchone()[0],
        "invoices": db.execute("SELECT COUNT(*) FROM invoices WHERE user_id = ?", (uid,)).fetchone()[0],
    }


@bp.route("/account")
@login_required
def settings():
    return render_template("account.html", errors={})


@bp.route("/account/password", methods=["POST"])
@login_required
def change_password():
    current = request.form.get("current_password", "")
    new = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")
    errors = {}
    ok, problem = _password_ok(current)
    if not ok:
        errors["current_password"] = problem
    if not MIN_PASSWORD <= len(new) <= MAX_PASSWORD:
        errors["new_password"] = f"Use at least {MIN_PASSWORD} characters."
    elif new != confirm:
        errors["confirm_password"] = "The two new passwords don't match."
    elif ok and new == current:
        errors["new_password"] = "Choose a password that's different from your current one."
    if errors:
        return render_template("account.html", errors=errors), 400

    password_hash = generate_password_hash(new)
    db = get_db()
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, g.user["id"]))
    db.commit()
    start_session(g.user["id"], password_hash)  # this browser stays in; every other one is logged out
    flash("Password changed. Any other browsers you were logged in on have been logged out.", "success")
    return redirect(url_for("account.settings"))


@bp.route("/account/delete", methods=["GET", "POST"])
@login_required
def delete():
    """GET shows what will be deleted and asks for the password; POST does it."""
    error = None
    if request.method == "POST":
        typed = request.form.get("confirm_text", "").strip()
        if request.form.get("confirm") != "yes":
            error = "Tick the box to confirm you want everything deleted."
        elif typed != "DELETE" and typed.lower() != g.user["email"].lower():
            error = "Type the word DELETE (or your email) to confirm."
        else:
            ok, error = _password_ok(request.form.get("password", ""))
            if ok:
                db = get_db()
                # Customers, invoices and follow-ups are removed with the user (ON DELETE CASCADE).
                db.execute("DELETE FROM users WHERE id = ?", (g.user["id"],))
                db.commit()
                session.clear()
                flash("Your account and all of its data have been deleted.", "success")
                return redirect(url_for("routes.index"))
    return render_template("account_delete.html", counts=_counts(), error=error), (400 if error else 200)


# Tables in the export, with the columns to include. Password hashes are never exported.
EXPORT_TABLES = (
    ("customers", "SELECT id, name, email, phone, company, created_at FROM customers WHERE user_id = ? ORDER BY id"),
    ("invoices", "SELECT id, customer_id, number, amount_cents, issue_date, due_date, notes, status, sent_on, "
                 "paid_on, created_at FROM invoices WHERE user_id = ? ORDER BY id"),
    ("followups", "SELECT id, invoice_id, stage, action, message, on_date, created_at FROM followups "
                  "WHERE user_id = ? ORDER BY id"),
)


def _csv(cursor):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([d[0] for d in cursor.description])
    for row in cursor.fetchall():
        # Stop spreadsheet apps from running text that starts like a formula.
        writer.writerow(["'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else v
                         for v in row])
    return buf.getvalue()


@bp.route("/account/export")
@login_required
def export():
    """Download everything in this account as a ZIP of CSV files."""
    db, uid = get_db(), g.user["id"]
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("account.csv", _csv(db.execute(
            "SELECT email, business_name, created_at, paid_until FROM users WHERE id = ?", (uid,))))
        for name, sql in EXPORT_TABLES:
            zf.writestr(f"{name}.csv", _csv(db.execute(sql, (uid,))))
    filename = f"payrecover-export-{date.today().isoformat()}.zip"
    return Response(out.getvalue(), mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
