"""Main application routes.

Every query on business data is limited to the signed-in user's account (`_aid()`), so ids belonging to another
business behave exactly like ids that don't exist (404). The only public routes are the landing page and the
customer report links under /r/<token>, which resolve their account from the share link itself.
"""
import sqlite3
from datetime import date

from flask import (Blueprint, Response, abort, current_app, flash, g, redirect, render_template,
                   request, send_file, url_for)

from . import services, storage
from .auth import login_required, manager_required
from .db import get_db
from .services import ITEM_STATES, SHARE_EXPIRY_CHOICES, STATUS_LABELS
from .utils import EMAIL_RE, Form, escape_like, now_iso, today_iso

bp = Blueprint("main", __name__)

OWNED_TABLES = {"customers", "properties", "templates", "photos"}


def _aid():
    """The signed-in user's account id."""
    return g.user["account_id"]


def _get_or_404(sql, params):
    row = get_db().execute(sql, params).fetchone()
    if row is None:
        abort(404)
    return row


def _owned_or_404(table, row_id):
    assert table in OWNED_TABLES  # never built from user input
    return _get_or_404(f"SELECT * FROM {table} WHERE id = ? AND account_id = ?", (row_id, _aid()))


def _job_or_404(job_id):
    job = services.get_job(job_id, _aid())
    if job is None:
        abort(404)
    return job


def _flash_errors(errors):
    for error in errors:
        flash(error, "error")


# ---------------------------------------------------------------- landing & dashboard

@bp.route("/")
def index():
    """Public landing page for visitors; the dashboard for signed-in users."""
    if g.user is None:
        return render_template("landing.html", price=current_app.config["PRICE_TEXT"],
                               trial_days=current_app.config["TRIAL_DAYS"])
    return _dashboard()


@bp.route("/dashboard")
@login_required
def dashboard():
    return _dashboard()


def _dashboard():
    db = get_db()
    select = services.JOB_SELECT
    today = today_iso()
    aid = _aid()
    return render_template(
        "dashboard.html",
        today=today,
        # assigned_worker is free text, so match it to the signed-in user's name.
        my_jobs=db.execute(
            select + " WHERE j.account_id = ? AND lower(trim(j.assigned_worker)) = lower(trim(?)) AND j.status IN"
            " ('scheduled', 'in_progress') AND j.appointment_date <= ?"
            " ORDER BY j.appointment_date, j.start_time", (aid, g.user["name"], today)).fetchall(),
        todays_jobs=db.execute(
            select + " WHERE j.account_id = ? AND j.appointment_date = ? AND j.status != 'cancelled'"
            " ORDER BY j.start_time = '', j.start_time, j.id", (aid, today)).fetchall(),
        in_progress=db.execute(
            select + " WHERE j.account_id = ? AND j.status = 'in_progress' ORDER BY j.appointment_date, j.start_time",
            (aid,)).fetchall(),
        awaiting=db.execute(
            select + " WHERE j.account_id = ? AND j.status = 'awaiting_review'"
            " ORDER BY j.appointment_date, j.start_time", (aid,)).fetchall(),
        completed=db.execute(
            select + " WHERE j.account_id = ? AND j.status = 'completed' ORDER BY j.completed_at DESC LIMIT 10",
            (aid,)).fetchall(),
        upcoming=db.execute(
            select + " WHERE j.account_id = ? AND j.appointment_date > ? AND j.status = 'scheduled'"
            " ORDER BY j.appointment_date, j.start_time LIMIT 10", (aid, today)).fetchall(),
    )


# ---------------------------------------------------------------- customers

def _customer_values(data):
    form = Form(data)
    values = {
        "name": form.text("name", "Customer name", 200, True),
        "contact_name": form.text("contact_name", "Contact name", 200),
        "email": form.text("email", "Email", 254),
        "phone": form.text("phone", "Phone", 50),
        "notes": form.text("notes", "Notes", 5000),
    }
    if values["email"] and not EMAIL_RE.match(values["email"]):
        form.errors.append("Email address looks invalid.")
    return values, form.errors


@bp.route("/customers")
@login_required
def customers():
    q = (request.args.get("q") or "").strip()[:100]
    sql = ("SELECT c.*, (SELECT COUNT(*) FROM properties p WHERE p.customer_id = c.id) AS property_count,"
           " (SELECT COUNT(*) FROM jobs j WHERE j.customer_id = c.id) AS job_count FROM customers c"
           " WHERE c.account_id = ?")
    params = [_aid()]
    if q:
        sql += " AND (c.name LIKE ? ESCAPE '\\' OR c.contact_name LIKE ? ESCAPE '\\' OR c.email LIKE ? ESCAPE '\\')"
        params += ["%" + escape_like(q) + "%"] * 3
    rows = get_db().execute(sql + " ORDER BY c.name COLLATE NOCASE", params).fetchall()
    return render_template("customers/list.html", customers=rows, q=q)


@bp.route("/customers/new", methods=["GET", "POST"])
@manager_required
def customer_new():
    if request.method == "POST":
        values, errors = _customer_values(request.form)
        if not errors:
            db = get_db()
            cur = db.execute(
                "INSERT INTO customers (account_id, name, contact_name, email, phone, notes, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_aid(), values["name"], values["contact_name"], values["email"], values["phone"], values["notes"],
                 now_iso()))
            db.commit()
            flash("Customer added. Now add a property for them.", "success")
            return redirect(url_for("main.customer_detail", customer_id=cur.lastrowid))
        _flash_errors(errors)
        return render_template("customers/form.html", values=values, customer=None), 400
    return render_template("customers/form.html", values={}, customer=None)


@bp.route("/customers/<int:customer_id>")
@login_required
def customer_detail(customer_id):
    customer = _owned_or_404("customers", customer_id)
    db = get_db()
    props = db.execute("SELECT * FROM properties WHERE customer_id = ? AND account_id = ? ORDER BY name COLLATE NOCASE",
                       (customer_id, _aid())).fetchall()
    jobs = db.execute(services.JOB_SELECT + " WHERE j.customer_id = ? AND j.account_id = ?"
                      " ORDER BY j.appointment_date DESC LIMIT 20", (customer_id, _aid())).fetchall()
    return render_template("customers/detail.html", customer=customer, properties=props, jobs=jobs)


@bp.route("/customers/<int:customer_id>/edit", methods=["GET", "POST"])
@manager_required
def customer_edit(customer_id):
    customer = _owned_or_404("customers", customer_id)
    if request.method == "POST":
        values, errors = _customer_values(request.form)
        if not errors:
            db = get_db()
            db.execute("UPDATE customers SET name = ?, contact_name = ?, email = ?, phone = ?, notes = ?"
                       " WHERE id = ? AND account_id = ?",
                       (values["name"], values["contact_name"], values["email"], values["phone"], values["notes"],
                        customer_id, _aid()))
            db.commit()
            flash("Customer updated.", "success")
            return redirect(url_for("main.customer_detail", customer_id=customer_id))
        _flash_errors(errors)
        return render_template("customers/form.html", values=values, customer=customer), 400
    return render_template("customers/form.html", values=dict(customer), customer=customer)


@bp.route("/customers/<int:customer_id>/delete", methods=["POST"])
@manager_required
def customer_delete(customer_id):
    _owned_or_404("customers", customer_id)
    db = get_db()
    try:
        db.execute("DELETE FROM customers WHERE id = ? AND account_id = ?", (customer_id, _aid()))
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        flash("This customer has jobs on record. Delete those jobs first.", "error")
        return redirect(url_for("main.customer_detail", customer_id=customer_id))
    flash("Customer deleted.", "success")
    return redirect(url_for("main.customers"))


# ---------------------------------------------------------------- properties

def _property_values(data):
    form = Form(data)
    values = {
        "name": form.text("name", "Property name", 200, True),
        "address": form.text("address", "Address", 500),
        "access_notes": form.text("access_notes", "Access notes", 2000),
    }
    return values, form.errors


@bp.route("/customers/<int:customer_id>/properties/new", methods=["GET", "POST"])
@manager_required
def property_new(customer_id):
    customer = _owned_or_404("customers", customer_id)
    if request.method == "POST":
        values, errors = _property_values(request.form)
        if not errors:
            db = get_db()
            db.execute("INSERT INTO properties (account_id, customer_id, name, address, access_notes, created_at)"
                       " VALUES (?, ?, ?, ?, ?, ?)",
                       (_aid(), customer_id, values["name"], values["address"], values["access_notes"], now_iso()))
            db.commit()
            flash("Property added.", "success")
            return redirect(url_for("main.customer_detail", customer_id=customer_id))
        _flash_errors(errors)
        return render_template("properties/form.html", values=values, customer=customer, prop=None), 400
    return render_template("properties/form.html", values={}, customer=customer, prop=None)


@bp.route("/properties/<int:property_id>/edit", methods=["GET", "POST"])
@manager_required
def property_edit(property_id):
    prop = _owned_or_404("properties", property_id)
    customer = _owned_or_404("customers", prop["customer_id"])
    if request.method == "POST":
        values, errors = _property_values(request.form)
        if not errors:
            db = get_db()
            db.execute("UPDATE properties SET name = ?, address = ?, access_notes = ? WHERE id = ? AND account_id = ?",
                       (values["name"], values["address"], values["access_notes"], property_id, _aid()))
            db.commit()
            flash("Property updated.", "success")
            return redirect(url_for("main.customer_detail", customer_id=customer["id"]))
        _flash_errors(errors)
        return render_template("properties/form.html", values=values, customer=customer, prop=prop), 400
    return render_template("properties/form.html", values=dict(prop), customer=customer, prop=prop)


@bp.route("/properties/<int:property_id>/delete", methods=["POST"])
@manager_required
def property_delete(property_id):
    prop = _owned_or_404("properties", property_id)
    db = get_db()
    try:
        db.execute("DELETE FROM properties WHERE id = ? AND account_id = ?", (property_id, _aid()))
        db.commit()
        flash("Property deleted.", "success")
    except sqlite3.IntegrityError:
        db.rollback()
        flash("This property has jobs on record. Delete those jobs first.", "error")
    return redirect(url_for("main.customer_detail", customer_id=prop["customer_id"]))


# ---------------------------------------------------------------- templates

def _parse_task_lines(text):
    return [line.strip()[:300] for line in (text or "").splitlines() if line.strip()]


def _template_values(data):
    form = Form(data)
    values = {
        "name": form.text("name", "Template name", 200, True),
        "description": form.text("description", "Description", 2000),
        "required_tasks": form.text("required_tasks", "Required tasks", 20000),
        "optional_tasks": form.text("optional_tasks", "Optional tasks", 20000),
    }
    required = _parse_task_lines(values["required_tasks"])
    optional = _parse_task_lines(values["optional_tasks"])
    if not required and not optional:
        form.errors.append("Add at least one task.")
    if len(required) + len(optional) > 200:
        form.errors.append("A template can have at most 200 tasks.")
    tasks = [(label, 1) for label in required] + [(label, 0) for label in optional]
    return values, tasks, form.errors


def _save_template_items(db, template_id, tasks):
    db.execute("DELETE FROM template_items WHERE template_id = ? AND account_id = ?", (template_id, _aid()))
    db.executemany("INSERT INTO template_items (account_id, template_id, position, label, required)"
                   " VALUES (?, ?, ?, ?, ?)",
                   [(_aid(), template_id, pos, label, req) for pos, (label, req) in enumerate(tasks)])


@bp.route("/templates")
@login_required
def templates():
    db = get_db()
    rows = db.execute("SELECT t.*, (SELECT COUNT(*) FROM template_items i WHERE i.template_id = t.id) AS task_count,"
                      " (SELECT COUNT(*) FROM template_items i WHERE i.template_id = t.id AND i.required = 1)"
                      " AS required_count FROM templates t WHERE t.account_id = ? ORDER BY t.name COLLATE NOCASE",
                      (_aid(),)).fetchall()
    return render_template("templates/list.html", templates=rows)


@bp.route("/templates/new", methods=["GET", "POST"])
@manager_required
def template_new():
    if request.method == "POST":
        values, tasks, errors = _template_values(request.form)
        if not errors:
            db = get_db()
            with db:
                cur = db.execute("INSERT INTO templates (account_id, name, description, created_at) VALUES (?, ?, ?, ?)",
                                 (_aid(), values["name"], values["description"], now_iso()))
                _save_template_items(db, cur.lastrowid, tasks)
            flash("Template created.", "success")
            return redirect(url_for("main.templates"))
        _flash_errors(errors)
        return render_template("templates/form.html", values=values, template=None), 400
    return render_template("templates/form.html", values={}, template=None)


@bp.route("/templates/<int:template_id>/edit", methods=["GET", "POST"])
@manager_required
def template_edit(template_id):
    template = _owned_or_404("templates", template_id)
    db = get_db()
    if request.method == "POST":
        values, tasks, errors = _template_values(request.form)
        if not errors:
            with db:
                db.execute("UPDATE templates SET name = ?, description = ? WHERE id = ? AND account_id = ?",
                           (values["name"], values["description"], template_id, _aid()))
                _save_template_items(db, template_id, tasks)
            flash("Template updated. Existing jobs keep their original checklist.", "success")
            return redirect(url_for("main.templates"))
        _flash_errors(errors)
        return render_template("templates/form.html", values=values, template=template), 400
    items = db.execute("SELECT * FROM template_items WHERE template_id = ? AND account_id = ? ORDER BY position",
                       (template_id, _aid())).fetchall()
    values = dict(template)
    values["required_tasks"] = "\n".join(i["label"] for i in items if i["required"])
    values["optional_tasks"] = "\n".join(i["label"] for i in items if not i["required"])
    return render_template("templates/form.html", values=values, template=template)


@bp.route("/templates/<int:template_id>/delete", methods=["POST"])
@manager_required
def template_delete(template_id):
    _owned_or_404("templates", template_id)
    db = get_db()
    db.execute("DELETE FROM templates WHERE id = ? AND account_id = ?", (template_id, _aid()))
    db.commit()
    flash("Template deleted. Jobs created from it keep their checklists.", "success")
    return redirect(url_for("main.templates"))


# ---------------------------------------------------------------- jobs: list, export

def _filter_choices():
    db = get_db()
    return {
        "customers": db.execute("SELECT id, name FROM customers WHERE account_id = ? ORDER BY name COLLATE NOCASE",
                                (_aid(),)).fetchall(),
        "properties": db.execute("SELECT p.id, p.name, p.customer_id, c.name AS customer_name FROM properties p"
                                 " JOIN customers c ON c.id = p.customer_id WHERE p.account_id = ?"
                                 " ORDER BY c.name COLLATE NOCASE, p.name COLLATE NOCASE", (_aid(),)).fetchall(),
    }


@bp.route("/jobs")
@login_required
def jobs():
    page = request.args.get("page", "1")
    page = int(page) if page.isdigit() and int(page) > 0 else 1
    rows, total, filters, errors = services.search_jobs(request.args, _aid(), page=page)
    _flash_errors(errors)
    pages = max(1, -(-total // services.PAGE_SIZE))
    export_args = {k: v for k, v in filters.items()}
    return render_template("jobs/list.html", jobs=rows, total=total, filters=filters, page=page, pages=pages,
                           export_args=export_args, statuses=STATUS_LABELS, **_filter_choices())


@bp.route("/jobs/export.csv")
@manager_required
def jobs_export():
    rows, _, _, _ = services.search_jobs(request.args, _aid(), limit=None)
    filename = f"proofofwork-jobs-{date.today().strftime('%Y%m%d')}.csv"
    return Response(services.jobs_to_csv(rows), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---------------------------------------------------------------- jobs: create/edit

def _job_values(data, creating):
    form = Form(data)
    values = {
        "title": form.text("title", "Job title", 200, True),
        "customer_id": form.integer("customer_id", "Customer", True),
        "property_id": form.integer("property_id", "Property", True),
        "assigned_worker": form.text("assigned_worker", "Assigned worker", 100),
        "appointment_date": form.date("appointment_date", "Appointment date", True),
        "start_time": form.time("start_time", "Start time"),
        "end_time": form.time("end_time", "End time"),
        "internal_notes": form.text("internal_notes", "Internal notes", 5000),
        "report_notes": form.text("report_notes", "Report notes", 5000),
        "template_id": form.integer("template_id", "Template") if creating else None,
    }
    db = get_db()
    errors = form.errors
    # Ids from another business are treated exactly like ids that don't exist.
    if values["customer_id"] and not db.execute("SELECT 1 FROM customers WHERE id = ? AND account_id = ?",
                                                (values["customer_id"], _aid())).fetchone():
        errors.append("Selected customer does not exist.")
    if values["property_id"]:
        prop = db.execute("SELECT customer_id FROM properties WHERE id = ? AND account_id = ?",
                          (values["property_id"], _aid())).fetchone()
        if prop is None:
            errors.append("Selected property does not exist.")
        elif values["customer_id"] and prop["customer_id"] != values["customer_id"]:
            errors.append("The selected property does not belong to the selected customer.")
    if values["template_id"] and not db.execute("SELECT 1 FROM templates WHERE id = ? AND account_id = ?",
                                                (values["template_id"], _aid())).fetchone():
        errors.append("Selected template does not exist.")
    if values["start_time"] and values["end_time"] and values["end_time"] < values["start_time"]:
        errors.append("End time cannot be earlier than start time.")
    return values, errors


def _job_form_context():
    ctx = _filter_choices()
    ctx["templates"] = get_db().execute("SELECT id, name FROM templates WHERE account_id = ? ORDER BY name COLLATE NOCASE",
                                        (_aid(),)).fetchall()
    ctx["team_members"] = [r[0] for r in get_db().execute(
        "SELECT name FROM users WHERE active = 1 AND account_id = ? ORDER BY name COLLATE NOCASE", (_aid(),)).fetchall()]
    return ctx


@bp.route("/jobs/new", methods=["GET", "POST"])
@manager_required
def job_new():
    if request.method == "POST":
        values, errors = _job_values(request.form, creating=True)
        if not errors:
            db = get_db()
            now = now_iso()
            with db:
                cur = db.execute(
                    "INSERT INTO jobs (account_id, title, customer_id, property_id, template_id, assigned_worker,"
                    " appointment_date, start_time, end_time, internal_notes, report_notes, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (_aid(), values["title"], values["customer_id"], values["property_id"], values["template_id"],
                     values["assigned_worker"], values["appointment_date"], values["start_time"], values["end_time"],
                     values["internal_notes"], values["report_notes"], now, now))
                job_id = cur.lastrowid
                if values["template_id"]:
                    db.execute("INSERT INTO job_items (account_id, job_id, position, label, required)"
                               " SELECT account_id, ?, position, label, required FROM template_items"
                               " WHERE template_id = ? AND account_id = ?",
                               (job_id, values["template_id"], _aid()))
                services.log_event(db, job_id, g.user["name"], "created", values["title"])
            flash("Job created.", "success")
            return redirect(url_for("main.job_detail", job_id=job_id))
        _flash_errors(errors)
        return render_template("jobs/form.html", values=values, job=None, **_job_form_context()), 400
    values = {"appointment_date": request.args.get("date") or today_iso()}
    for key in ("customer_id", "property_id", "template_id"):
        if (request.args.get(key) or "").isdigit():
            values[key] = int(request.args[key])
    return render_template("jobs/form.html", values=values, job=None, **_job_form_context())


@bp.route("/jobs/<int:job_id>/edit", methods=["GET", "POST"])
@manager_required
def job_edit(job_id):
    job = _job_or_404(job_id)
    if services.is_locked(job):
        flash("Reopen this job before editing it.", "error")
        return redirect(url_for("main.job_detail", job_id=job_id))
    if request.method == "POST":
        values, errors = _job_values(request.form, creating=False)
        if not errors:
            db = get_db()
            changed = [k for k in ("title", "customer_id", "property_id", "assigned_worker", "appointment_date",
                                   "start_time", "end_time", "internal_notes", "report_notes")
                       if values[k] != job[k]]
            with db:
                db.execute("UPDATE jobs SET title = ?, customer_id = ?, property_id = ?, assigned_worker = ?,"
                           " appointment_date = ?, start_time = ?, end_time = ?, internal_notes = ?, report_notes = ?,"
                           " updated_at = ? WHERE id = ? AND account_id = ?",
                           (values["title"], values["customer_id"], values["property_id"], values["assigned_worker"],
                            values["appointment_date"], values["start_time"], values["end_time"],
                            values["internal_notes"], values["report_notes"], now_iso(), job_id, _aid()))
                if changed:
                    services.log_event(db, job_id, g.user["name"], "edited",
                                       ", ".join(k.replace("_id", "").replace("_", " ") for k in changed))
            flash("Job updated.", "success")
            return redirect(url_for("main.job_detail", job_id=job_id))
        _flash_errors(errors)
        return render_template("jobs/form.html", values=values, job=job, **_job_form_context()), 400
    return render_template("jobs/form.html", values=dict(job), job=job, **_job_form_context())


@bp.route("/jobs/<int:job_id>/delete", methods=["POST"])
@manager_required
def job_delete(job_id):
    job = _job_or_404(job_id)
    stored = [p["stored_name"] for p in services.get_job_photos(job_id)]
    db = get_db()
    db.execute("DELETE FROM jobs WHERE id = ? AND account_id = ?", (job_id, _aid()))
    db.commit()
    for name in stored:
        storage.delete_stored(name)
    flash(f"Job “{job['title']}” deleted.", "success")
    return redirect(url_for("main.jobs"))


# ---------------------------------------------------------------- job detail & checklist

def _render_job(job_id, status=200, posted=None, new_share_url=None):
    job = _job_or_404(job_id)
    is_manager = g.user["role"] == "manager"
    photos = services.photos_with_status(job_id)
    photo_groups = services.photo_label_summary(photos)
    return render_template(
        "jobs/detail.html",
        job=job,
        items=services.get_job_items(job_id),
        photos=photos,
        photo_groups=photo_groups,
        unpaired=[label for label, g_ in photo_groups.items() if g_["before"] and not g_["after"]],
        issues=services.completion_issues(job) if job["status"] in ("scheduled", "in_progress", "awaiting_review") else [],
        locked=services.is_locked(job),
        events=services.get_job_events(job_id),
        event_labels=services.EVENT_LABELS,
        share_links=services.list_share_links(job_id) if is_manager else [],
        new_share_url=new_share_url,
        posted=posted or {},
        item_states=ITEM_STATES,
        expiry_choices=SHARE_EXPIRY_CHOICES,
        max_photos=current_app.config["MAX_PHOTOS_PER_UPLOAD"],
        now=now_iso(),
    ), status


@bp.route("/jobs/<int:job_id>")
@login_required
def job_detail(job_id):
    return _render_job(job_id)


def _locked_redirect(job):
    flash("This job is " + STATUS_LABELS[job["status"]].lower() + ". A manager must reopen it to make changes.",
          "error")
    return redirect(url_for("main.job_detail", job_id=job["id"]))


def _merge_field(form, name, current, cleaned, conflicts, label):
    """Apply a submitted field only if the user changed it, detecting edits made by someone else meanwhile.

    The page sends the value it was rendered with as orig_<name>. If the user left the field alone, the saved
    value wins; if both the user and someone else changed it, it is reported as a conflict instead of
    silently overwriting either change.
    """
    if name not in form:
        return current
    original = form.get("orig_" + name)
    if original is None:  # older page without originals: last write wins
        return cleaned
    original = original.replace("\x00", "").strip()
    if cleaned == original:
        return current
    if current != original and current != cleaned:
        conflicts.append(label)
        return current
    return cleaned


@bp.route("/jobs/<int:job_id>/checklist", methods=["POST"])
@login_required
def job_checklist(job_id):
    job = _job_or_404(job_id)
    if services.is_locked(job):
        return _locked_redirect(job)
    errors, conflicts, updates, changes = [], [], [], []
    # Only this job's items are considered; ids for other jobs' items in the form are ignored.
    for item in services.get_job_items(job_id):
        key, label = item["id"], item["label"]
        form = Form(request.form)
        submitted_state = request.form.get(f"state_{key}", item["state"])
        if submitted_state not in ITEM_STATES:
            errors.append(f"Invalid result for “{label}”.")
            continue
        note = form.text(f"note_{key}", f"Note for “{label}”", 1000)
        reason = form.text(f"reason_{key}", f"Reason for “{label}”", 1000)
        errors += form.errors
        state = _merge_field(request.form, f"state_{key}", item["state"], submitted_state, conflicts, label)
        note = _merge_field(request.form, f"note_{key}", item["note"], note, conflicts, label)
        reason = _merge_field(request.form, f"reason_{key}", item["reason"], reason, conflicts, label)
        if state == "incomplete" and not reason:
            errors.append(f"Give a reason why “{label}” was not completed.")
        if state != "incomplete":
            reason = ""
        if (state, note, reason) != (item["state"], item["note"], item["reason"]):
            updates.append((state, note, reason, g.user["name"], now_iso(), key, job_id))
            change = f"{label}: {ITEM_STATES[state]}"
            if state == "incomplete":
                change += f" ({reason})"
            changes.append(change)
    if conflicts:
        names = ", ".join(f"“{c}”" for c in dict.fromkeys(conflicts))
        errors.append(f"Someone else changed {names} while you were editing. Nothing was saved; the page now shows "
                      "their saved values under your entries — check them and save again.")
    if errors:
        _flash_errors(errors)
        return _render_job(job_id, status=400, posted=request.form)
    if not updates:
        flash("No checklist changes to save.", "info")
        return redirect(url_for("main.job_detail", job_id=job_id) + "#checklist")
    db = get_db()
    with db:
        db.executemany("UPDATE job_items SET state = ?, note = ?, reason = ?, updated_by = ?, updated_at = ?"
                       " WHERE id = ? AND job_id = ?", updates)
        services.log_event(db, job_id, g.user["name"], "checklist", "; ".join(changes))
        status_message = services.record_work_activity(db, job, g.user)
    flash("Checklist saved.", "success")
    if status_message:
        flash(status_message, "info")
    return redirect(url_for("main.job_detail", job_id=job_id) + "#checklist")


@bp.route("/jobs/<int:job_id>/notes", methods=["POST"])
@login_required
def job_notes(job_id):
    """Workers and managers can record notes on site without the manager-only edit form."""
    job = _job_or_404(job_id)
    if services.is_locked(job):
        return _locked_redirect(job)
    form = Form(request.form)
    conflicts = []
    values = {}
    for field, label in (("report_notes", "Report notes"), ("internal_notes", "Internal notes")):
        cleaned = form.text(field, label, 5000)
        values[field] = _merge_field(request.form, field, job[field], cleaned, conflicts, label)
    errors = form.errors
    if conflicts:
        errors.append("Someone else changed " + " and ".join(conflicts).lower() +
                      " while you were editing. Nothing was saved; copy your text and try again.")
    if errors:
        _flash_errors(errors)
        return _render_job(job_id, status=400, posted=request.form)
    changed = [f for f in values if values[f] != job[f]]
    if not changed:
        flash("No note changes to save.", "info")
        return redirect(url_for("main.job_detail", job_id=job_id) + "#notes")
    db = get_db()
    with db:
        db.execute("UPDATE jobs SET report_notes = ?, internal_notes = ?, updated_at = ? WHERE id = ? AND account_id = ?",
                   (values["report_notes"], values["internal_notes"], now_iso(), job_id, _aid()))
        services.log_event(db, job_id, g.user["name"], "notes",
                           ", ".join("report notes" if f == "report_notes" else "internal notes" for f in changed))
        status_message = services.record_work_activity(db, job, g.user)
    flash("Notes saved.", "success")
    if status_message:
        flash(status_message, "info")
    return redirect(url_for("main.job_detail", job_id=job_id) + "#notes")


@bp.route("/jobs/<int:job_id>/items", methods=["POST"])
@login_required
def job_item_add(job_id):
    job = _job_or_404(job_id)
    if services.is_locked(job):
        return _locked_redirect(job)
    form = Form(request.form)
    label = form.text("label", "Task", 300, True)
    if form.errors:
        _flash_errors(form.errors)
        return _render_job(job_id, status=400)
    required = 1 if form.checkbox("required") else 0
    db = get_db()
    with db:
        position = db.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM job_items WHERE job_id = ?",
                              (job_id,)).fetchone()[0]
        db.execute("INSERT INTO job_items (account_id, job_id, position, label, required) VALUES (?, ?, ?, ?, ?)",
                   (_aid(), job_id, position, label, required))
        services.log_event(db, job_id, g.user["name"], "task_added", label + ("" if required else " (optional)"))
        status_message = services.record_work_activity(db, job, g.user) if job["status"] == "awaiting_review" else None
    flash("Task added.", "success")
    if status_message:
        flash(status_message, "info")
    return redirect(url_for("main.job_detail", job_id=job_id) + "#checklist")


@bp.route("/jobs/<int:job_id>/items/<int:item_id>/delete", methods=["POST"])
@manager_required
def job_item_delete(job_id, item_id):
    job = _job_or_404(job_id)
    if services.is_locked(job):
        return _locked_redirect(job)
    db = get_db()
    item = db.execute("SELECT * FROM job_items WHERE id = ? AND job_id = ? AND account_id = ?",
                      (item_id, job_id, _aid())).fetchone()
    if item is None:
        abort(404)
    with db:
        db.execute("DELETE FROM job_items WHERE id = ? AND account_id = ?", (item_id, _aid()))
        services.log_event(db, job_id, g.user["name"], "task_removed",
                           f"{item['label']} (was {ITEM_STATES[item['state']]})")
    flash("Task removed.", "success")
    return redirect(url_for("main.job_detail", job_id=job_id) + "#checklist")


@bp.route("/jobs/<int:job_id>/status", methods=["POST"])
@login_required
def job_status(job_id):
    job = _job_or_404(job_id)
    action = request.form.get("action", "")
    errors = services.apply_transition(job, action, g.user, confirm_exceptions=request.form.get("confirm_exceptions") == "1")
    if errors:
        _flash_errors(errors)
        return _render_job(job_id, status=400)
    flash(services.TRANSITION_MESSAGES[action], "success")
    return redirect(url_for("main.job_detail", job_id=job_id))


# ---------------------------------------------------------------- photos

@bp.route("/jobs/<int:job_id>/photos", methods=["POST"])
@login_required
def photo_upload(job_id):
    job = _job_or_404(job_id)
    if services.is_locked(job):
        return _locked_redirect(job)
    form = Form(request.form)
    kind = request.form.get("kind", "")
    label = " ".join(form.text("label", "Photo label", 100, True).split())
    caption = form.text("caption", "Caption", 300)
    errors = form.errors
    if kind not in ("before", "after"):
        errors.append("Choose whether these are before or after photos.")
    files = [f for f in request.files.getlist("photo") if f and f.filename]
    limit = current_app.config["MAX_PHOTOS_PER_UPLOAD"]
    if not files:
        errors.append("Choose a photo to upload.")
    elif len(files) > limit:
        errors.append(f"Upload at most {limit} photos at a time.")
    saved = []
    if not errors:
        # All-or-nothing: if any photo is rejected, none are kept.
        for number, file in enumerate(files, start=1):
            try:
                saved.append(storage.save_upload(file))
            except storage.UploadError as exc:
                name = file.filename.replace("\\", "/").rsplit("/", 1)[-1][:60]
                prefix = f"Photo {number} ({name}): " if len(files) > 1 else ""
                errors.append(prefix + str(exc) + (" No photos were saved." if len(files) > 1 else ""))
                break
    if errors:
        for meta in saved:
            storage.delete_stored(meta["stored_name"])
        _flash_errors(errors)
        return _render_job(job_id, status=400, posted=request.form)
    db = get_db()
    try:
        with db:
            now = now_iso()
            db.executemany(
                "INSERT INTO photos (account_id, job_id, kind, label, caption, stored_name, width, height, byte_size,"
                " uploaded_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(_aid(), job_id, kind, label, caption, m["stored_name"], m["width"], m["height"], m["byte_size"],
                  g.user["name"], now) for m in saved])
            services.log_event(db, job_id, g.user["name"], "photos_added", f"{len(saved)} {kind} – {label}")
            status_message = services.record_work_activity(db, job, g.user)
    except Exception:
        for meta in saved:
            storage.delete_stored(meta["stored_name"])
        raise
    flash(f"{len(saved)} {kind} photo{'s' if len(saved) != 1 else ''} uploaded.", "success")
    if status_message:
        flash(status_message, "info")
    return redirect(url_for("main.job_detail", job_id=job_id) + "#photos")


@bp.route("/jobs/<int:job_id>/photos/<int:photo_id>/delete", methods=["POST"])
@login_required
def photo_delete(job_id, photo_id):
    job = _job_or_404(job_id)
    if services.is_locked(job):
        return _locked_redirect(job)
    photo = _get_or_404("SELECT * FROM photos WHERE id = ? AND job_id = ? AND account_id = ?",
                        (photo_id, job_id, _aid()))
    db = get_db()
    with db:
        db.execute("DELETE FROM photos WHERE id = ? AND account_id = ?", (photo_id, _aid()))
        services.log_event(db, job_id, g.user["name"], "photo_deleted", f"{photo['kind']} – {photo['label']}")
        status_message = services.record_work_activity(db, job, g.user) if job["status"] == "awaiting_review" else None
    storage.delete_stored(photo["stored_name"])
    flash("Photo deleted.", "success")
    if status_message:
        flash(status_message, "info")
    return redirect(url_for("main.job_detail", job_id=job_id) + "#photos")


def _send_photo(photo):
    try:
        path = storage.stored_path(photo["stored_name"])
    except ValueError:
        abort(404)
    try:
        return send_file(path, mimetype="image/jpeg", max_age=0, conditional=True)
    except FileNotFoundError:
        abort(404)


@bp.route("/photos/<int:photo_id>")
@login_required
def photo(photo_id):
    return _send_photo(_owned_or_404("photos", photo_id))


# ---------------------------------------------------------------- reports & sharing

@bp.route("/jobs/<int:job_id>/report")
@login_required
def job_report(job_id):
    report = services.build_report(job_id, _aid())
    if report is None:
        abort(404)
    return render_template("report.html", public=False, token=None, company_name=report["business_name"], **report)


def _share_url(token):
    path = url_for("main.shared_report", token=token)
    base = current_app.config.get("BASE_URL")
    return base + path if base else url_for("main.shared_report", token=token, _external=True)


@bp.route("/jobs/<int:job_id>/share", methods=["POST"])
@manager_required
def share_create(job_id):
    _job_or_404(job_id)
    raw = request.form.get("days", "30")
    days = int(raw) if raw.isdigit() else -1
    if days not in SHARE_EXPIRY_CHOICES:
        flash("Choose a valid link expiry.", "error")
        return _render_job(job_id, status=400)
    token = services.create_share_link(job_id, g.user, days)
    db = get_db()
    services.log_event(db, job_id, g.user["name"], "share_created",
                       f"…{token[-4:]}, expires: {SHARE_EXPIRY_CHOICES[days].lower()}")
    db.commit()
    # Rendered directly (not stored in the session cookie): the token is a secret shown exactly once.
    flash("Share link created. Copy it now — for security it is only shown once.", "success")
    return _render_job(job_id, new_share_url=_share_url(token))


@bp.route("/jobs/<int:job_id>/share/<int:link_id>/revoke", methods=["POST"])
@manager_required
def share_revoke(job_id, link_id):
    _job_or_404(job_id)
    if services.revoke_share_link(job_id, link_id):
        db = get_db()
        hint = db.execute("SELECT token_hint FROM share_links WHERE id = ? AND job_id = ?",
                          (link_id, job_id)).fetchone()[0]
        services.log_event(db, job_id, g.user["name"], "share_revoked", f"…{hint}")
        db.commit()
        flash("Share link revoked. Anyone using it will no longer see the report.", "success")
    else:
        flash("That link was not found or is already revoked.", "error")
    return redirect(url_for("main.job_detail", job_id=job_id) + "#sharing")


@bp.route("/jobs/<int:job_id>/share/revoke-all", methods=["POST"])
@manager_required
def share_revoke_all(job_id):
    _job_or_404(job_id)
    db = get_db()
    with db:
        cur = db.execute("UPDATE share_links SET revoked_at = ? WHERE job_id = ? AND account_id = ?"
                         " AND revoked_at IS NULL", (now_iso(), job_id, _aid()))
        if cur.rowcount:
            services.log_event(db, job_id, g.user["name"], "share_revoked", f"All active links ({cur.rowcount})")
    flash(f"Revoked {cur.rowcount} link(s)." if cur.rowcount else "There were no active links.", "success")
    return redirect(url_for("main.job_detail", job_id=job_id) + "#sharing")


# Public customer pages. They don't depend on who is signed in or on the business's subscription:
# a report link keeps working until it is revoked or expires, even after the owner's trial ends.

@bp.route("/r/<token>")
def shared_report(token):
    link = services.resolve_share_token(token)
    if link is None:
        abort(404)
    report = services.build_report(link["job_id"], link["account_id"])
    if report is None:
        abort(404)
    return render_template("report.html", public=True, token=token, company_name=report["business_name"], **report)


@bp.route("/r/<token>/photos/<int:photo_id>")
def shared_photo(token, photo_id):
    link = services.resolve_share_token(token)
    if link is None:
        abort(404)
    return _send_photo(_get_or_404("SELECT * FROM photos WHERE id = ? AND job_id = ? AND account_id = ?",
                                   (photo_id, link["job_id"], link["account_id"])))


# ---------------------------------------------------------------- admin

@bp.route("/admin")
@manager_required
def admin():
    db = get_db()
    counts = {table: db.execute(f"SELECT COUNT(*) FROM {table} WHERE account_id = ?", (_aid(),)).fetchone()[0]
              for table in ("customers", "properties", "templates", "jobs", "photos", "share_links")}
    return render_template("admin.html", counts=counts, reset_allowed=current_app.config["ALLOW_DEMO_RESET"],
                           audit=services.storage_audit(_aid()))


@bp.route("/admin/storage/cleanup", methods=["POST"])
@manager_required
def admin_storage_cleanup():
    removed = services.cleanup_orphans()
    flash(f"Removed {removed} unused photo file(s). Files under an hour old are kept in case an upload is in progress.",
          "success")
    return redirect(url_for("main.admin"))


@bp.route("/admin/reset", methods=["POST"])
@manager_required
def admin_reset():
    from .demo import reset_data
    if not current_app.config["ALLOW_DEMO_RESET"]:
        abort(403)
    if request.form.get("confirm") != "RESET":
        flash("Type RESET in capitals to confirm.", "error")
        return redirect(url_for("main.admin"))
    with_demo = request.form.get("mode") == "demo"
    reset_data(_aid(), with_demo=with_demo)
    flash("All of your business's job data was reset" + (" and demo data loaded." if with_demo else ".") +
          " User accounts were kept.", "success")
    return redirect(url_for("main.dashboard"))
