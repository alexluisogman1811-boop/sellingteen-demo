"""Freelancer-facing pages. Every route here requires a signed-in account, and every query is
limited to that account's own rows (account_id), so businesses never see each other's data."""
import csv
import io
import json

from flask import (
    Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request,
    url_for,
)

from . import services as s
from .auth import login_required
from .db import get_db
from .message_i18n import localized_message

bp = Blueprint("main", __name__)


@bp.before_request
@login_required
def require_login():
    return None


def aid():
    """The signed-in account's id: every query below is filtered by it."""
    return g.account["id"]


def actor():
    return f"freelancer:{g.account['email']}"


def get_project(project_id):
    p = get_db().execute(
        "SELECT p.*, c.name AS client_name, c.email AS client_email, c.company AS client_company"
        " FROM projects p JOIN clients c ON c.id = p.client_id WHERE p.id = ? AND p.account_id = ?",
        (project_id, aid()),
    ).fetchone()
    if p is None:
        abort(404)
    return p


CHILD_TABLES = frozenset({"deliverables", "exclusions", "milestones", "change_requests",
                          "revision_requests", "share_links", "scope_versions"})


def get_child(table, child_id, project_id):
    # Children are always looked up together with their project to block cross-project access.
    if table not in CHILD_TABLES:  # explicit check (not assert, which `python -O` strips)
        raise ValueError(f"unknown table {table!r}")
    row = get_db().execute(
        f"SELECT * FROM {table} WHERE id = ? AND project_id = ? AND account_id = ?", (child_id, project_id, aid())
    ).fetchone()
    if row is None:
        abort(404)
    return row


def errors_flash(exc):
    for e in exc.errors:
        flash(e, "error")


def to_project(project_id, anchor=None):
    return redirect(url_for("main.project", project_id=project_id, _anchor=anchor))


# ================================================================ dashboard


@bp.route("/dashboard")
def dashboard():
    db = get_db()
    projects = db.execute(
        "SELECT p.*, c.name AS client_name FROM projects p JOIN clients c ON c.id = p.client_id"
        " WHERE p.account_id = ? ORDER BY p.updated_at DESC", (aid(),)
    ).fetchall()
    active, on_hold, completed, pending, requested, drafts = [], [], [], [], [], []
    for p in projects:
        if p["status"] == "completed":
            completed.append(p)
            continue
        (active if p["status"] == "active" else on_hold).append(p)
        latest = s.latest_version(db, p["id"])
        if latest is None:
            drafts.append({"project": p, "text": "Scope not yet published"})
            continue
        decision = s.latest_decision(db, "scope_version", latest["id"])
        if decision is None:
            pending.append({"project": p, "kind": "Scope", "text": f"Version {latest['version_no']} awaiting client review"})
        elif decision["decision"] in ("changes_requested", "rejected"):
            verb = "requested changes to" if decision["decision"] == "changes_requested" else "rejected"
            requested.append({"project": p, "kind": "Scope feedback",
                              "text": f"{decision['approver_name']} {verb} v{latest['version_no']}: {decision['comment'] or ''}"})
        if s.draft_differs(db, p["id"]):
            drafts.append({"project": p, "text": f"Unpublished edits since v{latest['version_no']}"})
    for cr in db.execute(
        "SELECT cr.*, p.name AS project_name FROM change_requests cr JOIN projects p ON p.id = cr.project_id"
        " WHERE cr.account_id = ? AND cr.status IN ('draft', 'submitted', 'approved') AND p.status != 'completed'"
        " ORDER BY cr.created_at", (aid(),)
    ):
        item = {"project": {"id": cr["project_id"], "name": cr["project_name"]}, "cr": cr}
        if cr["status"] == "draft":
            drafts.append({"project": item["project"], "text": f"Draft change request “{cr['title']}” not submitted"})
        elif cr["status"] == "submitted":
            pending.append({**item, "kind": "Change request",
                            "text": f"{cr['title']} ({_hours_text(cr['estimated_hours'])}) awaiting client decision"})
        else:
            requested.append({**item, "kind": "Approved change", "text": f"{cr['title']}: approved, not yet completed"})
    for r in db.execute(
        "SELECT r.*, d.title AS deliverable, p.name AS project_name FROM revision_requests r"
        " JOIN deliverables d ON d.id = r.deliverable_id JOIN projects p ON p.id = r.project_id"
        " WHERE r.account_id = ? AND r.status = 'open' AND p.status != 'completed' ORDER BY r.created_at", (aid(),)
    ):
        requested.append({"project": {"id": r["project_id"], "name": r["project_name"]}, "kind": "Revision",
                          "text": f"{r['deliverable']}: {r['description']}"})
    return render_template(
        "dashboard.html", active=active, on_hold=on_hold, completed=completed, pending=pending,
        requested=requested, drafts=drafts,
        client_count=db.execute("SELECT COUNT(*) FROM clients WHERE account_id = ?", (aid(),)).fetchone()[0],
    )


# ================================================================ clients


def _client_form(form):
    f = s.Form(form)
    data = {
        "name": f.text("name", "Client name", max_len=120),
        "email": f.email("email", "Email"),
        "company": f.text("company", "Company", required=False, max_len=120) or None,
        "notes": f.text("notes", "Notes", required=False, max_len=2000) or None,
    }
    f.check()
    return data


def _get_client(client_id):
    client = get_db().execute("SELECT * FROM clients WHERE id = ? AND account_id = ?", (client_id, aid())).fetchone()
    if client is None:
        abort(404)
    return client


@bp.route("/clients")
def clients():
    rows = get_db().execute(
        "SELECT c.*, COUNT(p.id) AS project_count FROM clients c"
        " LEFT JOIN projects p ON p.client_id = c.id AND p.account_id = c.account_id"
        " WHERE c.account_id = ? GROUP BY c.id ORDER BY c.name COLLATE NOCASE", (aid(),)
    ).fetchall()
    return render_template("clients.html", clients=rows)


@bp.route("/clients/new", methods=["GET", "POST"])
def client_new():
    if request.method == "POST":
        try:
            d = _client_form(request.form)
        except s.ValidationError as exc:
            return render_template("client_form.html", client=request.form, errors=exc.errors), 400
        db = get_db()
        cur = db.execute(
            "INSERT INTO clients (account_id, name, email, company, notes, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (aid(), d["name"], d["email"], d["company"], d["notes"], s.now()),
        )
        db.commit()
        flash(f"Client “{d['name']}” added.", "success")
        if request.form.get("then") == "project":
            return redirect(url_for("main.project_new", client_id=cur.lastrowid))
        return redirect(url_for("main.client_detail", client_id=cur.lastrowid))
    return render_template("client_form.html", client={}, errors=[])


@bp.route("/clients/<int:client_id>")
def client_detail(client_id):
    db = get_db()
    client = _get_client(client_id)
    projects = db.execute("SELECT * FROM projects WHERE client_id = ? AND account_id = ? ORDER BY created_at DESC",
                          (client_id, aid())).fetchall()
    return render_template("client_detail.html", client=client, projects=projects)


@bp.route("/clients/<int:client_id>/edit", methods=["GET", "POST"])
def client_edit(client_id):
    db = get_db()
    client = _get_client(client_id)
    if request.method == "POST":
        try:
            d = _client_form(request.form)
        except s.ValidationError as exc:
            return render_template("client_form.html", client=request.form, editing=client, errors=exc.errors), 400
        db.execute(
            "UPDATE clients SET name = ?, email = ?, company = ?, notes = ? WHERE id = ? AND account_id = ?",
            (d["name"], d["email"], d["company"], d["notes"], client_id, aid()),
        )
        db.commit()
        flash("Client updated.", "success")
        return redirect(url_for("main.client_detail", client_id=client_id))
    return render_template("client_form.html", client=client, editing=client, errors=[])


# ================================================================ projects


def _project_form(form, client_required=True):
    f = s.Form(form)
    data = {
        "name": f.text("name", "Project name", max_len=150),
        "description": f.text("description", "Description", required=False, max_len=5000) or None,
        "start_date": f.date("start_date", "Start date"),
        "deadline": f.date("deadline", "Deadline"),
        "revision_allowance": f.number("revision_allowance", "Revisions per deliverable",
                                       required=True, maximum=100, integer=True),
        "hourly_rate": f.number("hourly_rate", "Hourly rate", maximum=100_000),
        "currency": (f.text("currency", "Currency", max_len=3) or "USD").upper(),
    }
    if client_required:
        data["client_id"] = f.number("client_id", "Client", required=True, minimum=1, maximum=10**9, integer=True)
    if data["start_date"] and data["deadline"] and data["deadline"] < data["start_date"]:
        f.errors.append("Deadline cannot be before the start date.")
    f.check()
    return data


@bp.route("/projects/new", methods=["GET", "POST"])
def project_new():
    db = get_db()
    clients_list = db.execute("SELECT id, name FROM clients WHERE account_id = ? ORDER BY name COLLATE NOCASE",
                              (aid(),)).fetchall()
    if not clients_list:
        flash("Add a client first; every project belongs to one client.", "info")
        return redirect(url_for("main.client_new"))
    if request.method == "POST":
        try:
            d = _project_form(request.form)
            if not db.execute("SELECT 1 FROM clients WHERE id = ? AND account_id = ?", (d["client_id"], aid())).fetchone():
                raise s.ValidationError(["Choose an existing client."])
        except s.ValidationError as exc:
            return render_template("project_form.html", project=request.form, clients=clients_list,
                                   errors=exc.errors), 400
        ts = s.now()
        cur = db.execute(
            "INSERT INTO projects (account_id, client_id, name, description, start_date, deadline, revision_allowance,"
            " hourly_rate, currency, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (aid(), d["client_id"], d["name"], d["description"], d["start_date"], d["deadline"],
             d["revision_allowance"], d["hourly_rate"], d["currency"], ts, ts),
        )
        s.log_event(db, cur.lastrowid, "project_created", f"Project “{d['name']}” created.", actor())
        db.commit()
        flash("Project created. Now add deliverables, exclusions and milestones, then publish scope version 1.", "success")
        return to_project(cur.lastrowid, "scope")
    preset = {"client_id": request.args.get("client_id", type=int), "revision_allowance": 2, "currency": "USD"}
    return render_template("project_form.html", project=preset, clients=clients_list, errors=[])


@bp.route("/projects/<int:project_id>")
def project(project_id):
    db = get_db()
    p = get_project(project_id)
    versions = db.execute(
        "SELECT * FROM scope_versions WHERE project_id = ? AND account_id = ? ORDER BY version_no DESC",
        (project_id, aid())
    ).fetchall()
    latest = versions[0] if versions else None
    latest_dec = s.latest_decision(db, "scope_version", latest["id"]) if latest else None
    approved_v, approved_dec = s.approved_version(db, project_id)
    version_decisions = {v["id"]: s.latest_decision(db, "scope_version", v["id"]) for v in versions}
    draft_diff = s.diff_scopes(s.snapshot(latest), s.current_scope(db, project_id)) if latest else None
    # Each query takes the project id and the account id (defence in depth: the project was already
    # checked to belong to this account).
    q = lambda sql: db.execute(sql, (project_id, aid())).fetchall()  # noqa: E731
    return render_template(
        "project.html",
        p=p,
        deliverables=q("SELECT * FROM deliverables WHERE project_id = ? AND account_id = ? AND archived_at IS NULL ORDER BY id"),
        archived_deliverables=q("SELECT * FROM deliverables WHERE project_id = ? AND account_id = ? AND archived_at IS NOT NULL ORDER BY id"),
        exclusions=q("SELECT * FROM exclusions WHERE project_id = ? AND account_id = ? AND archived_at IS NULL ORDER BY id"),
        milestones=q("SELECT * FROM milestones WHERE project_id = ? AND account_id = ? AND archived_at IS NULL"
                     " ORDER BY due_date IS NULL, due_date, id"),
        versions=versions,
        latest=latest,
        latest_dec=latest_dec,
        version_decisions=version_decisions,
        draft_differs=s.draft_differs(db, project_id),
        draft_diff=draft_diff,
        changes=q("SELECT * FROM change_requests WHERE project_id = ? AND account_id = ? ORDER BY created_at DESC"),
        crs=s.group_change_requests(db, project_id),
        baseline=s.scope_baseline(db, project_id),
        approved_v=approved_v,
        approved_dec=approved_dec,
        revisions=q("SELECT r.*, d.title AS deliverable FROM revision_requests r JOIN deliverables d"
                    " ON d.id = r.deliverable_id WHERE r.project_id = ? AND r.account_id = ? ORDER BY r.created_at DESC"),
        usage=s.revision_usage(db, p),
        links=q("SELECT * FROM share_links WHERE project_id = ? AND account_id = ? ORDER BY created_at DESC"),
        approvals=q("SELECT a.*, v.version_no, cr.title AS cr_title FROM approvals a"
                    " LEFT JOIN scope_versions v ON v.id = a.scope_version_id"
                    " LEFT JOIN change_requests cr ON cr.id = a.change_request_id"
                    " WHERE a.project_id = ? AND a.account_id = ? ORDER BY a.id DESC"),
        templates=db.execute("SELECT id, name FROM message_templates WHERE account_id = ? ORDER BY name",
                             (aid(),)).fetchall(),
        now=s.now(),
    )


@bp.route("/projects/<int:project_id>/edit", methods=["GET", "POST"])
def project_edit(project_id):
    p = get_project(project_id)
    if request.method == "POST":
        try:
            d = _project_form(request.form, client_required=False)
        except s.ValidationError as exc:
            return render_template("project_form.html", project=request.form, editing=p, errors=exc.errors), 400
        db = get_db()
        changes = s.describe_changes(dict(p), d, TERM_LABELS)
        db.execute(
            "UPDATE projects SET name = ?, description = ?, start_date = ?, deadline = ?, revision_allowance = ?,"
            " hourly_rate = ?, currency = ?, updated_at = ? WHERE id = ? AND account_id = ?",
            (d["name"], d["description"], d["start_date"], d["deadline"], d["revision_allowance"],
             d["hourly_rate"], d["currency"], s.now(), project_id, aid()),
        )
        s.log_event(db, project_id, "terms_edited",
                    f"Draft terms edited: {changes or 'no changes'}. Published versions unchanged.", actor())
        db.commit()
        flash("Project terms updated in the draft. Published versions are unchanged.", "success")
        return to_project(project_id, "scope")
    return render_template("project_form.html", project=p, editing=p, errors=[])


def _open_items(db, project_id):
    count = lambda sql: db.execute(sql, (project_id, aid())).fetchone()[0]  # noqa: E731
    items = []
    proposed = count("SELECT COUNT(*) FROM change_requests WHERE project_id = ? AND account_id = ?"
                     " AND status IN ('draft', 'submitted')")
    approved = count("SELECT COUNT(*) FROM change_requests WHERE project_id = ? AND account_id = ? AND status = 'approved'")
    revisions = count("SELECT COUNT(*) FROM revision_requests WHERE project_id = ? AND account_id = ? AND status = 'open'")
    if proposed:
        items.append(f"{proposed} change request(s) in draft or awaiting a client decision")
    if approved:
        items.append(f"{approved} approved change request(s) not marked completed")
    if revisions:
        items.append(f"{revisions} open revision request(s)")
    latest = s.latest_version(db, project_id)
    decision = s.latest_decision(db, "scope_version", latest["id"]) if latest else None
    if decision is None or decision["decision"] != "approved":
        items.append("the latest scope version has no recorded client approval")
    return items


@bp.route("/projects/<int:project_id>/status", methods=["POST"])
def project_status(project_id):
    p = get_project(project_id)
    status = request.form.get("status")
    if status not in s.PROJECT_STATUSES:
        abort(400)
    if status == p["status"]:
        flash(f"Project is already {status.replace('_', ' ')}.", "info")
        return to_project(project_id)
    db = get_db()
    if status == "completed":
        open_items = _open_items(db, project_id)
        if open_items:
            flash("Marked completed, but note: " + "; ".join(open_items) + ".", "warning")
    db.execute("UPDATE projects SET status = ?, updated_at = ? WHERE id = ? AND account_id = ?",
               (status, s.now(), project_id, aid()))
    s.log_event(db, project_id, "project_status", f"Project status: {p['status']} → {status}.", actor())
    db.commit()
    flash(f"Project marked {status.replace('_', ' ')}.", "success")
    return to_project(project_id)


# ---------------------------------------------------------------- scope items (draft)


def _bump(db, project_id):
    db.execute("UPDATE projects SET updated_at = ? WHERE id = ? AND account_id = ?", (s.now(), project_id, aid()))


def _touch(db, project_id, kind, message):
    _bump(db, project_id)
    s.log_event(db, project_id, kind, message, actor())
    db.commit()


TERM_LABELS = {"name": "Name", "description": "Description", "start_date": "Start date", "deadline": "Deadline",
               "revision_allowance": "Revisions per deliverable", "hourly_rate": "Hourly rate", "currency": "Currency"}
DELIVERABLE_LABELS = {"title": "Title", "description": "Details", "due_date": "Due date",
                      "revision_allowance": "Revision allowance"}


def _deliverable_form(form):
    f = s.Form(form)
    d = {
        "title": f.text("title", "Deliverable", max_len=200),
        "description": f.text("description", "Details", required=False, max_len=3000) or None,
        "revision_allowance": f.number("revision_allowance", "Revision allowance", maximum=100, integer=True),
        "due_date": f.date("due_date", "Due date"),
    }
    f.check()
    return d


@bp.route("/projects/<int:project_id>/deliverables", methods=["POST"])
def deliverable_add(project_id):
    get_project(project_id)
    try:
        d = _deliverable_form(request.form)
    except s.ValidationError as exc:
        errors_flash(exc)
        return to_project(project_id, "scope")
    db = get_db()
    db.execute(
        "INSERT INTO deliverables (account_id, project_id, title, description, revision_allowance, due_date, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (aid(), project_id, d["title"], d["description"], d["revision_allowance"], d["due_date"], s.now()),
    )
    _touch(db, project_id, "draft_edit", f"Draft: added deliverable “{d['title']}”.")
    return to_project(project_id, "scope")


@bp.route("/projects/<int:project_id>/deliverables/<int:item_id>/edit", methods=["GET", "POST"])
def deliverable_edit(project_id, item_id):
    p = get_project(project_id)
    item = get_child("deliverables", item_id, project_id)
    if request.method == "POST":
        try:
            d = _deliverable_form(request.form)
        except s.ValidationError as exc:
            return render_template("deliverable_form.html", p=p, item=request.form, errors=exc.errors), 400
        db = get_db()
        db.execute(
            "UPDATE deliverables SET title = ?, description = ?, revision_allowance = ?, due_date = ?"
            " WHERE id = ? AND account_id = ?",
            (d["title"], d["description"], d["revision_allowance"], d["due_date"], item_id, aid()),
        )
        changes = s.describe_changes(dict(item), d, DELIVERABLE_LABELS) or "no changes"
        _touch(db, project_id, "draft_edit", f"Draft: edited deliverable “{item['title']}”: {changes}.")
        flash("Deliverable updated in the draft. Earlier published versions keep their original wording.", "success")
        return to_project(project_id, "scope")
    return render_template("deliverable_form.html", p=p, item=item, errors=[])


ITEM_TABLES = {"deliverables": "title", "exclusions": "text", "milestones": "title"}


@bp.route("/projects/<int:project_id>/<kind>/<int:item_id>/archive", methods=["POST"])
def item_archive(project_id, kind, item_id):
    if kind not in ITEM_TABLES:
        abort(404)
    get_project(project_id)
    item = get_child(kind, item_id, project_id)
    db = get_db()
    restore = request.form.get("restore") == "1"
    db.execute(f"UPDATE {kind} SET archived_at = ? WHERE id = ? AND account_id = ?",
               (None if restore else s.now(), item_id, aid()))
    verb = "restored" if restore else "removed"
    _touch(db, project_id, "draft_edit", f"Draft: {verb} {kind[:-1]} “{item[ITEM_TABLES[kind]]}”.")
    return to_project(project_id, "scope")


@bp.route("/projects/<int:project_id>/exclusions", methods=["POST"])
def exclusion_add(project_id):
    get_project(project_id)
    f = s.Form(request.form)
    text = f.text("text", "Exclusion", max_len=500)
    if f.errors:
        errors_flash(s.ValidationError(f.errors))
        return to_project(project_id, "scope")
    db = get_db()
    db.execute("INSERT INTO exclusions (account_id, project_id, text, created_at) VALUES (?, ?, ?, ?)",
               (aid(), project_id, text, s.now()))
    _touch(db, project_id, "draft_edit", f"Draft: added exclusion “{text}”.")
    return to_project(project_id, "scope")


@bp.route("/projects/<int:project_id>/milestones", methods=["POST"])
def milestone_add(project_id):
    get_project(project_id)
    f = s.Form(request.form)
    title, due = f.text("title", "Milestone", max_len=200), f.date("due_date", "Due date")
    if f.errors:
        errors_flash(s.ValidationError(f.errors))
        return to_project(project_id, "scope")
    db = get_db()
    db.execute("INSERT INTO milestones (account_id, project_id, title, due_date, created_at) VALUES (?, ?, ?, ?, ?)",
               (aid(), project_id, title, due, s.now()))
    _touch(db, project_id, "draft_edit", f"Draft: added milestone “{title}”.")
    return to_project(project_id, "scope")


@bp.route("/projects/<int:project_id>/milestones/<int:item_id>/toggle", methods=["POST"])
def milestone_toggle(project_id, item_id):
    get_project(project_id)
    m = get_child("milestones", item_id, project_id)
    db = get_db()
    db.execute("UPDATE milestones SET done = ? WHERE id = ? AND account_id = ?", (0 if m["done"] else 1, item_id, aid()))
    _touch(db, project_id, "milestone", f"Milestone “{m['title']}” marked {'open' if m['done'] else 'done'}.")
    return to_project(project_id, "scope")


# ---------------------------------------------------------------- versions


@bp.route("/projects/<int:project_id>/versions", methods=["POST"])
def version_publish(project_id):
    get_project(project_id)
    db = get_db()
    note = (request.form.get("change_note") or "").strip()[:1000]
    try:
        no = s.publish_version(db, project_id, note, actor())
    except s.ValidationError as exc:
        errors_flash(exc)
        return to_project(project_id, "scope")
    _bump(db, project_id)
    db.commit()
    flash(f"Scope version {no} published. Earlier versions are preserved unchanged.", "success")
    return redirect(url_for("main.version_detail", project_id=project_id, version_no=no))


def _get_version(project_id, version_no):
    v = get_db().execute(
        "SELECT * FROM scope_versions WHERE project_id = ? AND account_id = ? AND version_no = ?",
        (project_id, aid(), version_no),
    ).fetchone()
    if v is None:
        abort(404)
    return v


@bp.route("/projects/<int:project_id>/versions/<int:version_no>")
def version_detail(project_id, version_no):
    db = get_db()
    p = get_project(project_id)
    v = _get_version(project_id, version_no)
    prev = db.execute(
        "SELECT * FROM scope_versions WHERE project_id = ? AND account_id = ? AND version_no = ?",
        (project_id, aid(), version_no - 1),
    ).fetchone()
    return render_template(
        "version.html", p=p, v=v, scope=s.snapshot(v),
        diff=s.diff_scopes(s.snapshot(prev), s.snapshot(v)),
        decisions=db.execute("SELECT * FROM approvals WHERE scope_version_id = ? AND account_id = ? ORDER BY id DESC",
                             (v["id"], aid())).fetchall(),
        is_latest=s.latest_version(db, project_id)["id"] == v["id"],
    )


@bp.route("/projects/<int:project_id>/versions/<int:version_no>/record", methods=["GET", "POST"])
def version_record(project_id, version_no):
    p = get_project(project_id)
    v = _get_version(project_id, version_no)
    errors = []
    if request.method == "POST":
        f = s.Form(request.form)
        decision = f.choice("decision", "Decision", ("approved", "changes_requested", "rejected"))
        name = f.text("approver_name", "Who made the decision", max_len=120)
        email = f.email("approver_email", "Their email")
        evidence = f.text("evidence_note", "Evidence", max_len=2000)
        comment = f.text("comment", "Comment", required=False, max_len=2000) or None
        if request.form.get("confirm") != "1":
            f.errors.append("Confirm that you are recording a real decision made by the client.")
        try:
            f.check()
            db = get_db()
            s.record_decision(
                db, project_id=project_id, subject_type="scope_version", subject_id=v["id"], decision=decision,
                approver_name=name, approver_email=email, method="recorded_by_freelancer",
                evidence_note=evidence, comment=comment,
            )
            s.log_event(db, project_id, "decision_recorded",
                        f"Recorded off-platform decision on scope v{version_no}: "
                        f"{s.DECISION_LABELS[decision]} by {name}.", actor())
            db.commit()
        except s.ValidationError as exc:
            errors = exc.errors
        else:
            flash("Decision recorded and labelled as recorded by you on the client's behalf.", "success")
            return redirect(url_for("main.version_detail", project_id=project_id, version_no=version_no))
    return render_template("record_decision.html", p=p, subject=f"scope version {version_no}",
                           allow_changes=True, form=request.form, errors=errors)


# ---------------------------------------------------------------- change requests


@bp.route("/projects/<int:project_id>/changes/new", methods=["GET", "POST"])
def change_new(project_id):
    p = get_project(project_id)
    if request.method == "POST":
        try:
            d = _change_form(request.form)
        except s.ValidationError as exc:
            return render_template("change_form.html", p=p, form=request.form, errors=exc.errors), 400
        submit = request.form.get("action") == "submit"
        db = get_db()
        ts = s.now()
        cur = db.execute(
            "INSERT INTO change_requests (account_id, project_id, title, description, reason, estimated_hours,"
            " proposed_cost, status, created_at, updated_at, submitted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (aid(), project_id, d["title"], d["description"], d["reason"], d["estimated_hours"], d["proposed_cost"],
             "submitted" if submit else "draft", ts, ts, ts if submit else None),
        )
        s.log_event(db, project_id, "change_request_created",
                    f"Change request #{cur.lastrowid} “{d['title']}” {'created and submitted to the client' if submit else 'saved as draft'}"
                    f" (time: {_hours_text(d['estimated_hours'])}; price: {s.money(d['proposed_cost'], p['currency'])}).",
                    actor())
        _bump(db, project_id)
        db.commit()
        flash("Change request submitted. The client can now approve or reject it on their review link."
              if submit else "Draft saved. The client can't see it until you submit it.", "success")
        return redirect(url_for("main.change_detail", project_id=project_id, cr_id=cur.lastrowid))
    prefill = {"title": request.args.get("title", "")[:200], "description": request.args.get("description", "")[:5000]}
    if p["hourly_rate"]:
        prefill["hint_rate"] = p["hourly_rate"]
    return render_template("change_form.html", p=p, form=prefill, errors=[])


CR_LABELS_FIELDS = {"title": "Title", "description": "Description", "reason": "Reason",
                    "estimated_hours": "Time impact (hours)", "proposed_cost": "Price impact"}


def _hours_text(hours):
    return "not estimated" if hours is None else f"{hours:g} h"


def _change_form(form):
    f = s.Form(form)
    d = {
        "title": f.text("title", "Title", max_len=200),
        "description": f.text("description", "What will change", max_len=5000),
        "reason": f.text("reason", "Reason for the change", max_len=3000),
        # Optional: left empty means "not estimated". ScopeGuard never fills these in.
        "estimated_hours": f.number("estimated_hours", "Time impact (extra hours)", maximum=10_000),
        "proposed_cost": f.number("proposed_cost", "Price impact", maximum=10_000_000),
    }
    f.check()
    return d


@bp.route("/projects/<int:project_id>/changes/<int:cr_id>/edit", methods=["GET", "POST"])
def change_edit(project_id, cr_id):
    p = get_project(project_id)
    cr = get_child("change_requests", cr_id, project_id)
    if cr["status"] != "draft":
        flash("Only drafts can be edited. A submitted request is what the client sees and decides on, so it is "
              "never changed. Withdraw it and create a new one instead.", "error")
        return redirect(url_for("main.change_detail", project_id=project_id, cr_id=cr_id))
    if request.method == "POST":
        try:
            d = _change_form(request.form)
        except s.ValidationError as exc:
            return render_template("change_form.html", p=p, form=request.form, errors=exc.errors, editing=cr), 400
        db = get_db()
        changes = s.describe_changes(dict(cr), d, CR_LABELS_FIELDS) or "no changes"
        cur = db.execute(
            "UPDATE change_requests SET title = ?, description = ?, reason = ?, estimated_hours = ?,"
            " proposed_cost = ?, updated_at = ? WHERE id = ? AND account_id = ? AND status = 'draft'",
            (d["title"], d["description"], d["reason"], d["estimated_hours"], d["proposed_cost"], s.now(), cr_id, aid()),
        )
        if cur.rowcount == 1:
            s.log_event(db, project_id, "change_request_edited", f"Draft change request #{cr_id} “{cr['title']}” edited: {changes}.",
                        actor())
            db.commit()
            flash("Draft updated.", "success")
        else:
            db.rollback()
            flash("This request is no longer a draft, so it was not changed.", "error")
        return redirect(url_for("main.change_detail", project_id=project_id, cr_id=cr_id))
    return render_template("change_form.html", p=p, form=cr, errors=[], editing=cr)


@bp.route("/projects/<int:project_id>/changes/<int:cr_id>")
def change_detail(project_id, cr_id):
    p = get_project(project_id)
    cr = get_child("change_requests", cr_id, project_id)
    db = get_db()
    decisions = db.execute(
        "SELECT * FROM approvals WHERE change_request_id = ? AND account_id = ? ORDER BY id DESC", (cr_id, aid())
    ).fetchall()
    events = db.execute(
        "SELECT * FROM events WHERE project_id = ? AND account_id = ? AND message LIKE ? ORDER BY id DESC",
        (project_id, aid(), f"%change request #{cr_id} “%"),
    ).fetchall()
    own = [t for t in s.CR_TRANSITIONS[cr["status"]] if (cr["status"], t) in s.FREELANCER_TRANSITIONS]
    return render_template("change_detail.html", p=p, cr=cr, decisions=decisions, events=events,
                           own_transitions=own, can_record_client=cr["status"] == "submitted")


@bp.route("/projects/<int:project_id>/changes/<int:cr_id>/status", methods=["POST"])
def change_status(project_id, cr_id):
    """The freelancer's own actions: submit, withdraw, mark completed. Never approve or reject."""
    get_project(project_id)
    cr = get_child("change_requests", cr_id, project_id)
    db = get_db()
    new_status = request.form.get("status", "")
    try:
        if new_status in s.CLIENT_DECISIONS:
            raise s.ValidationError([
                "Only the client can approve or reject a change request: they decide on their review link, "
                "or you record the decision they gave you elsewhere, with evidence."
            ])
        if (cr["status"], new_status) not in s.FREELANCER_TRANSITIONS:
            raise s.ValidationError([f"A {cr['status']} change request cannot become “{new_status}”."])
        s.change_cr_status(db, cr, new_status, actor())
        _bump(db, project_id)
        db.commit()
    except s.ValidationError as exc:
        db.rollback()
        errors_flash(exc)
    else:
        flash({"submitted": "Submitted. The client can now approve or reject it on their review link.",
               "withdrawn": "Withdrawn. It stays in the history and can no longer be approved.",
               "completed": "Marked completed."}[new_status], "success")
    return redirect(url_for("main.change_detail", project_id=project_id, cr_id=cr_id))


@bp.route("/projects/<int:project_id>/changes/<int:cr_id>/record", methods=["POST"])
def change_record_decision(project_id, cr_id):
    """Record a decision the client gave outside ScopeGuard, labelled as such, with evidence."""
    get_project(project_id)
    cr = get_child("change_requests", cr_id, project_id)
    db = get_db()
    f = s.Form(request.form)
    decision = f.choice("decision", "Decision", s.CLIENT_DECISIONS)
    name = f.text("approver_name", "Who made the decision", max_len=120)
    email = f.email("approver_email", "Their email")
    evidence = f.text("evidence_note", "Evidence", max_len=2000)
    if request.form.get("confirm") != "1":
        f.errors.append("Confirm that you are recording a real decision made by the client.")
    try:
        f.check()
        if cr["status"] != "submitted":
            raise s.ValidationError(["Only a submitted change request can be approved or rejected."])
        s.record_decision(
            db, project_id=project_id, subject_type="change_request", subject_id=cr_id, decision=decision,
            approver_name=name, approver_email=email, method="recorded_by_freelancer", evidence_note=evidence,
        )
        s.change_cr_status(db, cr, decision, actor())
        s.log_event(db, project_id, "change_request_decision",
                    f"Recorded off-platform client decision on change request #{cr_id} “{cr['title']}”: {s.DECISION_LABELS[decision]} "
                    f"by {name}.", actor())
        db.commit()
    except s.ValidationError as exc:
        db.rollback()
        errors_flash(exc)
    else:
        flash("Client decision recorded and labelled as recorded by you on the client's behalf.", "success")
    return redirect(url_for("main.change_detail", project_id=project_id, cr_id=cr_id))


# ---------------------------------------------------------------- revisions


@bp.route("/projects/<int:project_id>/revisions", methods=["POST"])
def revision_add(project_id):
    p = get_project(project_id)
    f = s.Form(request.form)
    did = f.number("deliverable_id", "Deliverable", required=True, minimum=1, maximum=10**9, integer=True)
    desc = f.text("description", "What was requested", max_len=3000)
    by = f.text("requested_by", "Requested by", required=False, max_len=120) or None
    if f.errors:
        errors_flash(s.ValidationError(f.errors))
        return to_project(project_id, "revisions")
    deliverable = get_child("deliverables", did, project_id)
    db = get_db()
    db.execute(
        "INSERT INTO revision_requests (account_id, project_id, deliverable_id, description, requested_by, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (aid(), project_id, did, desc, by, s.now()),
    )
    usage = s.revision_usage(db, p)[did]
    _touch(db, project_id, "revision_logged",
           f"Revision {usage['used']} of {usage['allowance']} logged for “{deliverable['title']}”.")
    if usage["over"]:
        flash(f"“{deliverable['title']}” has now had {usage['used']} revisions against an allowance of "
              f"{usage['allowance']}. Consider a change request for further work.", "warning")
    else:
        flash("Revision request logged.", "success")
    return to_project(project_id, "revisions")


@bp.route("/projects/<int:project_id>/revisions/<int:rev_id>/done", methods=["POST"])
def revision_done(project_id, rev_id):
    get_project(project_id)
    r = get_child("revision_requests", rev_id, project_id)
    db = get_db()
    reopen = r["status"] == "done"
    db.execute("UPDATE revision_requests SET status = ?, completed_at = ? WHERE id = ? AND account_id = ?",
               ("open" if reopen else "done", None if reopen else s.now(), rev_id, aid()))
    _touch(db, project_id, "revision_status", f"Revision #{rev_id} marked {'open' if reopen else 'done'}.")
    return to_project(project_id, "revisions")


# ---------------------------------------------------------------- client review links


@bp.route("/projects/<int:project_id>/links", methods=["POST"])
def link_create(project_id):
    p = get_project(project_id)
    db = get_db()
    if s.latest_version(db, project_id) is None:
        flash("Publish a scope version before sharing it with the client.", "error")
        return to_project(project_id, "share")
    f = s.Form(request.form)
    label = f.text("label", "Label", required=False, max_len=80)
    days = f.number("days", "Valid for (days)", required=True, minimum=1, maximum=180, integer=True)
    recipient = f.text("recipient_name", "Person this link is for", max_len=120)
    recipient_email = f.email("recipient_email", "Their email")
    can_decide = request.form.get("can_decide") == "1"
    if f.errors:
        errors_flash(s.ValidationError(f.errors))
        return to_project(project_id, "share")
    token = s.create_share_link(db, project_id, label, days, actor(), recipient=recipient,
                                recipient_email=recipient_email, can_decide=can_decide)
    db.commit()
    base = current_app.config["BASE_URL"]
    path = url_for("portal.review", token=token)  # already includes any mount prefix, e.g. /scopeguard
    if base and request.script_root and base.endswith(request.script_root):
        base = base[: -len(request.script_root)]  # BASE_URL may be given with or without the prefix
    url = base + path if base else url_for("portal.review", token=token, _external=True)
    local_only = not base and request.host.rsplit(":", 1)[0].strip("[]") in ("127.0.0.1", "localhost", "::1")
    tmpl = db.execute("SELECT * FROM message_templates WHERE account_id = ? ORDER BY id", (aid(),)).fetchall()
    ctx = s.message_context(db, p, g.account["name"], review_link=url)
    rendered = []
    for t in tmpl:
        if "{review_link}" in t["body"]:
            subject, body = localized_message(t, ctx, s.render_text)  # Thai only for unedited built-ins
            rendered.append({"name": t["name"], "subject": subject, "body": body})
    return render_template("link_created.html", p=p, url=url, days=days, rendered=rendered, local_only=local_only,
                           recipient=recipient, can_decide=can_decide)


@bp.route("/projects/<int:project_id>/links/<int:link_id>/revoke", methods=["POST"])
def link_revoke(project_id, link_id):
    get_project(project_id)
    link = get_child("share_links", link_id, project_id)
    if link["revoked_at"] is None:
        db = get_db()
        db.execute("UPDATE share_links SET revoked_at = ? WHERE id = ? AND account_id = ?", (s.now(), link_id, aid()))
        _touch(db, project_id, "link_revoked", f"Revoked client review link #{link_id}.")
        flash("Link revoked. Anyone using it will no longer have access.", "success")
    return to_project(project_id, "share")


# ---------------------------------------------------------------- history, summary, exports


def _project_events(project_id):
    return get_db().execute(
        "SELECT * FROM events WHERE project_id = ? AND account_id = ? ORDER BY id DESC", (project_id, aid())
    ).fetchall()


@bp.route("/projects/<int:project_id>/history")
def history(project_id):
    return render_template("history.html", p=get_project(project_id), events=_project_events(project_id))


def project_export_data(project_id):
    db = get_db()
    p = get_project(project_id)
    rows = lambda sql: [dict(r) for r in db.execute(sql, (project_id, aid()))]  # noqa: E731
    versions = rows("SELECT * FROM scope_versions WHERE project_id = ? AND account_id = ? ORDER BY version_no")
    for v in versions:
        v["snapshot"] = json.loads(v.pop("snapshot_json"))
    approvals = rows("SELECT * FROM approvals WHERE project_id = ? AND account_id = ? ORDER BY id")
    for a in approvals:
        a["method_label"] = s.METHOD_LABELS[a["method"]]
    return {
        "exported_at": s.now(),
        "notice": "ScopeGuard records document what was agreed; they are not a guarantee of legal enforceability.",
        "project": {k: p[k] for k in p.keys()},
        "current_draft_scope": s.current_scope(db, project_id),
        "scope_versions": versions,
        "change_requests": rows("SELECT * FROM change_requests WHERE project_id = ? AND account_id = ? ORDER BY id"),
        "revision_requests": rows("SELECT * FROM revision_requests WHERE project_id = ? AND account_id = ? ORDER BY id"),
        "approvals": approvals,
        "share_links": rows("SELECT id, label, created_at, expires_at, revoked_at, last_used_at"
                            " FROM share_links WHERE project_id = ? AND account_id = ? ORDER BY id"),
        "history": rows("SELECT * FROM events WHERE project_id = ? AND account_id = ? ORDER BY id"),
    }


@bp.route("/projects/<int:project_id>/summary")
def summary(project_id):
    db = get_db()
    p = get_project(project_id)
    latest = s.latest_version(db, project_id)
    approved_v, approved_dec = s.approved_version(db, project_id)
    shown = approved_v or latest
    versions = db.execute("SELECT * FROM scope_versions WHERE project_id = ? AND account_id = ? ORDER BY version_no",
                          (project_id, aid())).fetchall()
    return render_template(
        "summary.html", p=p, latest=latest, approved_v=approved_v, approved_dec=approved_dec,
        latest_dec=s.latest_decision(db, "scope_version", latest["id"]) if latest else None,
        shown=shown, scope=s.snapshot(shown),
        versions=[(v, s.latest_decision(db, "scope_version", v["id"])) for v in versions],
        crs=s.group_change_requests(db, project_id), open_items=_open_items(db, project_id),
        baseline=s.scope_baseline(db, project_id),
        data=project_export_data(project_id), usage=s.revision_usage(db, p, s.snapshot(shown)),
        freelancer=g.account["name"], generated=s.now(),
    )


def _slug(text):
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")[:40] or "project"


@bp.route("/projects/<int:project_id>/export.json")
def export_json(project_id):
    data = project_export_data(project_id)
    name = f"scopeguard-{_slug(data['project']['name'])}.json"
    return Response(json.dumps(data, indent=2, ensure_ascii=False), mimetype="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


CSV_KINDS = {
    "change_requests": ["id", "title", "description", "reason", "estimated_hours", "proposed_cost", "status",
                        "created_at", "submitted_at", "updated_at", "completed_at"],
    "revision_requests": ["id", "deliverable_id", "description", "requested_by", "status", "created_at", "completed_at"],
    "approvals": ["id", "subject_type", "scope_version_id", "change_request_id", "decision", "approver_name",
                  "approver_email", "method", "method_label", "evidence_note", "comment", "ip_address", "created_at"],
    "scope_versions": ["id", "version_no", "change_note", "created_by", "created_at", "deliverables", "exclusions",
                       "milestones"],
    "history": ["id", "kind", "message", "actor", "created_at"],
}


def _csv_safe(value):
    # Neutralise spreadsheet formula injection.
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


@bp.route("/projects/<int:project_id>/export/<kind>.csv")
def export_csv(project_id, kind):
    if kind not in CSV_KINDS:
        abort(404)
    data = project_export_data(project_id)
    records = data[kind]
    if kind == "scope_versions":
        records = [
            {**v, **{sec: " | ".join(i.get("title") or i.get("text") for i in v["snapshot"][sec])
                     for sec in ("deliverables", "exclusions", "milestones")}}
            for v in records
        ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_KINDS[kind], extrasaction="ignore")
    writer.writeheader()
    for r in records:
        writer.writerow({k: _csv_safe(r.get(k)) for k in CSV_KINDS[kind]})
    name = f"scopeguard-{_slug(data['project']['name'])}-{kind}.csv"
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@bp.route("/export/all.json")
def export_all():
    db = get_db()
    data = {
        "exported_at": s.now(),
        "account": {"name": g.account["name"], "email": g.account["email"]},
        "clients": [dict(r) for r in db.execute("SELECT * FROM clients WHERE account_id = ? ORDER BY id", (aid(),))],
        "projects": [project_export_data(r["id"])
                     for r in db.execute("SELECT id FROM projects WHERE account_id = ? ORDER BY id", (aid(),))],
        "message_templates": [dict(r) for r in db.execute(
            "SELECT * FROM message_templates WHERE account_id = ? ORDER BY id", (aid(),))],
    }
    return Response(json.dumps(data, indent=2, ensure_ascii=False), mimetype="application/json",
                    headers={"Content-Disposition": 'attachment; filename="scopeguard-all.json"'})


# Columns that are never exported (secrets and internal markers).
_EXPORT_SKIP = {"token_hash"}
ZIP_TABLES = ("clients", "projects", "deliverables", "exclusions", "milestones", "scope_versions",
              "change_requests", "revision_requests", "share_links", "approvals", "events", "message_templates")


@bp.route("/export/all.zip")
def export_zip():
    """Everything this account has stored, as a ZIP of CSV files (never the password or link tokens)."""
    import zipfile
    from datetime import date

    db = get_db()
    files = {}
    account = db.execute("SELECT name, email, created_at, paid_until FROM accounts WHERE id = ?", (aid(),)).fetchone()
    files["account.csv"] = [list(account.keys()), [list(account)]]
    files["settings.csv"] = [["key", "value"],
                             [list(r) for r in db.execute("SELECT key, value FROM settings WHERE account_id = ?"
                                                          " ORDER BY key", (aid(),))]]
    for table in ZIP_TABLES:
        cur = db.execute(f"SELECT * FROM {table} WHERE account_id = ? ORDER BY id", (aid(),))
        cols = [c[0] for c in cur.description if c[0] not in _EXPORT_SKIP]
        files[f"{table}.csv"] = [cols, [[r[c] for c in cols] for r in cur.fetchall()]]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, (cols, rows) in files.items():
            out = io.StringIO()
            writer = csv.writer(out)
            writer.writerow(cols)
            for row in rows:
                writer.writerow([_csv_safe(v) for v in row])
            zf.writestr(name, out.getvalue().encode("utf-8-sig"))  # BOM so Excel opens it correctly
    filename = f"scopeguard-export-{date.today().isoformat()}.zip"
    return Response(buf.getvalue(), mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ================================================================ message templates


@bp.route("/projects/<int:project_id>/compose")
def compose(project_id):
    db = get_db()
    p = get_project(project_id)
    templates = db.execute("SELECT * FROM message_templates WHERE account_id = ? ORDER BY name", (aid(),)).fetchall()
    if not templates:
        flash("Create a message template first.", "info")
        return redirect(url_for("main.templates"))
    tid = request.args.get("template_id", type=int)
    t = next((t for t in templates if t["id"] == tid), templates[0])
    cr_id = request.args.get("cr_id", type=int)
    cr = get_child("change_requests", cr_id, project_id) if cr_id else None
    changes = db.execute("SELECT id, title FROM change_requests WHERE project_id = ? AND account_id = ? ORDER BY id DESC",
                         (project_id, aid())).fetchall()
    ctx = s.message_context(db, p, g.account["name"], change_request=cr)
    subject, body = localized_message(t, ctx, s.render_text)  # Thai only for unedited built-ins
    return render_template(
        "compose.html", p=p, templates=templates, t=t, cr=cr, changes=changes, subject=subject, body=body,
    )


@bp.route("/templates")
def templates():
    rows = get_db().execute("SELECT * FROM message_templates WHERE account_id = ? ORDER BY name", (aid(),)).fetchall()
    return render_template("templates_list.html", templates=rows, placeholders=s.PLACEHOLDERS)


@bp.route("/templates/new", methods=["GET", "POST"])
@bp.route("/templates/<int:template_id>/edit", methods=["GET", "POST"])
def template_edit(template_id=None):
    db = get_db()
    t = None
    if template_id is not None:
        t = db.execute("SELECT * FROM message_templates WHERE id = ? AND account_id = ?",
                       (template_id, aid())).fetchone() or abort(404)
    if request.method == "POST":
        f = s.Form(request.form)
        name = f.text("name", "Name", max_len=100)
        subject = f.text("subject", "Subject", max_len=200)
        body = f.text("body", "Body", max_len=10_000)
        if f.errors:
            return render_template("template_form.html", t=request.form, editing=t, errors=f.errors,
                                   placeholders=s.PLACEHOLDERS), 400
        if t:
            db.execute("UPDATE message_templates SET name = ?, subject = ?, body = ?, updated_at = ?"
                       " WHERE id = ? AND account_id = ?", (name, subject, body, s.now(), template_id, aid()))
        else:
            db.execute("INSERT INTO message_templates (account_id, name, subject, body, updated_at) VALUES (?, ?, ?, ?, ?)",
                       (aid(), name, subject, body, s.now()))
        db.commit()
        flash("Template saved.", "success")
        return redirect(url_for("main.templates"))
    return render_template("template_form.html", t=t or {}, editing=t, errors=[], placeholders=s.PLACEHOLDERS)


@bp.route("/templates/<int:template_id>/delete", methods=["POST"])
def template_delete(template_id):
    db = get_db()
    cur = db.execute("DELETE FROM message_templates WHERE id = ? AND account_id = ?", (template_id, aid()))
    if cur.rowcount != 1:
        abort(404)
    db.commit()
    flash("Template deleted.", "success")
    return redirect(url_for("main.templates"))


# ================================================================ account settings, demo, reset


@bp.route("/settings", methods=["GET", "POST"])
def settings():
    db = get_db()
    counts = {t: db.execute(f"SELECT COUNT(*) FROM {t} WHERE account_id = ?", (aid(),)).fetchone()[0]
              for t in ("clients", "projects", "scope_versions", "approvals")}
    demo_projects = db.execute("SELECT COUNT(*) FROM projects WHERE is_demo = 1 AND account_id = ?",
                               (aid(),)).fetchone()[0]
    return render_template("settings.html", counts=counts, demo_projects=demo_projects)


@bp.route("/settings/account", methods=["POST"])
def account_update():
    db = get_db()
    f = s.Form(request.form)
    name = f.text("name", "Your name or business name", max_len=100)
    email = (f.email("email", "Email", required=True) or "").lower()
    if email and db.execute("SELECT 1 FROM accounts WHERE email = ? AND id != ?", (email, aid())).fetchone():
        f.errors.append("Another account already uses this email.")
    if f.errors:
        errors_flash(s.ValidationError(f.errors))
    else:
        db.execute("UPDATE accounts SET name = ?, email = ? WHERE id = ?", (name, email, aid()))
        db.commit()
        flash("Account details saved.", "success")
    return redirect(url_for("main.settings"))


@bp.route("/settings/password", methods=["POST"])
def password_change():
    from werkzeug.security import check_password_hash, generate_password_hash

    from .auth import validate_password

    db = get_db()
    account = db.execute("SELECT password_hash FROM accounts WHERE id = ?", (aid(),)).fetchone()
    f = s.Form(request.form)
    new = f.text("new_password", "New password", max_len=200)
    validate_password(f, new, "New password")
    if new != request.form.get("confirm_password", ""):
        f.errors.append("The new passwords do not match.")
    if not check_password_hash(account["password_hash"], request.form.get("current_password", "")):
        f.errors.append("Current password is incorrect.")
    if f.errors:
        errors_flash(s.ValidationError(f.errors))
    else:
        db.execute("UPDATE accounts SET password_hash = ? WHERE id = ?", (generate_password_hash(new), aid()))
        db.commit()
        flash("Password changed.", "success")
    return redirect(url_for("main.settings"))


@bp.route("/settings/demo", methods=["POST"])
def demo_load():
    from .demo import seed_demo

    db = get_db()
    if db.execute("SELECT COUNT(*) FROM projects WHERE is_demo = 1 AND account_id = ?", (aid(),)).fetchone()[0]:
        flash("Sample data is already loaded.", "info")
    else:
        seed_demo(db, aid())
        flash("Sample data loaded. Sample projects are labelled and can be removed at any time.", "success")
    return redirect(url_for("main.dashboard"))


@bp.route("/settings/demo/remove", methods=["POST"])
def demo_remove():
    from .demo import remove_demo

    n = remove_demo(get_db(), aid())
    flash(f"Removed {n} sample project(s). Your own records were not touched.", "success")
    return redirect(url_for("main.settings"))


@bp.route("/settings/reset", methods=["POST"])
def reset():
    from werkzeug.security import check_password_hash

    from .demo import reset_account

    db = get_db()
    account = db.execute("SELECT * FROM accounts WHERE id = ?", (aid(),)).fetchone()
    if request.form.get("confirm_text") != "RESET":
        flash("Type RESET (in capitals) to confirm.", "error")
        return redirect(url_for("main.settings"))
    if not check_password_hash(account["password_hash"], request.form.get("password", "")):
        flash("Password incorrect; nothing was reset.", "error")
        return redirect(url_for("main.settings"))
    reset_account(db, aid(), current_app.config["BACKUP_DIR"])
    flash("All of your project data was reset. A backup of the database was saved on the server first. "
          "Your login was kept.", "success")
    return redirect(url_for("main.dashboard"))


@bp.route("/settings/delete", methods=["GET", "POST"])
def account_delete():
    """Delete this account and ALL of its records (no backup is kept), then sign out."""
    from werkzeug.security import check_password_hash

    from .auth import _too_many_failures, record_failure
    from .demo import RESET_ORDER

    db = get_db()
    errors = []
    if request.method == "POST":
        typed = request.form.get("confirm", "").strip()
        ip = request.remote_addr or "?"
        if typed != "DELETE" and typed.lower() != g.account["email"].lower():
            errors.append("Type DELETE (or your email) in the box to confirm.")
        elif _too_many_failures(ip):
            abort(429, "Too many wrong passwords. Please wait 15 minutes and try again.")
        else:
            row = db.execute("SELECT password_hash FROM accounts WHERE id = ?", (aid(),)).fetchone()
            if not check_password_hash(row["password_hash"], request.form.get("password", "")):
                record_failure(ip)
                errors.append("That password isn't right. Your account has not been deleted.")
        if not errors:
            account_id = aid()
            try:
                # Lets the append-only triggers allow deleting this account's history, in this transaction only.
                db.execute("INSERT OR IGNORE INTO account_purges (account_id) VALUES (?)", (account_id,))
                for table in RESET_ORDER:
                    db.execute(f"DELETE FROM {table} WHERE account_id = ?", (account_id,))
                db.execute("DELETE FROM settings WHERE account_id = ?", (account_id,))
                db.execute("DELETE FROM account_purges WHERE account_id = ?", (account_id,))
                db.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
                db.commit()
            except Exception:
                db.rollback()
                raise
            from flask import session

            session.clear()
            flash("Your account and all of its data have been deleted.", "success")
            return redirect(url_for("public.index"))
    return render_template("account_delete.html", errors=errors), (400 if errors else 200)
