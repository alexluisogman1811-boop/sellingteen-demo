"""Business logic shared by the owner UI, the client portal, and the CLI."""

import re
from datetime import date, timedelta

from flask import current_app, g, has_request_context, request

from .db import get_db, now_iso, today
from .security import new_portal_token, store_upload

KINDS = ("task", "question", "file")
ASSIGNEES = ("owner", "client")
PRIORITIES = ("low", "medium", "high")
TASK_STATUSES = ("todo", "in_progress", "submitted", "needs_correction", "done")
CLIENT_STATUSES = ("active", "completed", "archived")

STATUS_LABELS = {
    "todo": "To do",
    "in_progress": "In progress",
    "submitted": "Awaiting review",
    "needs_correction": "Needs correction",
    "done": "Done",
}
KIND_LABELS = {"task": "Action", "question": "Question", "file": "File request"}

# Client-side statuses that mean "the ball is in the client's court".
WAITING_ON_CLIENT = ("todo", "in_progress", "needs_correction")


# ---------------------------------------------------------------- helpers

def current_account_id():
    """The signed-in account. Owner code paths must never run without one."""
    account = g.get("account")
    if account is None:
        raise RuntimeError("No signed-in account")
    return account["id"]


def log_activity(client_id, actor, message, commit=False):
    # The activity row inherits the client's account, so history can never cross accounts.
    get_db().execute(
        """INSERT INTO activity (account_id, client_id, actor, message, created_at)
           SELECT account_id, id, ?, ?, ? FROM clients WHERE id = ?""",
        (actor, message[:500], now_iso(), client_id),
    )
    if commit:
        get_db().commit()


def parse_date(value):
    """Return an ISO date string or None. Raises ValueError for malformed input."""
    value = (value or "").strip()
    if not value:
        return None
    return date.fromisoformat(value).isoformat()


def portal_expiry_date() -> str:
    """Expiry for a newly issued or extended portal link (valid through this date)."""
    return (today() + timedelta(days=int(current_app.config["PORTAL_LINK_DAYS"]))).isoformat()


def portal_link_state(client) -> str:
    """One of: active, expiring (within 7 days), expired, disabled, archived."""
    if client["status"] == "archived":
        return "archived"
    if not client["portal_enabled"]:
        return "disabled"
    expires = client["portal_expires_at"]
    if expires and expires < today().isoformat():
        return "expired"
    if expires and expires <= (today() + timedelta(days=7)).isoformat():
        return "expiring"
    return "active"


def is_overdue(task, on=None) -> bool:
    """Past due and still open. Submitted items are waiting on the agency, not overdue."""
    on = on or today()
    return (bool(task["due_date"]) and task["status"] not in ("done", "submitted")
            and task["due_date"] < on.isoformat())


# ---------------------------------------------------------------- progress

def calculate_progress(statuses):
    """Progress for a list of task statuses.

    Only 'done' counts as complete. An onboarding with no tasks is 0%,
    never a misleading 100%.
    """
    statuses = list(statuses)
    total = len(statuses)
    done = sum(1 for s in statuses if s == "done")
    percent = round(done * 100 / total) if total else 0
    return {
        "total": total,
        "done": done,
        "submitted": sum(1 for s in statuses if s == "submitted"),
        "needs_correction": sum(1 for s in statuses if s == "needs_correction"),
        "percent": percent,
    }


def client_progress(client_id):
    """Overall progress plus separate client-side and agency-side breakdowns."""
    rows = get_db().execute("SELECT status, assignee FROM tasks WHERE client_id = ?", (client_id,)).fetchall()
    result = calculate_progress(r["status"] for r in rows)
    result["client"] = calculate_progress(r["status"] for r in rows if r["assignee"] == "client")
    result["owner"] = calculate_progress(r["status"] for r in rows if r["assignee"] == "owner")
    return result


def progress_for_clients(client_ids):
    """Bulk version for list pages: {client_id: progress dict}."""
    result = {cid: calculate_progress([]) for cid in client_ids}
    if not client_ids:
        return result
    marks = ",".join("?" * len(client_ids))
    rows = get_db().execute(
        f"SELECT client_id, status FROM tasks WHERE client_id IN ({marks})", list(client_ids)
    ).fetchall()
    grouped = {}
    for r in rows:
        grouped.setdefault(r["client_id"], []).append(r["status"])
    for cid, statuses in grouped.items():
        result[cid] = calculate_progress(statuses)
    return result


# ---------------------------------------------------------------- clients & tasks

def create_client(fields, template_id=None, actor="owner", commit=True, account_id=None):
    db = get_db()
    ts = now_iso()
    account_id = account_id or current_account_id()
    cur = db.execute(
        """INSERT INTO clients (account_id, name, company, email, phone, project_name, project_type,
                                start_date, notes, status, portal_token, portal_expires_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
        (
            account_id, fields["name"], fields.get("company", ""), fields.get("email", ""), fields.get("phone", ""),
            fields["project_name"], fields.get("project_type", ""), fields.get("start_date"),
            fields.get("notes", ""), new_portal_token(), portal_expiry_date(), ts,
        ),
    )
    client_id = cur.lastrowid
    log_activity(client_id, actor, f"Onboarding created for {fields['name']} ({fields['project_name']}).")
    if template_id:
        count = apply_template(client_id, template_id, fields.get("start_date"), actor=actor)
        log_activity(client_id, actor, f"Applied template with {count} checklist items.")
    if commit:
        db.commit()
    return client_id


def apply_template(client_id, template_id, start_date=None, actor="owner"):
    db = get_db()
    base = date.fromisoformat(start_date) if start_date else today()
    # Only a template owned by the same account as the client can be applied.
    items = db.execute(
        """SELECT i.* FROM template_items i
           JOIN onboarding_templates t ON t.id = i.template_id
           JOIN clients c ON c.account_id = t.account_id
           WHERE i.template_id = ? AND c.id = ? ORDER BY i.position, i.id""", (template_id, client_id)
    ).fetchall()
    offset = db.execute("SELECT COALESCE(MAX(position), 0) FROM tasks WHERE client_id = ?", (client_id,)).fetchone()[0]
    ts = now_iso()
    for i, item in enumerate(items, start=1):
        due = (base + timedelta(days=item["due_offset_days"])).isoformat()
        db.execute(
            """INSERT INTO tasks (account_id, client_id, title, description, kind, assignee, priority, due_date,
                                  status, position, created_at, updated_at)
               SELECT account_id, id, ?, ?, ?, ?, ?, ?, 'todo', ?, ?, ? FROM clients WHERE id = ?""",
            (item["title"], item["description"], item["kind"], item["assignee"],
             item["priority"], due, offset + i, ts, ts, client_id),
        )
    return len(items)


def add_task(client_id, fields, actor="owner"):
    db = get_db()
    ts = now_iso()
    pos = db.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM tasks WHERE client_id = ?", (client_id,)).fetchone()[0]
    cur = db.execute(
        """INSERT INTO tasks (account_id, client_id, title, description, kind, assignee, priority, due_date,
                              status, position, created_at, updated_at)
           SELECT account_id, id, ?, ?, ?, ?, ?, ?, 'todo', ?, ?, ? FROM clients WHERE id = ?""",
        (fields["title"], fields.get("description", ""), fields["kind"], fields["assignee"],
         fields["priority"], fields.get("due_date"), pos, ts, ts, client_id),
    )
    log_activity(client_id, actor, f"Added checklist item “{fields['title']}”.")
    db.commit()
    return cur.lastrowid


def set_task_status(task, status, actor="owner"):
    if status not in TASK_STATUSES:
        raise ValueError("Unknown status")
    db = get_db()
    ts = now_iso()
    completed = ts if status == "done" else None
    db.execute(
        "UPDATE tasks SET status = ?, updated_at = ?, completed_at = ? WHERE id = ? AND account_id = ?",
        (status, ts, completed, task["id"], task["account_id"]),
    )
    if status != task["status"]:
        log_activity(task["client_id"], actor,
                     f"“{task['title']}” moved from {STATUS_LABELS[task['status']]} to {STATUS_LABELS[status]}.")


def add_comment(task, author, body, is_correction=False, is_internal=False):
    """Internal notes are owner-only and never rendered in the client portal."""
    internal = bool(is_internal) and author == "owner" and not is_correction
    get_db().execute(
        """INSERT INTO comments (account_id, task_id, author, body, is_correction, is_internal, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (task["account_id"], task["id"], author, body[:4000], 1 if is_correction else 0, 1 if internal else 0, now_iso()),
    )


def save_upload(task, display_name, ext, mime, data, uploaded_by):
    stored = store_upload(data, ext)
    get_db().execute(
        """INSERT INTO uploads (account_id, task_id, client_id, original_name, stored_name, mime_type,
                                size_bytes, uploaded_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (task["account_id"], task["id"], task["client_id"], display_name, stored, mime, len(data), uploaded_by, now_iso()),
    )
    return stored


def delete_upload(upload_row, actor):
    """Remove one upload (DB row first, then the file on disk)."""
    from .security import delete_stored_file

    get_db().execute("DELETE FROM uploads WHERE id = ? AND account_id = ?",
                     (upload_row["id"], upload_row["account_id"]))
    log_activity(upload_row["client_id"], actor, f"Removed file {upload_row['original_name']}.")
    get_db().commit()
    delete_stored_file(upload_row["stored_name"])


def _remove_files(rows):
    from .security import delete_stored_file

    for r in rows:
        delete_stored_file(r["stored_name"])


def delete_task(task, actor="owner"):
    db = get_db()
    files = db.execute("SELECT stored_name FROM uploads WHERE task_id = ? AND account_id = ?",
                       (task["id"], task["account_id"])).fetchall()
    db.execute("DELETE FROM tasks WHERE id = ? AND account_id = ?", (task["id"], task["account_id"]))
    log_activity(task["client_id"], actor, f"Deleted checklist item “{task['title']}”.")
    db.commit()
    _remove_files(files)


def delete_client(client_id, account_id=None):
    db = get_db()
    account_id = account_id or current_account_id()
    files = db.execute("SELECT stored_name FROM uploads WHERE client_id = ? AND account_id = ?",
                       (client_id, account_id)).fetchall()
    # cascades to tasks/uploads/comments/activity
    db.execute("DELETE FROM clients WHERE id = ? AND account_id = ?", (client_id, account_id))
    db.commit()
    _remove_files(files)


def dashboard_data(account_id=None):
    db = get_db()
    a = account_id or current_account_id()
    t = today().isoformat()
    counts = {
        "active": db.execute("SELECT COUNT(*) FROM clients WHERE account_id = ? AND status = 'active'",
                             (a,)).fetchone()[0],
        "completed": db.execute("SELECT COUNT(*) FROM clients WHERE account_id = ? AND status = 'completed'",
                                (a,)).fetchone()[0],
    }
    task_cols = """t.*, c.name AS client_name, c.project_name"""
    overdue = db.execute(
        f"""SELECT {task_cols} FROM tasks t JOIN clients c ON c.id = t.client_id
            WHERE c.account_id = ? AND t.account_id = ? AND c.status = 'active'
              AND t.status NOT IN ('done', 'submitted') AND t.due_date IS NOT NULL AND t.due_date < ?
            ORDER BY t.due_date, t.id""", (a, a, t)).fetchall()
    waiting = db.execute(
        f"""SELECT {task_cols} FROM tasks t JOIN clients c ON c.id = t.client_id
            WHERE c.account_id = ? AND t.account_id = ? AND c.status = 'active' AND t.assignee = 'client'
              AND t.status IN ('todo','in_progress','needs_correction')
            ORDER BY t.due_date IS NULL, t.due_date, t.id""", (a, a)).fetchall()
    review = db.execute(
        f"""SELECT {task_cols} FROM tasks t JOIN clients c ON c.id = t.client_id
            WHERE c.account_id = ? AND t.account_id = ? AND c.status = 'active' AND t.status = 'submitted'
            ORDER BY t.updated_at DESC""", (a, a)).fetchall()
    active_clients = db.execute(
        "SELECT * FROM clients WHERE account_id = ? AND status = 'active' ORDER BY start_date IS NULL, start_date, name",
        (a,)).fetchall()
    completed_clients = db.execute(
        "SELECT * FROM clients WHERE account_id = ? AND status = 'completed' ORDER BY completed_at DESC LIMIT 5",
        (a,)).fetchall()
    recent = db.execute(
        """SELECT a.*, c.name AS client_name FROM activity a LEFT JOIN clients c ON c.id = a.client_id
           WHERE a.account_id = ? ORDER BY a.id DESC LIMIT 12""", (a,)).fetchall()
    progress = progress_for_clients([c["id"] for c in active_clients] + [c["id"] for c in completed_clients])
    counts.update(overdue=len(overdue), waiting=len(waiting), review=len(review))
    return dict(counts=counts, overdue=overdue, waiting=waiting, review=review,
                active_clients=active_clients, completed_clients=completed_clients,
                recent=recent, progress=progress)


# ---------------------------------------------------------------- reminders

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
REMINDER_PLACEHOLDERS = ("client_name", "company", "project_name", "portal_link", "open_items",
                         "overdue_items", "correction_items", "agency_name", "start_date")


def render_reminder(text, values):
    """Replace {placeholder} names only — never Python format() (no attribute access)."""
    return _PLACEHOLDER.sub(lambda m: str(values.get(m.group(1), m.group(0))), text)


def current_lang() -> str:
    """'th' when the hosting layer's language cookie selects Thai, else 'en'. Safe outside a request."""
    return "th" if has_request_context() and request.cookies.get("st_lang") == "th" else "en"


# Built-in reminder templates (name, subject, body). Seeded per account in English; rows are
# never rewritten afterwards.
DEFAULT_REMINDER_TEMPLATES = [
    ("Friendly nudge", "Quick reminder: a few items for {project_name}",
     "Hi {client_name},\n\nHope you're well! To keep {project_name} on track for {start_date}, "
     "we still need a few things from you:\n\n{open_items}\n\nYou can complete everything in your "
     "secure onboarding portal:\n{portal_link}\n\nThanks so much,\n{agency_name}"),
    ("Overdue items", "Action needed: overdue onboarding items for {project_name}",
     "Hi {client_name},\n\nThe following onboarding items are now past their due date:\n\n"
     "{overdue_items}\n\nCould you take a look when you have a moment? Your portal link:\n"
     "{portal_link}\n\nLet us know if anything is unclear.\n\n{agency_name}"),
    ("Corrections requested", "A couple of updates needed for {project_name}",
     "Hi {client_name},\n\nThanks for your submissions! We've reviewed them and need a small "
     "update on:\n\n{correction_items}\n\nOur notes are on each item in your portal:\n"
     "{portal_link}\n\nThank you,\n{agency_name}"),
]

# Thai versions of the built-ins, same order and same placeholders. Polite, gender-neutral
# (no ครับ/ค่ะ), and they add no deadlines or consequences the English does not state.
DEFAULT_REMINDER_TEMPLATES_TH = [
    ("แจ้งเตือนรายการที่รอข้อมูล", "แจ้งเตือน: รายการที่ยังรอข้อมูลสำหรับ {project_name}",
     "เรียน คุณ{client_name}\n\nหวังว่าคุณจะสบายดี เพื่อให้โครงการ {project_name} ดำเนินไปตามแผนสำหรับวันที่ "
     "{start_date} ทางเรายังต้องการข้อมูลบางรายการจากคุณ ดังนี้\n\n{open_items}\n\n"
     "คุณสามารถดำเนินการทุกรายการได้ในพอร์ทัลเริ่มงานที่ปลอดภัยของคุณ:\n{portal_link}\n\n"
     "ขอบคุณมาก\n{agency_name}"),
    ("รายการที่เลยกำหนด", "ขอความร่วมมือ: รายการเริ่มงานที่เลยกำหนดสำหรับ {project_name}",
     "เรียน คุณ{client_name}\n\nรายการเริ่มงานต่อไปนี้เลยวันครบกำหนดแล้ว\n\n{overdue_items}\n\n"
     "รบกวนช่วยตรวจสอบเมื่อสะดวก ลิงก์พอร์ทัลของคุณ:\n{portal_link}\n\n"
     "หากมีข้อสงสัยใด ๆ โปรดแจ้งให้เราทราบ\n\n{agency_name}"),
    ("ขอแก้ไขข้อมูล", "ขอแก้ไขข้อมูลเล็กน้อยสำหรับ {project_name}",
     "เรียน คุณ{client_name}\n\nขอบคุณสำหรับข้อมูลที่ส่งมา ทางเราได้ตรวจสอบแล้ว "
     "และขอให้ช่วยปรับแก้เล็กน้อยในรายการต่อไปนี้\n\n{correction_items}\n\n"
     "เราได้เขียนหมายเหตุไว้ในแต่ละรายการในพอร์ทัลของคุณ:\n{portal_link}\n\n"
     "ขอบคุณ\n{agency_name}"),
]

_BUILTIN_TH = {en: th for en, th in zip(DEFAULT_REMINDER_TEMPLATES, DEFAULT_REMINDER_TEMPLATES_TH)}


def _nl(text):
    return (text or "").replace("\r\n", "\n")


def builtin_reminder(row, lang):
    """The (name, subject, body) translation for an UNEDITED built-in row in `lang`, else None.

    A row counts as built-in only if name, subject and body all still equal the English default
    exactly (line endings aside). Any user edit, or a user-created template, returns None so the
    user's own text is used as-is.
    """
    if lang != "th":
        return None
    return _BUILTIN_TH.get((_nl(row["name"]), _nl(row["subject"]), _nl(row["body"])))


def render_reminders(rows, client, tasks, portal_link, business_name, lang=None):
    """Render reminder rows for one client. Thai built-ins are used only for unedited defaults."""
    lang = lang or current_lang()
    values = {"en": reminder_values(client, tasks, portal_link, business_name)}
    out = []
    for r in rows:
        name, subject, body = r["name"], r["subject"], r["body"]
        th = builtin_reminder(r, lang)
        if th:
            name, subject, body = th
            if "th" not in values:
                values["th"] = reminder_values(client, tasks, portal_link, business_name, lang="th")
            vals = values["th"]
        else:
            vals = values["en"]
        out.append(dict(name=name, subject=render_reminder(subject, vals), body=render_reminder(body, vals)))
    return out


def reminder_values(client, tasks, portal_link, business_name, lang="en"):
    on = today()
    thai = lang == "th"

    def bullet(rows):
        if not rows:
            return "- (ไม่มี)" if thai else "- (none)"
        lines = []
        for r in rows:
            if r["due_date"]:
                due = f" (กำหนดส่ง {r['due_date']})" if thai else f" (due {r['due_date']})"
            else:
                due = ""
            lines.append(f"- {r['title']}{due}")
        return "\n".join(lines)

    client_tasks = [t for t in tasks if t["assignee"] == "client"]
    open_items = [t for t in client_tasks if t["status"] in WAITING_ON_CLIENT]
    return {
        "client_name": client["name"],
        "company": client["company"] or client["name"],
        "project_name": client["project_name"],
        "start_date": client["start_date"] or ("รอยืนยัน" if thai else "TBC"),
        "portal_link": portal_link,
        "agency_name": business_name,
        "open_items": bullet(open_items),
        "overdue_items": bullet([t for t in open_items if is_overdue(t, on)]),
        "correction_items": bullet([t for t in client_tasks if t["status"] == "needs_correction"]),
    }


# ---------------------------------------------------------------- CSV export

def csv_safe(value):
    """Neutralise spreadsheet formula injection in exported cells."""
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


# ---------------------------------------------------------------- reset & demo

# Tables holding an account's data (order matters for deletion).
ACCOUNT_TABLES = ("uploads", "comments", "activity", "tasks", "clients",
                  "template_items", "onboarding_templates", "reminder_templates")


def reset_account_data(account_id, load_demo=True):
    """Delete ONE account's records and uploaded files, then optionally load demo data into it.

    Other accounts are never touched.
    """
    db = get_db()
    files = db.execute("SELECT stored_name FROM uploads WHERE account_id = ?", (account_id,)).fetchall()
    for table in ACCOUNT_TABLES:
        db.execute(f"DELETE FROM {table} WHERE account_id = ?", (account_id,))
    db.commit()
    _remove_files(files)
    seed_reminder_templates(account_id)
    if load_demo:
        seed_demo(account_id)
    db.commit()


def seed_reminder_templates(account_id):
    ts = now_iso()
    get_db().executemany(
        "INSERT INTO reminder_templates (account_id, name, subject, body, created_at) VALUES (?, ?, ?, ?, ?)",
        [(account_id, n, s, b, ts) for n, s, b in DEFAULT_REMINDER_TEMPLATES],
    )


DEMO_TEMPLATES = [
    ("Website Redesign", "Website", "Everything needed before design and development can start.", [
        ("Sign the project agreement", "Review and confirm the signed agreement.", "task", "client", "high", 2),
        ("Upload your logo files", "Vector (PDF) or high-resolution PNG preferred.", "file", "client", "high", 3),
        ("Brand guidelines", "Upload any brand guide, fonts list, or colour references.", "file", "client", "medium", 5),
        ("Who is your target audience?", "Describe your ideal customers in a few sentences.", "question", "client", "medium", 5),
        ("List 3 websites you admire", "Paste links and tell us what you like about each.", "question", "client", "low", 7),
        ("Provide hosting / domain access details", "Tell us who manages your domain and hosting (no passwords here — we'll arrange a secure handover).", "question", "client", "high", 7),
        ("Set up project workspace", "Create project folders, board, and internal kickoff notes.", "task", "owner", "medium", 1),
        ("Schedule kickoff call", "Send calendar invite to client stakeholders.", "task", "owner", "high", 2),
    ]),
    ("Brand Identity", "Branding", "Discovery inputs for a logo and visual identity project.", [
        ("Complete the brand questionnaire", "What are your brand values, tone, and personality?", "question", "client", "high", 3),
        ("Share existing brand assets", "Current logo, any previous brand work.", "file", "client", "medium", 4),
        ("Competitor list", "Name 3–5 competitors and their websites.", "question", "client", "medium", 5),
        ("Confirm decision makers", "Who signs off on design rounds?", "question", "client", "high", 2),
        ("Prepare moodboard", "Initial visual research for the kickoff.", "task", "owner", "medium", 6),
    ]),
    ("SEO Retainer", "Marketing", "Access and baseline information for an ongoing SEO engagement.", [
        ("Grant analytics viewer access", "Add our team as a viewer in your analytics tool, then mark this done.", "task", "client", "high", 3),
        ("Target keywords and services", "Which services and locations matter most?", "question", "client", "medium", 5),
        ("Upload last 12 months of reports", "Any previous SEO or traffic reports (PDF/CSV).", "file", "client", "low", 7),
        ("Run technical baseline audit", "Crawl site and document issues.", "task", "owner", "high", 5),
    ]),
]


def seed_demo(account_id):
    """Insert demo records. Nothing commits until the end, so seeding is all-or-nothing."""
    db = get_db()
    ts = now_iso()
    template_ids = {}
    for name, ptype, desc, items in DEMO_TEMPLATES:
        tid = db.execute(
            "INSERT INTO onboarding_templates (account_id, name, project_type, description, created_at) VALUES (?, ?, ?, ?, ?)",
            (account_id, name, ptype, desc, ts)).lastrowid
        template_ids[name] = tid
        for pos, (title, d, kind, who, prio, offset) in enumerate(items, start=1):
            db.execute(
                """INSERT INTO template_items (account_id, template_id, title, description, kind, assignee, priority,
                                               due_offset_days, position) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (account_id, tid, title, d, kind, who, prio, offset, pos))

    t0 = today()

    def iso(days):
        return (t0 + timedelta(days=days)).isoformat()

    # 1. Northwind Bakery — started 10 days ago, mixed progress, some overdue.
    c1 = create_client(dict(name="Maya Chen", company="Northwind Bakery", email="maya@northwind.example",
                            phone="+1 555 0100", project_name="Northwind Website Redesign",
                            project_type="Website", start_date=iso(-10),
                            notes="Wants online ordering in phase 2. Prefers email over calls."),
                       template_ids["Website Redesign"], actor="demo", commit=False, account_id=account_id)
    tasks = {r["title"]: r for r in db.execute("SELECT * FROM tasks WHERE client_id = ?", (c1,))}
    set_task_status(tasks["Set up project workspace"], "done", actor="demo")
    set_task_status(tasks["Schedule kickoff call"], "done", actor="demo")
    set_task_status(tasks["Sign the project agreement"], "done", actor="demo")
    logo = tasks["Upload your logo files"]
    save_upload(logo, "northwind-logo-notes.txt", "txt", "text/plain",
                b"Demo upload: placeholder for the Northwind logo pack.\n", "client")
    set_task_status(logo, "submitted", actor="client")
    q = tasks["Who is your target audience?"]
    db.execute("UPDATE tasks SET answer = ? WHERE id = ?",
               ("Local families and office workers within 5 miles who want fresh bread and catering.", q["id"]))
    set_task_status(q, "needs_correction", actor="demo")
    add_comment(q, "owner", "Great start! Could you add a bit about your catering customers (offices, events)?", True)

    # 2. Atlas Fitness — brand project, just started.
    c2 = create_client(dict(name="Jordan Rivera", company="Atlas Fitness", email="jordan@atlasfit.example",
                            phone="+1 555 0142", project_name="Atlas Brand Refresh", project_type="Branding",
                            start_date=iso(-1), notes="Two gym locations; opening a third in spring."),
                       template_ids["Brand Identity"], actor="demo", commit=False, account_id=account_id)
    t2 = {r["title"]: r for r in db.execute("SELECT * FROM tasks WHERE client_id = ?", (c2,))}
    db.execute("UPDATE tasks SET answer = ? WHERE id = ?",
               ("Jordan (owner) and Sam (marketing lead).", t2["Confirm decision makers"]["id"]))
    set_task_status(t2["Confirm decision makers"], "submitted", actor="client")

    # 3. Greenleaf Dental — SEO, overdue client items.
    c3 = create_client(dict(name="Dr. Priya Patel", company="Greenleaf Dental", email="office@greenleaf.example",
                            project_name="Greenleaf Local SEO", project_type="Marketing", start_date=iso(-14),
                            notes="Focus on 'family dentist' + neighbourhood keywords."),
                       template_ids["SEO Retainer"], actor="demo", commit=False, account_id=account_id)
    t3 = {r["title"]: r for r in db.execute("SELECT * FROM tasks WHERE client_id = ?", (c3,))}
    set_task_status(t3["Run technical baseline audit"], "in_progress", actor="demo")

    # 4. Harbor Legal — completed onboarding.
    c4 = create_client(dict(name="Alex Morgan", company="Harbor Legal", email="alex@harborlegal.example",
                            project_name="Harbor Legal Website", project_type="Website", start_date=iso(-40)),
                       template_ids["Website Redesign"], actor="demo", commit=False, account_id=account_id)
    for r in db.execute("SELECT * FROM tasks WHERE client_id = ?", (c4,)).fetchall():
        set_task_status(r, "done", actor="demo")
    db.execute("UPDATE clients SET status = 'completed', completed_at = ? WHERE id = ?", (ts, c4))
    log_activity(c4, "demo", "Onboarding marked complete.")
    db.commit()
