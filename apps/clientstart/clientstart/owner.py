"""Owner (business) side of ClientStart. Every query is filtered by the signed-in account."""
import csv
import io

from flask import (Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request,
                   send_file, url_for)

from . import services as svc
from .db import get_db, now_iso, today
from .auth import require_owner
from .security import new_portal_token, upload_path

bp = Blueprint("owner", __name__)
# Every owner route requires a signed-in owner (see auth.py). Clients only ever use /p/<token>.
bp.before_request(require_owner)


# ---------------------------------------------------------------- helpers

def A():
    """The signed-in account's id. Every query below filters on it."""
    return g.account["id"]


def one_or_404(sql, args):
    row = get_db().execute(sql, args).fetchone()
    if row is None:
        abort(404)  # also for another account's records: same answer as "doesn't exist"
    return row


def get_client(client_id):
    return one_or_404("SELECT * FROM clients WHERE id = ? AND account_id = ?", (client_id, A()))


def get_task(task_id):
    return one_or_404("SELECT * FROM tasks WHERE id = ? AND account_id = ?", (task_id, A()))


def get_template(template_id):
    return one_or_404("SELECT * FROM onboarding_templates WHERE id = ? AND account_id = ?", (template_id, A()))


def account_templates():
    return get_db().execute(
        """SELECT t.*, (SELECT COUNT(*) FROM template_items i WHERE i.template_id = t.id) AS item_count
           FROM onboarding_templates t WHERE t.account_id = ? ORDER BY t.name""", (A(),)).fetchall()


def text(name, max_len=200, required=False, label=None, errors=None):
    value = (request.form.get(name) or "").strip()[:max_len]
    if required and not value and errors is not None:
        errors.append(f"{label or name.replace('_', ' ').capitalize()} is required.")
    return value


def choice(name, options, default, errors=None, label=None):
    """A value from a fixed set. Missing -> default; present but invalid -> error (not silently changed)."""
    value = request.form.get(name)
    if value is None or value == "":
        return default
    if value not in options:
        if errors is not None:
            errors.append(f"{label or name.capitalize()} must be one of: {', '.join(options)}.")
        return default
    return value


def date_field(name, errors, label):
    try:
        return svc.parse_date(request.form.get(name))
    except ValueError:
        errors.append(f"{label} must be a valid date (YYYY-MM-DD).")
        return None


def client_form():
    errors = []
    fields = dict(
        name=text("name", required=True, label="Client name", errors=errors),
        company=text("company"),
        email=text("email", 254),
        phone=text("phone", 50),
        project_name=text("project_name", required=True, label="Project name", errors=errors),
        project_type=text("project_type", 80),
        notes=text("notes", 5000),
        start_date=date_field("start_date", errors, "Start date"),
    )
    if fields["email"] and ("@" not in fields["email"] or " " in fields["email"]):
        errors.append("Email address looks invalid.")
    return fields, errors


def task_form():
    errors = []
    fields = dict(
        title=text("title", required=True, label="Title", errors=errors),
        description=text("description", 4000),
        kind=choice("kind", svc.KINDS, "task", errors, "Type"),
        assignee=choice("assignee", svc.ASSIGNEES, "client", errors, "Assigned to"),
        priority=choice("priority", svc.PRIORITIES, "medium", errors, "Priority"),
        due_date=date_field("due_date", errors, "Due date"),
    )
    return fields, errors


def portal_link(client):
    """Absolute portal URL. Uses CLIENTSTART_PUBLIC_URL when set (e.g. behind a proxy)."""
    base = current_app.config.get("PUBLIC_URL")
    path = url_for("portal.home", token=client["portal_token"])
    return base + path if base else url_for("portal.home", token=client["portal_token"], _external=True)


def csv_response(filename, header, rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for row in rows:
        writer.writerow([svc.csv_safe(v) for v in row])
    return Response(
        "﻿" + buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------- dashboard

@bp.get("/dashboard")
def dashboard():
    return render_template("dashboard.html", **svc.dashboard_data(A()), today=today().isoformat())


# ---------------------------------------------------------------- clients

def filtered_clients():
    q = (request.args.get("q") or "").strip()
    status = request.args.get("status", "")
    ptype = request.args.get("project_type", "")
    sql = "SELECT * FROM clients WHERE account_id = ?"
    args = [A()]
    if q:
        like = f"%{q}%"
        sql += " AND (name LIKE ? OR company LIKE ? OR email LIKE ? OR project_name LIKE ? OR notes LIKE ?)"
        args += [like] * 5
    if status in svc.CLIENT_STATUSES:
        sql += " AND status = ?"
        args.append(status)
    if ptype:
        sql += " AND project_type = ?"
        args.append(ptype)
    sql += " ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'completed' THEN 1 ELSE 2 END, name"
    return get_db().execute(sql, args).fetchall(), dict(q=q, status=status, project_type=ptype)


@bp.get("/clients")
def clients():
    rows, filters = filtered_clients()
    types = [r[0] for r in get_db().execute(
        "SELECT DISTINCT project_type FROM clients WHERE account_id = ? AND project_type != '' ORDER BY 1", (A(),))]
    total = get_db().execute("SELECT COUNT(*) FROM clients WHERE account_id = ?", (A(),)).fetchone()[0]
    return render_template("clients.html", clients=rows, filters=filters, types=types, total=total,
                           progress=svc.progress_for_clients([c["id"] for c in rows]))


@bp.get("/clients/export.csv")
def export_clients():
    rows, _ = filtered_clients()
    prog = svc.progress_for_clients([c["id"] for c in rows])
    return csv_response(
        "clientstart-onboardings.csv",
        ["id", "client", "company", "email", "phone", "project", "project_type", "start_date",
         "status", "progress_percent", "tasks_done", "tasks_total", "created_at", "notes"],
        ([c["id"], c["name"], c["company"], c["email"], c["phone"], c["project_name"], c["project_type"],
          c["start_date"], c["status"], prog[c["id"]]["percent"], prog[c["id"]]["done"],
          prog[c["id"]]["total"], c["created_at"], c["notes"]] for c in rows),
    )


@bp.route("/clients/new", methods=["GET", "POST"])
def client_new():
    db = get_db()
    templates = account_templates()
    if request.method == "POST":
        fields, errors = client_form()
        template_id = request.form.get("template_id", type=int)
        tpl = db.execute("SELECT * FROM onboarding_templates WHERE id = ? AND account_id = ?",
                         (template_id, A())).fetchone() if template_id else None
        if template_id and tpl is None:
            errors.append("Selected template does not exist.")
        if tpl is not None and not fields["project_type"]:
            fields["project_type"] = tpl["project_type"]
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("client_form.html", client=fields, templates=templates, is_new=True), 400
        client_id = svc.create_client(fields, template_id, account_id=A())
        count = db.execute("SELECT COUNT(*) FROM tasks WHERE client_id = ? AND account_id = ?",
                           (client_id, A())).fetchone()[0]
        if count == 0:
            flash("Onboarding created with an empty checklist. Add items or apply a template below.", "info")
        else:
            flash(f"Onboarding created with {count} checklist items. Review them, then copy the "
                  "client portal link to share.", "success")
        return redirect(url_for("owner.client_detail", client_id=client_id))
    return render_template("client_form.html", client={}, templates=templates, is_new=True)


@bp.get("/clients/<int:client_id>")
def client_detail(client_id):
    db = get_db()
    client = get_client(client_id)
    tasks = db.execute(
        """SELECT t.*, (SELECT COUNT(*) FROM uploads u WHERE u.task_id = t.id) AS file_count,
                  (SELECT COUNT(*) FROM comments m WHERE m.task_id = t.id) AS comment_count
           FROM tasks t WHERE t.client_id = ? AND t.account_id = ? ORDER BY t.position, t.id""",
        (client_id, A())).fetchall()
    activity = db.execute(
        "SELECT * FROM activity WHERE client_id = ? AND account_id = ? ORDER BY id DESC LIMIT 50",
        (client_id, A())).fetchall()
    templates = account_templates()
    link = portal_link(client)
    values = svc.reminder_values(client, tasks, link, g.account["name"])
    reminders = [
        dict(name=r["name"], subject=svc.render_reminder(r["subject"], values),
             body=svc.render_reminder(r["body"], values))
        for r in db.execute("SELECT * FROM reminder_templates WHERE account_id = ? ORDER BY id", (A(),))
    ]
    # Checklist grouped by who needs to act next, so the page reads as a to-do list.
    groups = [
        ("review", "Needs your review", "Client submissions waiting for approval.",
         [t for t in tasks if t["status"] == "submitted"]),
        ("client", "Waiting on client", "Shown in the client portal.",
         [t for t in tasks if t["assignee"] == "client" and t["status"] in svc.WAITING_ON_CLIENT]),
        ("owner", "Agency to-do", "Internal tasks. Never shown to the client.",
         [t for t in tasks if t["assignee"] == "owner" and t["status"] in svc.WAITING_ON_CLIENT]),
        ("done", "Done", "", [t for t in tasks if t["status"] == "done"]),
    ]
    return render_template(
        "client_detail.html", client=client, tasks=tasks, groups=groups, activity=activity,
        templates=templates, progress=svc.client_progress(client_id), portal_link=link,
        reminders=reminders, today=today().isoformat(), is_overdue=svc.is_overdue,
        link_state=svc.portal_link_state(client),
    )


@bp.route("/clients/<int:client_id>/edit", methods=["GET", "POST"])
def client_edit(client_id):
    client = get_client(client_id)
    if request.method == "POST":
        fields, errors = client_form()
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("client_form.html", client=fields | {"id": client_id}, is_new=False), 400
        get_db().execute(
            """UPDATE clients SET name=?, company=?, email=?, phone=?, project_name=?, project_type=?,
                                  start_date=?, notes=? WHERE id=? AND account_id=?""",
            (fields["name"], fields["company"], fields["email"], fields["phone"], fields["project_name"],
             fields["project_type"], fields["start_date"], fields["notes"], client_id, A()))
        svc.log_activity(client_id, "owner", "Client details updated.", commit=True)
        flash("Client details saved.", "success")
        return redirect(url_for("owner.client_detail", client_id=client_id))
    return render_template("client_form.html", client=client, is_new=False)


@bp.post("/clients/<int:client_id>/status")
def client_status(client_id):
    client = get_client(client_id)
    status = request.form.get("status")
    if status not in svc.CLIENT_STATUSES:
        abort(400)
    completed = now_iso() if status == "completed" else None
    get_db().execute("UPDATE clients SET status = ?, completed_at = ? WHERE id = ? AND account_id = ?",
                     (status, completed, client_id, A()))
    labels = {"active": "reopened", "completed": "marked complete", "archived": "archived"}
    svc.log_activity(client_id, "owner", f"Onboarding {labels[status]}.", commit=True)
    flash(f"{client['name']}: onboarding {labels[status]}.", "success")
    return redirect(url_for("owner.client_detail", client_id=client_id))


@bp.post("/clients/<int:client_id>/delete")
def client_delete(client_id):
    client = get_client(client_id)
    if request.form.get("confirm_name", "").strip() != client["name"]:
        flash("To delete, type the client name exactly as shown.", "error")
        return redirect(url_for("owner.client_detail", client_id=client_id))
    svc.delete_client(client_id, A())
    flash(f"Deleted {client['name']} and all of their tasks, files, and history.", "success")
    return redirect(url_for("owner.clients"))


@bp.post("/clients/<int:client_id>/portal")
def client_portal(client_id):
    client = get_client(client_id)
    action = request.form.get("action")
    db = get_db()
    if action == "regenerate":
        db.execute("UPDATE clients SET portal_token = ?, portal_enabled = 1, portal_expires_at = ? "
                   "WHERE id = ? AND account_id = ?", (new_portal_token(), svc.portal_expiry_date(), client_id, A()))
        svc.log_activity(client_id, "owner", "Portal link regenerated; the old link no longer works.")
        flash("New portal link generated. The previous link has stopped working.", "success")
    elif action == "extend":
        expires = svc.portal_expiry_date()
        db.execute("UPDATE clients SET portal_expires_at = ? WHERE id = ? AND account_id = ?",
                   (expires, client_id, A()))
        svc.log_activity(client_id, "owner", f"Portal link extended until {expires}.")
        flash(f"Portal link now works until {expires}. The link itself is unchanged.", "success")
    elif action in ("enable", "disable"):
        db.execute("UPDATE clients SET portal_enabled = ? WHERE id = ? AND account_id = ?",
                   (1 if action == "enable" else 0, client_id, A()))
        svc.log_activity(client_id, "owner", f"Client portal {action}d.")
        flash(f"Client portal {action}d.", "success")
    else:
        abort(400)
    db.commit()
    return redirect(url_for("owner.client_detail", client_id=client["id"]) + "#portal")


@bp.post("/clients/<int:client_id>/apply-template")
def client_apply_template(client_id):
    client = get_client(client_id)
    template_id = request.form.get("template_id", type=int)
    tpl = get_template(template_id)
    count = svc.apply_template(client_id, template_id, client["start_date"])
    svc.log_activity(client_id, "owner", f"Applied template “{tpl['name']}” ({count} items).", commit=True)
    flash(f"Added {count} items from “{tpl['name']}”.", "success")
    return redirect(url_for("owner.client_detail", client_id=client_id))


@bp.get("/clients/<int:client_id>/export.csv")
def client_export(client_id):
    client = get_client(client_id)
    rows = get_db().execute(
        "SELECT * FROM tasks WHERE client_id = ? AND account_id = ? ORDER BY position, id", (client_id, A())).fetchall()
    return csv_response(
        f"onboarding-{client_id}.csv",
        ["client", "project", "task", "type", "assigned_to", "priority", "due_date", "status",
         "overdue", "answer", "completed_at"],
        ([client["name"], client["project_name"], t["title"], t["kind"], t["assignee"], t["priority"],
          t["due_date"], t["status"], "yes" if svc.is_overdue(t) else "no", t["answer"], t["completed_at"]]
         for t in rows),
    )


@bp.post("/clients/<int:client_id>/tasks")
def task_add(client_id):
    get_client(client_id)
    fields, errors = task_form()
    if errors:
        for e in errors:
            flash(e, "error")
    else:
        svc.add_task(client_id, fields)
        flash("Checklist item added.", "success")
    return redirect(url_for("owner.client_detail", client_id=client_id) + "#checklist")


# ---------------------------------------------------------------- tasks

def filtered_tasks():
    f = dict(q=(request.args.get("q") or "").strip(), status=request.args.get("status", ""),
             assignee=request.args.get("assignee", ""), priority=request.args.get("priority", ""),
             overdue=request.args.get("overdue", ""), client_status=request.args.get("client_status", "active"))
    sql = """SELECT t.*, c.name AS client_name, c.project_name FROM tasks t
             JOIN clients c ON c.id = t.client_id WHERE t.account_id = ? AND c.account_id = ?"""
    args = [A(), A()]
    if f["q"]:
        like = f"%{f['q']}%"
        sql += " AND (t.title LIKE ? OR t.description LIKE ? OR t.answer LIKE ? OR c.name LIKE ? OR c.project_name LIKE ?)"
        args += [like] * 5
    if f["status"] == "waiting":  # same definition as the dashboard's "waiting" cards
        sql += " AND t.status IN ('todo', 'in_progress', 'needs_correction')"
    elif f["status"] in svc.TASK_STATUSES:
        sql += " AND t.status = ?"
        args.append(f["status"])
    if f["assignee"] in svc.ASSIGNEES:
        sql += " AND t.assignee = ?"
        args.append(f["assignee"])
    if f["priority"] in svc.PRIORITIES:
        sql += " AND t.priority = ?"
        args.append(f["priority"])
    if f["overdue"] == "1":
        sql += " AND t.status NOT IN ('done', 'submitted') AND t.due_date IS NOT NULL AND t.due_date < ?"
        args.append(today().isoformat())
    if f["client_status"] in svc.CLIENT_STATUSES:
        sql += " AND c.status = ?"
        args.append(f["client_status"])
    sql += " ORDER BY t.due_date IS NULL, t.due_date, CASE t.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, t.id"
    return get_db().execute(sql, args).fetchall(), f


@bp.get("/tasks")
def tasks():
    rows, filters = filtered_tasks()
    total = get_db().execute("SELECT COUNT(*) FROM tasks WHERE account_id = ?", (A(),)).fetchone()[0]
    return render_template("tasks.html", tasks=rows, filters=filters, total=total, is_overdue=svc.is_overdue)


@bp.get("/tasks/export.csv")
def tasks_export():
    rows, _ = filtered_tasks()
    return csv_response(
        "clientstart-tasks.csv",
        ["task_id", "client", "project", "task", "type", "assigned_to", "priority", "due_date", "status",
         "overdue", "answer", "completed_at"],
        ([t["id"], t["client_name"], t["project_name"], t["title"], t["kind"], t["assignee"], t["priority"],
          t["due_date"], t["status"], "yes" if svc.is_overdue(t) else "no", t["answer"], t["completed_at"]]
         for t in rows),
    )


@bp.get("/tasks/<int:task_id>")
def task_detail(task_id):
    db = get_db()
    task = get_task(task_id)
    client = get_client(task["client_id"])
    files = db.execute("SELECT * FROM uploads WHERE task_id = ? AND account_id = ? ORDER BY id DESC",
                       (task_id, A())).fetchall()
    comments = db.execute("SELECT * FROM comments WHERE task_id = ? AND account_id = ? ORDER BY id",
                          (task_id, A())).fetchall()
    return render_template("task_detail.html", task=task, client=client, files=files, comments=comments,
                           overdue=svc.is_overdue(task))


@bp.route("/tasks/<int:task_id>/edit", methods=["GET", "POST"])
def task_edit(task_id):
    task = get_task(task_id)
    if request.method == "POST":
        fields, errors = task_form()
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("task_form.html", task=fields | {"id": task_id, "client_id": task["client_id"]}), 400
        get_db().execute(
            """UPDATE tasks SET title=?, description=?, kind=?, assignee=?, priority=?, due_date=?, updated_at=?
               WHERE id=? AND account_id=?""",
            (fields["title"], fields["description"], fields["kind"], fields["assignee"], fields["priority"],
             fields["due_date"], now_iso(), task_id, A()))
        svc.log_activity(task["client_id"], "owner", f"Edited checklist item “{fields['title']}”.", commit=True)
        flash("Checklist item saved.", "success")
        return redirect(url_for("owner.task_detail", task_id=task_id))
    return render_template("task_form.html", task=task)


def back_to(task):
    nxt = request.form.get("next", "")
    root = request.script_root or ""
    # Only local paths inside this app (they include the /clientstart prefix when mounted).
    if (nxt.startswith("/") and not nxt.startswith(("//", "/\\")) and "\\" not in nxt
            and (not root or nxt.startswith(root + "/"))):
        return redirect(nxt)
    return redirect(url_for("owner.task_detail", task_id=task["id"]))


@bp.post("/tasks/<int:task_id>/status")
def task_status(task_id):
    task = get_task(task_id)
    status = request.form.get("status")
    if status not in svc.TASK_STATUSES:
        abort(400)
    svc.set_task_status(task, status)
    get_db().commit()
    flash(f"“{task['title']}” is now {svc.STATUS_LABELS[status]}.", "success")
    return back_to(task)


@bp.post("/tasks/<int:task_id>/review")
def task_review(task_id):
    task = get_task(task_id)
    decision = request.form.get("decision")
    note = text("comment", 4000)
    if decision == "approve":
        if note:
            svc.add_comment(task, "owner", note)
        svc.set_task_status(task, "done")
        svc.log_activity(task["client_id"], "owner", f"Approved “{task['title']}”.")
        flash("Submission approved.", "success")
    elif decision == "correction":
        if not note:
            flash("Explain what needs correcting so the client knows what to change.", "error")
            return back_to(task)
        svc.add_comment(task, "owner", note, is_correction=True)
        svc.set_task_status(task, "needs_correction")
        svc.log_activity(task["client_id"], "owner", f"Requested a correction on “{task['title']}”.")
        flash("Correction requested. The client will see your note in their portal.", "success")
    else:
        abort(400)
    get_db().commit()
    return back_to(task)


@bp.post("/tasks/<int:task_id>/comment")
def task_comment(task_id):
    task = get_task(task_id)
    body = text("body", 4000)
    if not body:
        flash("Comment cannot be empty.", "error")
    else:
        internal = task["assignee"] == "owner" or request.form.get("internal") == "1"
        svc.add_comment(task, "owner", body, is_internal=internal)
        svc.log_activity(task["client_id"], "owner",
                         f"{'Internal note' if internal else 'Comment'} on “{task['title']}”.", commit=True)
        flash("Internal note added (hidden from the client)." if internal
              else "Comment added. The client can see it in their portal.", "success")
    return redirect(url_for("owner.task_detail", task_id=task_id) + "#comments")


@bp.post("/tasks/<int:task_id>/delete")
def task_delete(task_id):
    task = get_task(task_id)
    svc.delete_task(task)
    flash(f"Deleted “{task['title']}”.", "success")
    return redirect(url_for("owner.client_detail", client_id=task["client_id"]) + "#checklist")


@bp.get("/files/<int:file_id>")
def file_download(file_id):
    f = one_or_404("SELECT * FROM uploads WHERE id = ? AND account_id = ?", (file_id, A()))
    return send_file(upload_path(f["stored_name"]), mimetype=f["mime_type"], as_attachment=True,
                     download_name=f["original_name"])


@bp.post("/files/<int:file_id>/delete")
def file_delete(file_id):
    f = one_or_404("SELECT * FROM uploads WHERE id = ? AND account_id = ?", (file_id, A()))
    svc.delete_upload(f, "owner")
    flash("File deleted.", "success")
    return redirect(url_for("owner.task_detail", task_id=f["task_id"]))


# ---------------------------------------------------------------- onboarding templates

@bp.get("/templates")
def templates():
    rows = get_db().execute(
        """SELECT t.*, (SELECT COUNT(*) FROM template_items i WHERE i.template_id = t.id) AS item_count
           FROM onboarding_templates t WHERE t.account_id = ? ORDER BY t.project_type, t.name""", (A(),)).fetchall()
    return render_template("templates.html", templates=rows)


@bp.post("/templates/new")
def template_new():
    errors = []
    name = text("name", required=True, label="Template name", errors=errors)
    ptype = text("project_type", 80, required=True, label="Project type", errors=errors)
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("owner.templates"))
    tid = get_db().execute(
        "INSERT INTO onboarding_templates (account_id, name, project_type, description, created_at) VALUES (?, ?, ?, ?, ?)",
        (A(), name, ptype, text("description", 1000), now_iso())).lastrowid
    get_db().commit()
    flash("Template created. Add checklist items below.", "success")
    return redirect(url_for("owner.template_detail", template_id=tid))


@bp.route("/templates/<int:template_id>", methods=["GET", "POST"])
def template_detail(template_id):
    db = get_db()
    tpl = get_template(template_id)
    if request.method == "POST":
        errors = []
        name = text("name", required=True, label="Template name", errors=errors)
        ptype = text("project_type", 80, required=True, label="Project type", errors=errors)
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            db.execute("UPDATE onboarding_templates SET name=?, project_type=?, description=? WHERE id=? AND account_id=?",
                       (name, ptype, text("description", 1000), template_id, A()))
            db.commit()
            flash("Template saved.", "success")
        return redirect(url_for("owner.template_detail", template_id=template_id))
    items = db.execute("SELECT * FROM template_items WHERE template_id = ? AND account_id = ? ORDER BY position, id",
                       (template_id, A())).fetchall()
    return render_template("template_detail.html", tpl=tpl, items=items)


@bp.post("/templates/<int:template_id>/items")
def template_item_add(template_id):
    get_template(template_id)
    fields, errors = task_form()
    offset = request.form.get("due_offset_days", type=int)
    if offset is None or not 0 <= offset <= 365:
        errors.append("Due offset must be a number of days between 0 and 365.")
    if errors:
        for e in errors:
            flash(e, "error")
    else:
        db = get_db()
        pos = db.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM template_items WHERE template_id = ?",
                         (template_id,)).fetchone()[0]
        db.execute(
            """INSERT INTO template_items (account_id, template_id, title, description, kind, assignee, priority,
                                           due_offset_days, position) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (A(), template_id, fields["title"], fields["description"], fields["kind"], fields["assignee"],
             fields["priority"], offset, pos))
        db.commit()
        flash("Item added to template.", "success")
    return redirect(url_for("owner.template_detail", template_id=template_id))


@bp.post("/templates/<int:template_id>/items/<int:item_id>/delete")
def template_item_delete(template_id, item_id):
    one_or_404("SELECT 1 FROM template_items WHERE id = ? AND template_id = ? AND account_id = ?",
               (item_id, template_id, A()))
    get_db().execute("DELETE FROM template_items WHERE id = ? AND account_id = ?", (item_id, A()))
    get_db().commit()
    flash("Item removed from template.", "success")
    return redirect(url_for("owner.template_detail", template_id=template_id))


@bp.post("/templates/<int:template_id>/delete")
def template_delete(template_id):
    tpl = get_template(template_id)
    get_db().execute("DELETE FROM onboarding_templates WHERE id = ? AND account_id = ?", (template_id, A()))
    get_db().commit()
    flash(f"Deleted template “{tpl['name']}”. Existing onboardings are unaffected.", "success")
    return redirect(url_for("owner.templates"))


# ---------------------------------------------------------------- reminder templates

@bp.route("/reminders", methods=["GET", "POST"])
def reminders():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        rid = request.form.get("id", type=int)
        if action == "delete":
            db.execute("DELETE FROM reminder_templates WHERE id = ? AND account_id = ?", (rid, A()))
            flash("Reminder template deleted.", "success")
        else:
            errors = []
            name = text("name", 100, required=True, label="Name", errors=errors)
            subject = text("subject", 200, required=True, label="Subject", errors=errors)
            body = text("body", 5000, required=True, label="Message", errors=errors)
            if errors:
                for e in errors:
                    flash(e, "error")
                return redirect(url_for("owner.reminders"))
            if action == "update" and rid:
                db.execute("UPDATE reminder_templates SET name=?, subject=?, body=? WHERE id=? AND account_id=?",
                           (name, subject, body, rid, A()))
                flash("Reminder template saved.", "success")
            else:
                db.execute("INSERT INTO reminder_templates (account_id, name, subject, body, created_at) "
                           "VALUES (?, ?, ?, ?, ?)", (A(), name, subject, body, now_iso()))
                flash("Reminder template added.", "success")
        db.commit()
        return redirect(url_for("owner.reminders"))
    rows = db.execute("SELECT * FROM reminder_templates WHERE account_id = ? ORDER BY id", (A(),)).fetchall()
    return render_template("reminders.html", reminders=rows, placeholders=svc.REMINDER_PLACEHOLDERS)


# ---------------------------------------------------------------- data / reset

@bp.get("/settings")
def settings():
    db = get_db()
    stats = {t: db.execute(f"SELECT COUNT(*) FROM {t} WHERE account_id = ?", (A(),)).fetchone()[0]
             for t in ("clients", "tasks", "uploads", "onboarding_templates")}
    return render_template("settings.html", stats=stats, allow_reset=current_app.config["ALLOW_RESET"],
                           data_dir=current_app.config["DATA_DIR"])


@bp.get("/settings/export.zip")
def export_all():
    """All of this account's records as a ZIP of CSV files (one per table). Works after the trial too."""
    import zipfile

    db = get_db()
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as zf:
        acc = g.account
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["name", "email", "created_at", "paid_until"])
        writer.writerow([svc.csv_safe(acc[k]) for k in ("name", "email", "created_at", "paid_until")])
        zf.writestr("account.csv", "﻿" + buf.getvalue())
        for table in svc.ACCOUNT_TABLES:
            cur = db.execute(f"SELECT * FROM {table} WHERE account_id = ? ORDER BY id", (A(),))
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow([d[0] for d in cur.description])
            for row in cur.fetchall():
                writer.writerow([svc.csv_safe(v) for v in row])
            zf.writestr(f"{table}.csv", "﻿" + buf.getvalue())
    mem.seek(0)
    return send_file(mem, mimetype="application/zip", as_attachment=True,
                     download_name=f"clientstart-export-{today()}.zip")


@bp.post("/settings/reset")
def reset():
    if not current_app.config["ALLOW_RESET"]:
        abort(403, description="Resetting data is disabled. Set CLIENTSTART_ALLOW_RESET=1 only on a demo install.")
    if request.form.get("confirm", "").strip() != "RESET":
        flash("Type RESET (in capitals) to confirm. Nothing was changed.", "error")
        return redirect(url_for("owner.settings"))
    load_demo = request.form.get("mode") != "empty"
    svc.reset_account_data(A(), load_demo=load_demo)
    flash("Your data was reset" + (" and demo data loaded." if load_demo else ". Your workspace is now empty."), "success")
    return redirect(url_for("owner.dashboard"))
