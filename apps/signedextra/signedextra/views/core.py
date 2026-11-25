"""Dashboard, GCs (customers), jobs, settings, labor rates, team and data export."""
import csv
import io
import sqlite3

from flask import (Blueprint, Response, abort, flash, g, redirect, render_template, request,
                   url_for)

from ..db import get_db
from ..links import shareable_url
from ..passwords import create_reset_link
from ..security import audit, login_required, new_token, roles_required
from ..tickets_service import STATUS_LABELS, get_job, labor_rates, status_totals
from ..util import iso_in, money_to_cents, now_iso, x100_to_str
from ..validation import Form

bp = Blueprint("core", __name__)
MANAGERS = ("owner", "office")


@bp.route("/")
def dashboard():
    if g.user is None:
        from flask import current_app
        return render_template("landing.html", price=current_app.config["PRICE_TEXT"],
                               trial_days=current_app.config["TRIAL_DAYS"],
                               billing=current_app.config["BILLING_ENABLED"])
    db = get_db()
    cid = g.company["id"]
    if g.user["role"] == "foreman":
        job_ids = [r["job_id"] for r in db.execute(
            "SELECT job_id FROM job_members WHERE user_id = ?", (g.user["id"],))]
        totals = status_totals(cid, job_ids)
        recent = db.execute(
            "SELECT t.*, j.name AS job_name FROM tickets t JOIN jobs j ON j.id = t.job_id"
            " JOIN job_members m ON m.job_id = t.job_id AND m.user_id = ?"
            " WHERE t.company_id = ? ORDER BY t.updated_at DESC LIMIT 15", (g.user["id"], cid)).fetchall()
    else:
        totals = status_totals(cid)
        recent = db.execute(
            "SELECT t.*, j.name AS job_name FROM tickets t JOIN jobs j ON j.id = t.job_id"
            " WHERE t.company_id = ? ORDER BY t.updated_at DESC LIMIT 15", (cid,)).fetchall()

    def bucket(*statuses):
        return {"n": sum(totals.get(s, {}).get("n", 0) for s in statuses),
                "cents": sum(totals.get(s, {}).get("cents", 0) for s in statuses)}

    buckets = [
        ("Waiting on signature", bucket("draft", "awaiting_signature"), "warn"),
        ("Refused / declined (needs notice)", bucket("declined", "notice_pending"), "danger"),
        ("Signed or noticed, not invoiced", bucket("signed", "noticed"), "info"),
        ("Invoiced, unpaid", bucket("invoiced"), "info"),
        ("Disputed", bucket("disputed"), "danger"),
        ("Paid", bucket("paid"), "ok"),
    ]
    needs_setup = g.user["role"] != "foreman" and not labor_rates(cid)
    return render_template("dashboard.html", buckets=buckets, recent=recent, needs_setup=needs_setup)


# --- customers (GCs) --------------------------------------------------------

@bp.route("/customers")
@roles_required(*MANAGERS)
def customers():
    rows = get_db().execute(
        "SELECT c.*, (SELECT COUNT(*) FROM jobs j WHERE j.customer_id = c.id) AS job_count"
        " FROM customers c WHERE c.company_id = ? ORDER BY c.name", (g.company["id"],)).fetchall()
    return render_template("customers/list.html", customers=rows)


def _get_customer(customer_id):
    row = get_db().execute("SELECT * FROM customers WHERE id = ? AND company_id = ?",
                           (customer_id, g.company["id"])).fetchone()
    if row is None:
        abort(404)
    return row


@bp.route("/customers/new", methods=["GET", "POST"])
@bp.route("/customers/<int:customer_id>/edit", methods=["GET", "POST"])
@roles_required(*MANAGERS)
def customer_form(customer_id=None):
    customer = _get_customer(customer_id) if customer_id else None
    form = Form(request.form)
    if request.method == "POST":
        name = form.text("name", "GC name", max_len=120)
        address = form.text("address", "Address", required=False, max_len=300)
        notes = form.text("notes", "Contract notes", required=False, max_len=2000)
        if form.ok:
            db = get_db()
            if customer:
                db.execute("UPDATE customers SET name = ?, address = ?, notes = ? WHERE id = ?",
                           (name, address, notes, customer["id"]))
                cid = customer["id"]
            else:
                cid = db.execute(
                    "INSERT INTO customers (company_id, name, address, notes, created_at)"
                    " VALUES (?, ?, ?, ?, ?)", (g.company["id"], name, address, notes, now_iso())).lastrowid
            audit(g.company["id"], "customer.saved", details={"customer_id": cid})
            db.commit()
            flash("GC saved.", "success")
            return redirect(url_for("core.customer_detail", customer_id=cid))
    elif customer:
        form.values = dict(customer)
    return render_template("customers/form.html", form=form, customer=customer)


@bp.route("/customers/<int:customer_id>")
@roles_required(*MANAGERS)
def customer_detail(customer_id):
    customer = _get_customer(customer_id)
    db = get_db()
    contacts = db.execute("SELECT * FROM customer_contacts WHERE customer_id = ? ORDER BY name",
                          (customer_id,)).fetchall()
    jobs = db.execute("SELECT * FROM jobs WHERE customer_id = ? AND company_id = ? ORDER BY name",
                      (customer_id, g.company["id"])).fetchall()
    return render_template("customers/detail.html", customer=customer, contacts=contacts,
                           jobs=jobs, form=Form({}))


@bp.route("/customers/<int:customer_id>/contacts", methods=["POST"])
@roles_required(*MANAGERS)
def add_contact(customer_id):
    customer = _get_customer(customer_id)
    form = Form(request.form)
    name = form.text("name", "Contact name", max_len=120)
    title = form.text("title", "Title", required=False, max_len=120)
    email = form.email("email", required=False)
    phone = form.text("phone", "Phone", required=False, max_len=40)
    receives = form.checkbox("receives_notices")
    if receives and not email:
        form.errors["email"] = "An email is needed for contacts who receive notices."
    if not form.ok:
        db = get_db()
        contacts = db.execute("SELECT * FROM customer_contacts WHERE customer_id = ?", (customer_id,)).fetchall()
        jobs = db.execute("SELECT * FROM jobs WHERE customer_id = ?", (customer_id,)).fetchall()
        return render_template("customers/detail.html", customer=customer, contacts=contacts,
                               jobs=jobs, form=form), 422
    db = get_db()
    db.execute("INSERT INTO customer_contacts (company_id, customer_id, name, title, email, phone,"
               " receives_notices) VALUES (?, ?, ?, ?, ?, ?, ?)",
               (g.company["id"], customer_id, name, title, email, phone, int(receives)))
    audit(g.company["id"], "contact.added", details={"customer_id": customer_id, "name": name})
    db.commit()
    flash("Contact added.", "success")
    return redirect(url_for("core.customer_detail", customer_id=customer_id))


@bp.route("/customers/<int:customer_id>/contacts/<int:contact_id>/delete", methods=["POST"])
@roles_required(*MANAGERS)
def delete_contact(customer_id, contact_id):
    _get_customer(customer_id)
    db = get_db()
    db.execute("DELETE FROM customer_contacts WHERE id = ? AND customer_id = ? AND company_id = ?",
               (contact_id, customer_id, g.company["id"]))
    audit(g.company["id"], "contact.removed", details={"contact_id": contact_id})
    db.commit()
    flash("Contact removed.", "success")
    return redirect(url_for("core.customer_detail", customer_id=customer_id))


# --- jobs -------------------------------------------------------------------

@bp.route("/jobs")
@login_required
def jobs():
    db = get_db()
    if g.user["role"] == "foreman":
        rows = db.execute(
            "SELECT j.*, c.name AS customer_name FROM jobs j JOIN customers c ON c.id = j.customer_id"
            " JOIN job_members m ON m.job_id = j.id AND m.user_id = ?"
            " WHERE j.company_id = ? AND j.status = 'active' ORDER BY j.name",
            (g.user["id"], g.company["id"])).fetchall()
    else:
        rows = db.execute(
            "SELECT j.*, c.name AS customer_name FROM jobs j JOIN customers c ON c.id = j.customer_id"
            " WHERE j.company_id = ? ORDER BY j.status, j.name", (g.company["id"],)).fetchall()
    return render_template("jobs/list.html", jobs=rows)


def _job_form_save(form, job=None):
    db = get_db()
    customers = db.execute("SELECT id FROM customers WHERE company_id = ?", (g.company["id"],)).fetchall()
    customer_ids = {str(c["id"]) for c in customers}
    name = form.text("name", "Job name", max_len=120)
    customer_id = form.choice("customer_id", "GC", customer_ids)
    address = form.text("address", "Site address", required=False, max_len=300)
    gc_ref = form.text("gc_reference", "GC job / PO number", required=False, max_len=80)
    markup = form.decimal("markup_pct", "Markup %", required=False, min_value=0, max_value=100)
    notice = form.integer("notice_hours", "Notice deadline (hours)", required=False, min_value=1, max_value=720)
    status = form.choice("status", "Status", ("active", "closed")) if job else "active"
    if not form.ok:
        return None
    markup_bp = int(markup * 100) if markup is not None else None
    if job:
        db.execute("UPDATE jobs SET customer_id = ?, name = ?, address = ?, gc_reference = ?, markup_bp = ?,"
                   " notice_hours = ?, status = ? WHERE id = ? AND company_id = ?",
                   (int(customer_id), name, address, gc_ref, markup_bp, notice, status, job["id"], g.company["id"]))
        job_id = job["id"]
    else:
        job_id = db.execute(
            "INSERT INTO jobs (company_id, customer_id, name, address, gc_reference, markup_bp, notice_hours,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (g.company["id"], int(customer_id), name, address, gc_ref, markup_bp, notice, now_iso())).lastrowid
    audit(g.company["id"], "job.saved", details={"job_id": job_id})
    db.commit()
    return job_id


@bp.route("/jobs/new", methods=["GET", "POST"])
@bp.route("/jobs/<int:job_id>/edit", methods=["GET", "POST"])
@roles_required(*MANAGERS)
def job_form(job_id=None):
    job = get_job(job_id, g.company["id"]) if job_id else None
    customers = get_db().execute("SELECT id, name FROM customers WHERE company_id = ? ORDER BY name",
                                 (g.company["id"],)).fetchall()
    form = Form(request.form)
    if request.method == "POST":
        saved_id = _job_form_save(form, job)
        if saved_id:
            flash("Job saved.", "success")
            return redirect(url_for("core.job_detail", job_id=saved_id))
    elif job:
        form.values = dict(job)
        form.values["customer_id"] = str(job["customer_id"])
        form.values["markup_pct"] = x100_to_str(job["markup_bp"]) if job["markup_bp"] is not None else ""
        form.values["notice_hours"] = job["notice_hours"] or ""
    else:
        form.values["customer_id"] = request.args.get("customer_id", "")
    return render_template("jobs/form.html", form=form, job=job, customers=customers)


@bp.route("/jobs/<int:job_id>")
@login_required
def job_detail(job_id):
    from ..tickets_service import user_can_access_job
    job = get_job(job_id, g.company["id"])
    if not user_can_access_job(g.user, job_id):
        abort(404)
    db = get_db()
    tickets = db.execute("SELECT * FROM tickets WHERE job_id = ? AND company_id = ? ORDER BY number DESC",
                         (job_id, g.company["id"])).fetchall()
    members = db.execute("SELECT u.* FROM users u JOIN job_members m ON m.user_id = u.id WHERE m.job_id = ?"
                         " ORDER BY u.name", (job_id,)).fetchall()
    foremen = db.execute("SELECT * FROM users WHERE company_id = ? AND role = 'foreman' AND active = 1"
                         " ORDER BY name", (g.company["id"],)).fetchall()
    invoices = db.execute("SELECT * FROM invoices WHERE job_id = ? AND company_id = ? ORDER BY number DESC",
                          (job_id, g.company["id"])).fetchall()
    return render_template("jobs/detail.html", job=job, tickets=tickets, members=members,
                           foremen=foremen, invoices=invoices)


@bp.route("/jobs/<int:job_id>/members", methods=["POST"])
@roles_required(*MANAGERS)
def job_members(job_id):
    get_job(job_id, g.company["id"])
    db = get_db()
    foremen = {r["id"] for r in db.execute(
        "SELECT id FROM users WHERE company_id = ? AND role = 'foreman'", (g.company["id"],))}
    chosen = {int(v) for v in request.form.getlist("user_id") if v.isdigit()} & foremen
    db.execute("DELETE FROM job_members WHERE job_id = ?", (job_id,))
    for uid in chosen:
        db.execute("INSERT INTO job_members (job_id, user_id) VALUES (?, ?)", (job_id, uid))
    audit(g.company["id"], "job.members_set", details={"job_id": job_id, "user_ids": sorted(chosen)})
    db.commit()
    flash("Crew assignment saved.", "success")
    return redirect(url_for("core.job_detail", job_id=job_id))


# --- settings & labor rates -------------------------------------------------

@bp.route("/settings", methods=["GET", "POST"])
@roles_required(*MANAGERS)
def settings():
    db = get_db()
    form = Form(request.form)
    if request.method == "POST":
        if g.user["role"] != "owner":
            abort(403)
        name = form.text("name", "Company name", max_len=120)
        address = form.text("address", "Address", required=False, max_len=300)
        phone = form.text("phone", "Phone", required=False, max_len=40)
        email = form.email("email", "Company email", required=False)
        markup = form.decimal("markup_pct", "Default markup %", min_value=0, max_value=100)
        notice = form.integer("notice_hours", "Default notice deadline (hours)", min_value=1, max_value=720)
        sees = form.checkbox("foreman_sees_prices")
        if form.ok:
            db.execute("UPDATE companies SET name = ?, address = ?, phone = ?, email = ?, default_markup_bp = ?,"
                       " notice_hours = ?, foreman_sees_prices = ? WHERE id = ?",
                       (name, address, phone, email, int(markup * 100), notice, int(sees), g.company["id"]))
            audit(g.company["id"], "company.settings_saved")
            db.commit()
            flash("Settings saved.", "success")
            return redirect(url_for("core.settings"))
    else:
        form.values = dict(g.company)
        form.values["markup_pct"] = x100_to_str(g.company["default_markup_bp"])
    rates = labor_rates(g.company["id"], active_only=False)
    return render_template("settings.html", form=form, rates=rates, rate_form=Form({}))


@bp.route("/settings/rates", methods=["POST"])
@roles_required(*MANAGERS)
def add_rate():
    form = Form(request.form)
    label = form.text("label", "Worker type", max_len=60)
    rate = form.decimal("rate", "Hourly rate", min_value=0, max_value=10000)
    db = get_db()
    if form.ok:
        try:
            db.execute("INSERT INTO labor_rates (company_id, label, rate_cents) VALUES (?, ?, ?)",
                       (g.company["id"], label, money_to_cents(rate)))
            audit(g.company["id"], "rate.added", details={"label": label, "rate_cents": money_to_cents(rate)})
            db.commit()
            flash("Labor rate added.", "success")
            return redirect(url_for("core.settings"))
        except sqlite3.IntegrityError:
            db.rollback()
            form.errors["label"] = "That worker type already exists."
    settings_form = Form({})
    settings_form.values = dict(g.company)
    settings_form.values["markup_pct"] = x100_to_str(g.company["default_markup_bp"])
    return render_template("settings.html", form=settings_form, rates=labor_rates(g.company["id"], False),
                           rate_form=form), 422


@bp.route("/settings/rates/<int:rate_id>", methods=["POST"])
@roles_required(*MANAGERS)
def update_rate(rate_id):
    db = get_db()
    rate = db.execute("SELECT * FROM labor_rates WHERE id = ? AND company_id = ?",
                      (rate_id, g.company["id"])).fetchone()
    if rate is None:
        abort(404)
    form = Form(request.form)
    value = form.decimal("rate", "Hourly rate", min_value=0, max_value=10000)
    active = form.checkbox("active")
    if not form.ok:
        flash(next(iter(form.errors.values())), "error")
        return redirect(url_for("core.settings"))
    db.execute("UPDATE labor_rates SET rate_cents = ?, active = ? WHERE id = ?",
               (money_to_cents(value), int(active), rate_id))
    audit(g.company["id"], "rate.updated", details={"rate_id": rate_id, "from": rate["rate_cents"],
                                                     "to": money_to_cents(value), "active": active})
    db.commit()
    flash("Rate updated. Existing tickets keep the rate they were created with.", "success")
    return redirect(url_for("core.settings"))


# --- team -------------------------------------------------------------------

@bp.route("/team")
@roles_required(*MANAGERS)
def team():
    users = get_db().execute("SELECT * FROM users WHERE company_id = ? ORDER BY active DESC, name",
                             (g.company["id"],)).fetchall()
    return render_template("team.html", users=users, form=Form({}), invite_link=None)


@bp.route("/team/invite", methods=["POST"])
@roles_required("owner")
def invite():
    db = get_db()
    form = Form(request.form)
    name = form.text("name", "Name", max_len=120)
    email = form.email("email")
    role = form.choice("role", "Role", ("office", "foreman"))
    invite_link = None
    if form.ok:
        try:
            uid = db.execute("INSERT INTO users (company_id, name, email, role, created_at) VALUES (?, ?, ?, ?, ?)",
                             (g.company["id"], name, email, role, now_iso())).lastrowid
        except sqlite3.IntegrityError:
            db.rollback()
            form.errors["email"] = "That email already has an account."
        else:
            invite_link = _make_invite(uid)
            audit(g.company["id"], "user.invited", details={"user_id": uid, "role": role})
            db.commit()
            form = Form({})
    users = db.execute("SELECT * FROM users WHERE company_id = ? ORDER BY active DESC, name",
                       (g.company["id"],)).fetchall()
    return render_template("team.html", users=users, form=form, invite_link=invite_link), (200 if form.ok else 422)


def _make_invite(user_id):
    raw, token_hash = new_token()
    from flask import current_app
    get_db().execute("INSERT INTO invites (company_id, user_id, token_hash, expires_at) VALUES (?, ?, ?, ?)",
                     (g.company["id"], user_id, token_hash, iso_in(days=current_app.config["INVITE_DAYS"])))
    return shareable_url("auth.accept_invite", token=raw)


@bp.route("/team/<int:user_id>/reset-link", methods=["POST"])
@roles_required("owner")
def reset_link(user_id):
    """Owner creates a one-time, 24-hour password-reset link for a team member (shown once)."""
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ? AND company_id = ?", (user_id, g.company["id"])).fetchone()
    if user is None or user["id"] == g.user["id"] or not user["active"] or not user["password_hash"]:
        abort(404)
    link = create_reset_link(user, created_by=g.user["name"])
    db.commit()
    users = db.execute("SELECT * FROM users WHERE company_id = ? ORDER BY active DESC, name",
                       (g.company["id"],)).fetchall()
    return render_template("team.html", users=users, form=Form({}), invite_link=None,
                           reset_link=link, reset_for=user["name"])


@bp.route("/team/<int:user_id>/reinvite", methods=["POST"])
@roles_required("owner")
def reinvite(user_id):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ? AND company_id = ?", (user_id, g.company["id"])).fetchone()
    if user is None or user["role"] == "owner":
        abort(404)
    db.execute("UPDATE invites SET used_at = ? WHERE user_id = ? AND used_at IS NULL", (now_iso(), user_id))
    link = _make_invite(user_id)
    audit(g.company["id"], "user.reinvited", details={"user_id": user_id})
    db.commit()
    users = db.execute("SELECT * FROM users WHERE company_id = ? ORDER BY active DESC, name",
                       (g.company["id"],)).fetchall()
    return render_template("team.html", users=users, form=Form({}), invite_link=link)


@bp.route("/team/<int:user_id>/active", methods=["POST"])
@roles_required("owner")
def set_active(user_id):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ? AND company_id = ?", (user_id, g.company["id"])).fetchone()
    if user is None or user["id"] == g.user["id"]:
        abort(404)
    active = request.form.get("active") == "1"
    # Bumping the epoch ends any session the user currently has.
    db.execute("UPDATE users SET active = ?, session_epoch = session_epoch + 1 WHERE id = ?", (int(active), user_id))
    audit(g.company["id"], "user.activated" if active else "user.deactivated", details={"user_id": user_id})
    db.commit()
    flash("Team member updated.", "success")
    return redirect(url_for("core.team"))


# --- export -----------------------------------------------------------------

def _cell(value):
    """Stop spreadsheet apps from treating user text as a formula."""
    text = str(value or "")
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@bp.route("/export/tickets.csv")
@roles_required(*MANAGERS)
def export_tickets():
    db = get_db()
    rows = db.execute(
        "SELECT t.*, j.name AS job_name, c.name AS customer_name FROM tickets t JOIN jobs j ON j.id = t.job_id"
        " JOIN customers c ON c.id = j.customer_id WHERE t.company_id = ? ORDER BY t.number",
        (g.company["id"],)).fetchall()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["ticket_number", "status", "gc", "job", "work_date", "description", "requested_by",
                     "requested_by_role", "labor", "materials", "markup", "total", "content_sha256",
                     "created_at", "updated_at"])
    for t in rows:
        writer.writerow([f"T-{t['number']:04d}", STATUS_LABELS[t["status"]], _cell(t["customer_name"]),
                         _cell(t["job_name"]), t["work_date"], _cell(t["description"]),
                         _cell(t["requested_by_name"]), _cell(t["requested_by_role"]),
                         f"{t['labor_cents'] / 100:.2f}", f"{t['material_cents'] / 100:.2f}",
                         f"{t['markup_cents'] / 100:.2f}", f"{t['total_cents'] / 100:.2f}",
                         t["content_hash"], t["created_at"], t["updated_at"]])
    audit(g.company["id"], "export.tickets_csv")
    db.commit()
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=tickets.csv"})
