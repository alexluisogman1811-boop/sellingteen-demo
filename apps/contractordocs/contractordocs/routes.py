import csv
import functools
import hashlib
import io
import os
import re
import time
import uuid
import zipfile
from datetime import date, datetime

from flask import (Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template,
                   request, send_file, session, url_for)
from itsdangerous import BadSignature, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

from . import mailer
from . import reminders as reminders_mod
from .db import get_db
from .extraction import extract
from .status import (DEFAULT_REQUIRED_TYPES, DOC_TYPES, EXPIRED, EXPIRING, LABELS, MISSING,
                     REVIEW, VALID, days_left, document_status, requirement_slots)

bp = Blueprint("main", __name__)

# Signature bytes -> (mime type, extension). Uploads are checked by content, not filename.
SIGNATURES = [
    (b"%PDF-", "application/pdf", "pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
]
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def today():
    return current_app.config.get("TODAY") or date.today()


def sniff(head):
    for sig, mime, ext in SIGNATURES:
        if head.startswith(sig):
            return mime, ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None, None


@bp.before_app_request
def load_user():
    uid = session.get("user_id")
    g.user = None
    if uid is not None:
        g.user = get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        # Cookies are signed but stateless; the version check lets logout revoke copies of them.
        if g.user is None or session.get("sv") != g.user["session_version"]:
            g.user = None
            session.clear()


def start_session(user_id, version):
    session.clear()
    session.permanent = True
    session["user_id"] = user_id
    session["sv"] = version


@bp.app_context_processor
def inject():
    count = 0
    if g.get("user"):
        count = len(reminders_mod.active(get_db(), g.user["id"]))
    return {"LABELS": LABELS, "DOC_TYPES": DOC_TYPES, "reminder_count": count}


def login_required(view):
    @functools.wraps(view)
    def wrapped(**kwargs):
        if g.user is None:
            # script_root keeps the address prefix when the app runs under one (e.g. /contractordocs)
            # full_path keeps the query string; Werkzeug adds a bare "?" when there is none.
            return redirect(url_for("main.login", next=(request.script_root + request.full_path).rstrip("?")))
        return view(**kwargs)
    return wrapped


def required_types():
    return [t for t in g.user["required_types"].split("|") if t]


def own_sub(sub_id):
    sub = get_db().execute("SELECT * FROM subcontractors WHERE id = ? AND user_id = ?",
                           (sub_id, g.user["id"])).fetchone()
    if sub is None:
        abort(404)
    return sub


def own_doc(doc_id):
    doc = get_db().execute(
        "SELECT d.*, s.name AS subcontractor_name FROM documents d "
        "JOIN subcontractors s ON s.id = d.subcontractor_id WHERE d.id = ? AND d.user_id = ?",
        (doc_id, g.user["id"])).fetchone()
    if doc is None:
        abort(404)
    return doc


# ---------- auth ----------

@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        company = request.form.get("company_name", "").strip()
        error = None
        if not EMAIL_RE.match(email):
            error = "Enter a valid email address."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        elif get_db().execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            error = "An account with that email already exists."
        if error:
            flash(error, "error")
        else:
            db = get_db()
            cur = db.execute(
                "INSERT INTO users (email, password_hash, company_name, required_types) VALUES (?, ?, ?, ?)",
                (email, generate_password_hash(password), company, "|".join(DEFAULT_REQUIRED_TYPES)))
            db.commit()
            start_session(cur.lastrowid, 0)
            return redirect(url_for("main.dashboard"))
    return render_template("auth.html", mode="signup")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if user is None or not check_password_hash(user["password_hash"], request.form.get("password", "")):
            flash("Incorrect email or password.", "error")
        else:
            start_session(user["id"], user["session_version"])
            nxt = request.args.get("next", "")
            return redirect(nxt if _safe_next(nxt) else url_for("main.dashboard"))
    return render_template("auth.html", mode="login", next=request.args.get("next", ""),
                           mail_on=mailer.mail_enabled(current_app.config.get("MAIL")))


def _safe_next(nxt):
    """Only local paths inside this app (e.g. /contractordocs/documents) are allowed as a redirect target."""
    if not nxt or not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt:
        return False
    if any(ord(ch) < 32 for ch in nxt):
        return False
    root = request.script_root
    return not root or nxt == root or nxt.startswith(root + "/")


@bp.route("/logout", methods=["POST"])
def logout():
    if g.user is not None:
        # Invalidates every copy of this user's session cookie (signs out all devices).
        db = get_db()
        db.execute("UPDATE users SET session_version = session_version + 1 WHERE id = ?", (g.user["id"],))
        db.commit()
    session.clear()
    return redirect(url_for("main.login"))


# ---------- dashboard ----------

def _user_slots():
    db = get_db()
    subs = db.execute("SELECT * FROM subcontractors WHERE user_id = ? ORDER BY name", (g.user["id"],)).fetchall()
    docs = db.execute("SELECT * FROM documents WHERE user_id = ?", (g.user["id"],)).fetchall()
    return subs, docs, requirement_slots(subs, docs, required_types(), today())


@bp.route("/")
def dashboard():
    if g.user is None:
        # Public home page for visitors who aren't logged in.
        return render_template("landing.html")
    reminders_mod.generate(get_db(), g.user["id"], today())
    subs, docs, slots = _user_slots()
    counts = {k: 0 for k in (VALID, EXPIRING, EXPIRED, MISSING, REVIEW)}
    for s in slots:
        counts[s["status"]] += 1
    selected = request.args.get("status", "")
    order = {EXPIRED: 0, MISSING: 1, REVIEW: 2, EXPIRING: 3, VALID: 4}
    if selected in counts:
        shown = [s for s in slots if s["status"] == selected]
    else:
        selected = ""
        shown = [s for s in slots if s["status"] != VALID]
    shown.sort(key=lambda s: (order[s["status"]], s["days_left"] if s["days_left"] is not None else 0,
                              s["subcontractor"]["name"]))
    return render_template("dashboard.html", counts=counts, slots=shown, selected=selected,
                           sub_count=len(subs), doc_count=len(docs),
                           reminders=reminders_mod.active(get_db(), g.user["id"]))


# ---------- subcontractors ----------

@bp.route("/subcontractors", methods=["GET", "POST"])
@login_required
def subcontractors():
    db = get_db()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Subcontractor name is required.", "error")
        else:
            cur = db.execute(
                "INSERT INTO subcontractors (user_id, name, trade, contact_email, phone) VALUES (?, ?, ?, ?, ?)",
                (g.user["id"], name, request.form.get("trade", "").strip(),
                 request.form.get("contact_email", "").strip(), request.form.get("phone", "").strip()))
            db.commit()
            flash(f"Added {name}.", "success")
            return redirect(url_for("main.subcontractor_detail", sub_id=cur.lastrowid))

    q = request.args.get("q", "").strip()
    subs, docs, slots = _user_slots()
    if q:
        ql = q.lower()
        subs = [s for s in subs if ql in s["name"].lower() or ql in s["trade"].lower()
                or ql in s["contact_email"].lower()]
    summary = {}
    for s in slots:
        summary.setdefault(s["subcontractor"]["id"], {}).setdefault(s["status"], 0)
        summary[s["subcontractor"]["id"]][s["status"]] += 1
    return render_template("subcontractors.html", subs=subs, q=q, summary=summary)


@bp.route("/subcontractors/<int:sub_id>")
@login_required
def subcontractor_detail(sub_id):
    sub = own_sub(sub_id)
    docs = get_db().execute("SELECT * FROM documents WHERE subcontractor_id = ? AND user_id = ? "
                            "ORDER BY uploaded_at DESC", (sub_id, g.user["id"])).fetchall()
    slots = requirement_slots([sub], docs, required_types(), today())
    rows = [{"doc": d, "status": document_status(d, today()), "days_left": days_left(d, today())} for d in docs]
    return render_template("subcontractor.html", sub=sub, slots=slots, rows=rows)


# ---------- documents ----------

@bp.route("/documents/upload", methods=["POST"])
@login_required
def upload():
    sub = own_sub(request.form.get("subcontractor_id", type=int) or 0)
    f = request.files.get("file")
    if f is None or not f.filename:
        flash("Choose a file to upload.", "error")
        return redirect(url_for("main.subcontractor_detail", sub_id=sub["id"]))
    head = f.stream.read(16)
    f.stream.seek(0)
    mime, ext = sniff(head)
    if mime is None:
        flash("Only PDF, PNG, JPEG or WEBP files are accepted.", "error")
        return redirect(url_for("main.subcontractor_detail", sub_id=sub["id"]))

    stored = f"{uuid.uuid4().hex}.{ext}"
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], stored)
    f.save(path)
    # Keep only the base name, and make its extension match the real content type, so a
    # download of e.g. "x.html" that is really a PDF can't be opened as a web page.
    base = re.sub(r"[\x00-\x1f]", "", os.path.basename(f.filename.replace("\\", "/"))).strip() or "document"
    stem = os.path.splitext(base)[0] or "document"
    original = f"{stem[:200]}.{ext}"
    result = extract(path, mime, original)

    name = request.form.get("name", "").strip() or stem[:120]
    db = get_db()
    cur = db.execute(
        """INSERT INTO documents (user_id, subcontractor_id, name, doc_type, original_filename, stored_filename,
               mime_type, uploaded_at, expiration_date, no_expiration, needs_review, review_reason,
               extraction_source, extraction_evidence)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (g.user["id"], sub["id"], name, result["doc_type"], original, stored, mime,
         datetime.now().isoformat(timespec="seconds"), result["expiration_date"], int(result["no_expiration"]),
         int(result["needs_review"]), result["review_reason"], result["source"], result["evidence"][:500]))
    db.commit()
    if result["needs_review"]:
        flash("Uploaded. Some details could not be read with confidence - please review below.", "warning")
    else:
        flash("Uploaded. Please check the extracted details below.", "success")
    return redirect(url_for("main.document_detail", doc_id=cur.lastrowid))


@bp.route("/documents")
@login_required
def documents():
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "")
    doc_type = request.args.get("type", "")
    sub_id = request.args.get("sub", type=int)
    sql = ("SELECT d.*, s.name AS subcontractor_name FROM documents d "
           "JOIN subcontractors s ON s.id = d.subcontractor_id WHERE d.user_id = ?")
    params = [g.user["id"]]
    if q:
        sql += " AND (d.name LIKE ? OR d.original_filename LIKE ? OR s.name LIKE ?)"
        like = f"%{q}%"
        params += [like, like, like]
    if doc_type:
        sql += " AND d.doc_type = ?"
        params.append(doc_type)
    if sub_id:
        sql += " AND d.subcontractor_id = ?"
        params.append(sub_id)
    sql += " ORDER BY d.uploaded_at DESC"
    rows = [{"doc": d, "status": document_status(d, today()), "days_left": days_left(d, today())}
            for d in get_db().execute(sql, params).fetchall()]
    if status:
        rows = [r for r in rows if r["status"] == status]
    subs = get_db().execute("SELECT id, name FROM subcontractors WHERE user_id = ? ORDER BY name",
                            (g.user["id"],)).fetchall()
    return render_template("documents.html", rows=rows, q=q, status=status, doc_type=doc_type,
                           sub_id=sub_id, subs=subs)


@bp.route("/documents/<int:doc_id>", methods=["GET", "POST"])
@login_required
def document_detail(doc_id):
    doc = own_doc(doc_id)
    if request.method == "POST":
        name = request.form.get("name", "").strip() or doc["name"]
        doc_type = request.form.get("doc_type") or None
        if doc_type and doc_type not in DOC_TYPES:
            abort(400)
        no_exp = request.form.get("no_expiration") == "on"
        exp_raw = request.form.get("expiration_date", "").strip()
        exp = None
        error = None
        if exp_raw and not no_exp:
            try:
                exp = date.fromisoformat(exp_raw).isoformat()
            except ValueError:
                error = "Expiration date must be a valid date."
        if not error and doc_type is None:
            error = "Choose a document type."
        if not error and exp is None and not no_exp:
            error = "Enter an expiration date, or tick 'This document does not expire'."
        if not error:
            db = get_db()
            db.execute("UPDATE documents SET name = ?, doc_type = ?, expiration_date = ?, no_expiration = ?, "
                       "needs_review = 0, review_reason = '' WHERE id = ? AND user_id = ?",
                       (name, doc_type, exp, int(no_exp), doc_id, g.user["id"]))
            db.commit()
            flash("Document details saved.", "success")
            return redirect(url_for("main.document_detail", doc_id=doc_id))
        flash(error, "error")
        # Re-show what the user entered so they don't have to fill it in again.
        form = dict(doc)
        form.update(name=name, doc_type=doc_type, expiration_date=exp_raw or None, no_expiration=int(no_exp))
    else:
        form = doc
    return render_template("document.html", doc=doc, form=form, status=document_status(doc, today()),
                           days_left=days_left(doc, today()))


@bp.route("/documents/<int:doc_id>/file")
@login_required
def document_file(doc_id):
    doc = own_doc(doc_id)
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], doc["stored_filename"])
    if not os.path.exists(path):
        abort(404)
    resp = send_file(path, mimetype=doc["mime_type"], download_name=doc["original_filename"])
    # Uploaded content is untrusted. nosniff (set globally) plus the sniffed Content-Type means it
    # can never be treated as HTML. Images also get a sandbox CSP; PDFs can't, because Chrome's PDF
    # viewer refuses to render in sandboxed documents.
    if doc["mime_type"].startswith("image/"):
        resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self'"
    return resp


@bp.route("/documents/<int:doc_id>/delete", methods=["POST"])
@login_required
def document_delete(doc_id):
    doc = own_doc(doc_id)
    db = get_db()
    db.execute("DELETE FROM documents WHERE id = ? AND user_id = ?", (doc_id, g.user["id"]))
    db.commit()
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], doc["stored_filename"])
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    flash(f"Deleted {doc['name']}.", "success")
    return redirect(url_for("main.subcontractor_detail", sub_id=doc["subcontractor_id"]))


# ---------- reminders & settings ----------

@bp.route("/reminders")
@login_required
def reminders():
    reminders_mod.generate(get_db(), g.user["id"], today())
    items = [{"r": r, "days_left": (date.fromisoformat(r["expiration_date"]) - today()).days}
             for r in reminders_mod.active(get_db(), g.user["id"])]
    return render_template("reminders.html", items=items)


@bp.route("/reminders/<int:rid>/dismiss", methods=["POST"])
@login_required
def reminder_dismiss(rid):
    db = get_db()
    db.execute("UPDATE reminders SET dismissed = 1 WHERE id = ? AND user_id = ?", (rid, g.user["id"]))
    db.commit()
    return redirect(url_for("main.reminders"))


@bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        chosen = [t for t in DOC_TYPES if t in request.form.getlist("required_types")]
        db = get_db()
        db.execute("UPDATE users SET required_types = ? WHERE id = ?", ("|".join(chosen), g.user["id"]))
        db.commit()
        flash("Required documents updated.", "success")
        return redirect(url_for("main.settings"))
    return render_template("settings.html", required=required_types())


# ---------- account ----------

@bp.route("/account")
@login_required
def account():
    return render_template("account.html")


@bp.route("/account/password", methods=["POST"])
@login_required
def change_password():
    current = request.form.get("current_password", "")
    new = request.form.get("new_password", "")
    error = None
    if not check_password_hash(g.user["password_hash"], current):
        error = "Your current password is incorrect."
    elif len(new) < 8:
        error = "New password must be at least 8 characters."
    elif new != request.form.get("confirm_password", ""):
        error = "The new passwords don't match."
    if error:
        flash(error, "error")
        return redirect(url_for("main.account"))
    db = get_db()
    # Bumping the session version signs out every other device; this one gets a fresh session.
    db.execute("UPDATE users SET password_hash = ?, session_version = session_version + 1 WHERE id = ?",
               (generate_password_hash(new), g.user["id"]))
    db.commit()
    start_session(g.user["id"], g.user["session_version"] + 1)
    flash("Password changed. You've been signed out on your other devices.", "success")
    return redirect(url_for("main.account"))


@bp.route("/account/delete", methods=["GET", "POST"])
@login_required
def delete_account():
    if request.method == "GET":
        return render_template("account_delete.html")
    typed = request.form.get("confirm", "").strip()
    if typed != "DELETE" and typed.lower() != g.user["email"].lower():
        flash('Type DELETE in capital letters (or your email address) to confirm.', "error")
        return redirect(url_for("main.delete_account"))
    if not check_password_hash(g.user["password_hash"], request.form.get("password", "")):
        flash("Your password is incorrect.", "error")
        return redirect(url_for("main.delete_account"))
    db = get_db()
    stored = [r["stored_filename"] for r in
              db.execute("SELECT stored_filename FROM documents WHERE user_id = ?", (g.user["id"],))]
    # Subcontractors, documents and reminders are removed with the user (ON DELETE CASCADE).
    db.execute("DELETE FROM users WHERE id = ?", (g.user["id"],))
    db.commit()
    for name in stored:
        try:
            os.remove(os.path.join(current_app.config["UPLOAD_FOLDER"], name))
        except FileNotFoundError:
            pass
    session.clear()
    flash("Your account and all its data have been deleted.", "success")
    return redirect(url_for("main.dashboard"))


# ---------- export all my data ----------

def _csv(rows, columns):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    for r in rows:
        w.writerow(["" if r[c] is None else r[c] for c in columns])
    return buf.getvalue()


@bp.route("/account/export")
@login_required
def export_data():
    """A ZIP with CSV files of everything in this account, plus its uploaded files."""
    db = get_db()
    uid = g.user["id"]
    user = db.execute("SELECT email, company_name, required_types, created_at, paid_until FROM users WHERE id = ?",
                      (uid,)).fetchall()
    subs = db.execute("SELECT id, name, trade, contact_email, phone, created_at FROM subcontractors "
                      "WHERE user_id = ? ORDER BY id", (uid,)).fetchall()
    docs = db.execute(
        "SELECT d.id, d.subcontractor_id, s.name AS subcontractor_name, d.name, d.doc_type, d.original_filename, "
        "d.stored_filename, d.mime_type, d.uploaded_at, d.expiration_date, d.no_expiration, d.needs_review, "
        "d.review_reason, d.extraction_source, d.extraction_evidence FROM documents d "
        "JOIN subcontractors s ON s.id = d.subcontractor_id WHERE d.user_id = ? ORDER BY d.id", (uid,)).fetchall()
    rems = db.execute("SELECT id, document_id, threshold_days, expiration_date, created_at, dismissed FROM reminders "
                      "WHERE user_id = ? ORDER BY id", (uid,)).fetchall()

    def file_name(d):
        return f"files/{d['id']}_{d['original_filename']}"

    doc_rows = [dict(d, file_in_zip=file_name(d)) for d in docs]
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("account.csv", _csv(user, ["email", "company_name", "required_types", "created_at", "paid_until"]))
        z.writestr("subcontractors.csv", _csv(subs, ["id", "name", "trade", "contact_email", "phone", "created_at"]))
        z.writestr("documents.csv", _csv(doc_rows, [
            "id", "subcontractor_id", "subcontractor_name", "name", "doc_type", "original_filename", "mime_type",
            "uploaded_at", "expiration_date", "no_expiration", "needs_review", "review_reason",
            "extraction_source", "extraction_evidence", "file_in_zip"]))
        z.writestr("reminders.csv", _csv(rems, ["id", "document_id", "threshold_days", "expiration_date",
                                                "created_at", "dismissed"]))
        folder = current_app.config["UPLOAD_FOLDER"]
        for d in docs:
            # stored_filename is our own random name, never user input; basename() is a safety net.
            path = os.path.join(folder, os.path.basename(d["stored_filename"]))
            if os.path.isfile(path):
                z.write(path, file_name(d))
    out.seek(0)
    return send_file(out, mimetype="application/zip", as_attachment=True,
                     download_name=f"contractordocs-export-{today().isoformat()}.zip")


# ---------- optional "forgot password" by email (only when email is set up) ----------

RESET_MAX_AGE = 3600  # seconds (1 hour)
RESET_LIMIT = 5       # requests per IP and per email ...
RESET_WINDOW = 900    # ... in this many seconds


def _mail_on():
    return mailer.mail_enabled(current_app.config.get("MAIL"))


def _reset_serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="pw-reset")


def _pw_fingerprint(password_hash):
    # Changes when the password changes, so a used link stops working.
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()[:16]


def _throttled(*keys):
    hits = current_app.extensions.setdefault("contractordocs_reset_hits", {})
    now = time.time()
    blocked = False
    for key in keys:
        recent = [t for t in hits.get(key, []) if now - t < RESET_WINDOW]
        if len(recent) >= RESET_LIMIT:
            blocked = True
        recent.append(now)
        hits[key] = recent
    return blocked


@bp.route("/forgot", methods=["GET", "POST"])
def forgot_password():
    if not _mail_on():
        abort(404)
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if email and not _throttled("ip:" + (request.remote_addr or ""), "email:" + email):
            user = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            if user is not None:
                token = _reset_serializer().dumps({"e": user["email"], "h": _pw_fingerprint(user["password_hash"])})
                link = url_for("main.reset_password", token=token, _external=True)
                mailer.send_mail(current_app.config["MAIL"], user["email"], "Reset your ContractorDocs password",
                                 "Someone asked to reset the password for your ContractorDocs account.\n\n"
                                 f"To choose a new password, open this link within 1 hour:\n{link}\n\n"
                                 "If you did not ask for this, you can ignore this email. "
                                 "Your password stays the same.")
        flash("If an account exists, we sent a link to reset your password. Check your email.", "success")
        return redirect(url_for("main.forgot_password"))
    return render_template("forgot.html")


@bp.route("/reset/<token>", methods=["GET", "POST"])
def reset_password(token):
    if not _mail_on():
        abort(404)
    user = None
    try:
        data = _reset_serializer().loads(token, max_age=RESET_MAX_AGE)
        user = get_db().execute("SELECT * FROM users WHERE email = ?", (str(data.get("e", "")),)).fetchone()
        if user is not None and data.get("h") != _pw_fingerprint(user["password_hash"]):
            user = None
    except (BadSignature, AttributeError):
        user = None
    if user is None:
        flash("This reset link is invalid or has expired. Please ask for a new one.", "error")
        return redirect(url_for("main.forgot_password"))
    if request.method == "POST":
        new = request.form.get("new_password", "")
        if len(new) < 8:
            flash("New password must be at least 8 characters.", "error")
        elif new != request.form.get("confirm_password", ""):
            flash("The new passwords don't match.", "error")
        else:
            db = get_db()
            # New hash = this link stops working; new session version = signed out everywhere.
            db.execute("UPDATE users SET password_hash = ?, session_version = session_version + 1 WHERE id = ?",
                       (generate_password_hash(new), user["id"]))
            db.commit()
            session.clear()
            flash("Your password was changed. Please log in.", "success")
            return redirect(url_for("main.login"))
    return render_template("reset.html", token=token)


@bp.route("/healthz")
def healthz():
    return jsonify(status="ok")
