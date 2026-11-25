"""Job domain logic: queries, status workflow, completion rules, reports, share links, CSV."""
import csv
import hashlib
import io
import re
import secrets
from datetime import datetime, timedelta

from . import storage
from .db import get_db
from .utils import csv_safe, escape_like, is_valid_date, now_iso

STATUS_LABELS = {
    "scheduled": "Scheduled",
    "in_progress": "In progress",
    "awaiting_review": "Awaiting review",
    "completed": "Completed",
    "cancelled": "Cancelled",
}
LOCKED_STATUSES = {"completed", "cancelled"}
ITEM_STATES = {"pending": "Not checked", "done": "Done", "incomplete": "Not completed"}
SHARE_EXPIRY_CHOICES = {0: "Never", 7: "7 days", 30: "30 days", 90: "90 days"}
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
PAGE_SIZE = 25

JOB_SELECT = """
SELECT j.*, c.name AS customer_name, p.name AS property_name, p.address AS property_address,
       t.name AS template_name,
       (SELECT COUNT(*) FROM job_items i WHERE i.job_id = j.id) AS items_total,
       (SELECT COUNT(*) FROM job_items i WHERE i.job_id = j.id AND i.state = 'done') AS items_done,
       (SELECT COUNT(*) FROM job_items i WHERE i.job_id = j.id AND i.state = 'incomplete') AS items_incomplete,
       (SELECT COUNT(*) FROM job_items i WHERE i.job_id = j.id AND i.state = 'pending') AS items_pending,
       (SELECT COUNT(*) FROM photos ph WHERE ph.job_id = j.id AND ph.kind = 'before') AS before_photos,
       (SELECT COUNT(*) FROM photos ph WHERE ph.job_id = j.id AND ph.kind = 'after') AS after_photos,
       (SELECT MIN(e.created_at) FROM job_events e WHERE e.job_id = j.id AND e.action = 'start') AS work_started_at,
       (SELECT MAX(e.created_at) FROM job_events e WHERE e.job_id = j.id AND e.action = 'submit') AS submitted_at
FROM jobs j
JOIN customers c ON c.id = j.customer_id
JOIN properties p ON p.id = j.property_id
LEFT JOIN templates t ON t.id = j.template_id
"""


def get_job(job_id, account_id):
    """A job only if it belongs to the given account (a different account's id behaves as "not found")."""
    return get_db().execute(JOB_SELECT + " WHERE j.id = ? AND j.account_id = ?", (job_id, account_id)).fetchone()


def get_job_items(job_id):
    return get_db().execute(
        "SELECT * FROM job_items WHERE job_id = ? ORDER BY position, id", (job_id,)
    ).fetchall()


def get_job_photos(job_id):
    return get_db().execute(
        "SELECT * FROM photos WHERE job_id = ? ORDER BY created_at, id", (job_id,)
    ).fetchall()


def is_locked(job):
    return job["status"] in LOCKED_STATUSES


def photos_with_status(job_id):
    """Photo rows as dicts, flagging any whose file is missing from storage."""
    return [dict(row, missing=not storage.file_exists(row["stored_name"])) for row in get_job_photos(job_id)]


def storage_audit(account_id=None):
    """Compare photo records with files on disk. Missing files are listed for one account only;
    unreferenced ("orphan") files belong to no account, so they are checked against every photo record."""
    db = get_db()
    rows = db.execute(
        "SELECT p.id, p.job_id, p.kind, p.label, p.stored_name, j.title FROM photos p"
        " JOIN jobs j ON j.id = p.job_id" + (" WHERE p.account_id = ?" if account_id else "") +
        " ORDER BY p.job_id, p.id", (account_id,) if account_id else ()).fetchall()
    referenced = {r[0] for r in db.execute("SELECT stored_name FROM photos")}
    return {
        "photo_count": len(rows),
        "missing": [dict(r) for r in rows if not storage.file_exists(r["stored_name"])],
        "orphans": [n for n in storage.list_stored_files() if n not in referenced],
    }


def cleanup_orphans(min_age_seconds=3600):
    """Delete unreferenced photo files older than min_age (younger ones may belong to an upload in progress)."""
    removed = 0
    for name in storage_audit()["orphans"]:
        if storage.file_age_seconds(name) >= min_age_seconds:
            storage.delete_stored(name)
            removed += 1
    return removed


def photo_label_summary(photos):
    """Group photos by label (case/space-insensitive) -> {display label: {"before": [...], "after": [...]}}."""
    groups, display = {}, {}
    for photo in photos:
        key = " ".join(photo["label"].split()).casefold()
        display.setdefault(key, " ".join(photo["label"].split()))
        groups.setdefault(display[key], {"before": [], "after": []})[photo["kind"]].append(photo)
    return groups


# ---------------------------------------------------------------- history

EVENT_LABELS = {
    "created": "Job created",
    "edited": "Job details edited",
    "start": "Started",
    "submit": "Submitted for review",
    "approve": "Approved as completed",
    "return": "Returned to in progress",
    "cancel": "Cancelled",
    "reopen": "Reopened",
    "exceptions_confirmed": "Exceptions confirmed",
    "checklist": "Checklist updated",
    "task_added": "Task added",
    "task_removed": "Task removed",
    "photos_added": "Photos added",
    "photo_deleted": "Photo deleted",
    "notes": "Notes updated",
    "share_created": "Share link created",
    "share_revoked": "Share link revoked",
}


def log_event(db, job_id, actor, action, detail=""):
    """Append to the job history. The caller commits as part of its own transaction."""
    db.execute("INSERT INTO job_events (job_id, account_id, created_at, actor, action, detail)"
               " VALUES (?, (SELECT account_id FROM jobs WHERE id = ?), ?, ?, ?, ?)",
               (job_id, job_id, now_iso(), actor, action, detail[:4000]))


def get_job_events(job_id):
    return get_db().execute("SELECT * FROM job_events WHERE job_id = ? ORDER BY id DESC", (job_id,)).fetchall()


def latest_event(job_id, action):
    return get_db().execute("SELECT * FROM job_events WHERE job_id = ? AND action = ? ORDER BY id DESC LIMIT 1",
                            (job_id, action)).fetchone()


def record_work_activity(db, job, user):
    """Keep the job status consistent with work being recorded. Caller commits.

    Returns a message for the user when the status changed, else None.
    """
    if job["status"] == "scheduled":
        # Appointment times are left alone: the history entry is the record of when work began.
        db.execute("UPDATE jobs SET status = 'in_progress', updated_at = ? WHERE id = ?", (now_iso(), job["id"]))
        log_event(db, job["id"], user["name"], "start", "Started automatically when work was recorded.")
        return "Job is now in progress."
    if job["status"] == "awaiting_review" and user["role"] != "manager":
        db.execute("UPDATE jobs SET status = 'in_progress', updated_at = ? WHERE id = ?", (now_iso(), job["id"]))
        log_event(db, job["id"], user["name"], "return", "Changed after it was submitted; it must be submitted again.")
        return "This job was awaiting review, so it is back in progress. Submit it again when you are done."
    return None


def has_recorded_work(job_id):
    db = get_db()
    return bool(db.execute("SELECT 1 FROM job_items WHERE job_id = ? AND state != 'pending' LIMIT 1", (job_id,))
                .fetchone() or db.execute("SELECT 1 FROM photos WHERE job_id = ? LIMIT 1", (job_id,)).fetchone())


# ---------------------------------------------------------------- search

def parse_job_filters(args, account_id):
    """Turn query-string args into (filters, where_sql, params, errors). Invalid values are ignored.
    Always limited to one account."""
    filters, where, params, errors = {}, ["j.account_id = ?"], [account_id], []
    q = (args.get("q") or "").strip()[:100]
    if q:
        filters["q"] = q
        like = "%" + escape_like(q) + "%"
        where.append(
            "(j.title LIKE ? ESCAPE '\\' OR c.name LIKE ? ESCAPE '\\' OR p.name LIKE ? ESCAPE '\\'"
            " OR p.address LIKE ? ESCAPE '\\' OR j.assigned_worker LIKE ? ESCAPE '\\')"
        )
        params += [like] * 5
    status = args.get("status") or ""
    if status:
        if status in STATUS_LABELS:
            filters["status"] = status
            where.append("j.status = ?")
            params.append(status)
        else:
            errors.append("Unknown status filter ignored.")
    for key, column in (("customer_id", "j.customer_id"), ("property_id", "j.property_id")):
        raw = args.get(key) or ""
        if raw:
            if raw.isdigit():
                filters[key] = int(raw)
                where.append(f"{column} = ?")
                params.append(int(raw))
            else:
                errors.append(f"Invalid {key.replace('_id', '')} filter ignored.")
    for key, op in (("date_from", ">="), ("date_to", "<=")):
        raw = args.get(key) or ""
        if raw:
            if is_valid_date(raw):
                filters[key] = raw
                where.append(f"j.appointment_date {op} ?")
                params.append(raw)
            else:
                errors.append(f"Invalid date '{raw[:20]}' ignored.")
    where_sql = " WHERE " + " AND ".join(where)
    return filters, where_sql, params, errors


def search_jobs(args, account_id, page=1, limit=PAGE_SIZE):
    filters, where_sql, params, errors = parse_job_filters(args, account_id)
    db = get_db()
    total = db.execute(
        "SELECT COUNT(*) FROM jobs j JOIN customers c ON c.id = j.customer_id "
        "JOIN properties p ON p.id = j.property_id" + where_sql, params
    ).fetchone()[0]
    order = " ORDER BY j.appointment_date DESC, j.start_time DESC, j.id DESC"
    if limit is None:
        rows = db.execute(JOB_SELECT + where_sql + order, params).fetchall()
    else:
        rows = db.execute(
            JOB_SELECT + where_sql + order + " LIMIT ? OFFSET ?", params + [limit, (page - 1) * limit]
        ).fetchall()
    return rows, total, filters, errors


# ---------------------------------------------------------------- completion rules

def completion_issues(job):
    """Reasons the job cannot be submitted/completed yet. Empty list means OK."""
    issues = []
    items = get_job_items(job["id"])
    if not items:
        issues.append("Add at least one checklist item before completing the job.")
    for item in items:
        if item["state"] == "pending" and item["required"]:
            issues.append(
                f"Required task “{item['label']}” is not checked off. "
                "Mark it done, or mark it not completed and give a reason."
            )
        elif item["state"] == "incomplete" and not item["reason"].strip():
            issues.append(f"Task “{item['label']}” is marked not completed but has no reason.")
    if job["start_time"] and job["end_time"] and job["end_time"] < job["start_time"]:
        issues.append("End time is earlier than start time.")
    return issues


# action: (allowed from-statuses, new status, required role)
TRANSITIONS = {
    "start": ({"scheduled"}, "in_progress", "worker"),
    "submit": ({"in_progress"}, "awaiting_review", "worker"),
    "approve": ({"in_progress", "awaiting_review"}, "completed", "manager"),
    "return": ({"awaiting_review"}, "in_progress", "manager"),
    "cancel": ({"scheduled", "in_progress", "awaiting_review"}, "cancelled", "manager"),
    "reopen": ({"completed", "cancelled"}, "in_progress", "manager"),
}
TRANSITION_MESSAGES = {
    "start": "Job started.",
    "submit": "Job submitted for review.",
    "approve": "Job marked as completed.",
    "return": "Job returned to in progress.",
    "cancel": "Job cancelled.",
    "reopen": "Job reopened for editing.",
}


def apply_transition(job, action, user, confirm_exceptions=False):
    """Validate and apply a status change. Returns a list of error messages (empty on success)."""
    if action not in TRANSITIONS:
        return ["Unknown action."]
    allowed_from, new_status, role = TRANSITIONS[action]
    if role == "manager" and user["role"] != "manager":
        return ["Only a manager can do that."]
    if job["status"] not in allowed_from:
        return [f"A job that is {STATUS_LABELS[job['status']].lower()} cannot be changed that way."]

    exceptions = []
    if action in ("submit", "approve"):
        issues = completion_issues(job)
        if issues:
            return issues
        exceptions = [i for i in get_job_items(job["id"]) if i["state"] == "incomplete"]
        if exceptions and not confirm_exceptions:
            return [
                f"{len(exceptions)} task(s) are recorded as not completed. "
                "Tick “I confirm these exceptions” to continue."
            ]
    if action == "reopen" and not has_recorded_work(job["id"]):
        new_status = "scheduled"

    now = now_iso()
    fields = {"status": new_status, "updated_at": now}
    # start_time/end_time are the planned appointment window and are never filled in automatically;
    # when work actually started or was submitted is taken from the job history instead.
    if action == "approve":
        fields["completed_at"] = now
        fields["reviewed_by"] = user["name"]
    if action in ("reopen", "return"):
        fields["completed_at"] = None
        fields["reviewed_by"] = ""

    db = get_db()
    assignments = ", ".join(f"{column} = ?" for column in fields)  # column names are fixed above
    db.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", list(fields.values()) + [job["id"]])
    if exceptions:
        log_event(db, job["id"], user["name"], "exceptions_confirmed",
                  "; ".join(f"{i['label']}: {i['reason']}" for i in exceptions))
    log_event(db, job["id"], user["name"], action, STATUS_LABELS[job["status"]] + " → " + STATUS_LABELS[new_status])
    db.commit()
    return []


# ---------------------------------------------------------------- reports

def build_report(job_id, account_id):
    """Everything on a report comes from saved rows of this one account's job."""
    job = get_job(job_id, account_id)
    if job is None:
        return None
    account = get_db().execute("SELECT name FROM accounts WHERE id = ?", (account_id,)).fetchone()
    checklist = get_job_items(job_id)
    status = job["status"]
    started = get_db().execute("SELECT * FROM job_events WHERE job_id = ? AND action = 'start' ORDER BY id LIMIT 1",
                               (job_id,)).fetchone()
    submitted = latest_event(job_id, "submit") if status in ("awaiting_review", "completed") else None
    approved = latest_event(job_id, "approve") if status == "completed" else None
    has_exceptions = any(i["state"] == "incomplete" for i in checklist)
    confirmed = latest_event(job_id, "exceptions_confirmed") if has_exceptions and (submitted or approved) else None
    return {
        "job": job,
        "business_name": account["name"] if account else "",
        "checklist": checklist,
        "photo_groups": photo_label_summary(photos_with_status(job_id)),
        "started": started,
        "submitted": submitted,
        "approved": approved,
        "exceptions_confirmed": confirmed,
        "summary": {
            "total": len(checklist),
            "done": sum(1 for i in checklist if i["state"] == "done"),
            "incomplete": sum(1 for i in checklist if i["state"] == "incomplete"),
            "pending": sum(1 for i in checklist if i["state"] == "pending"),
        },
        "generated_at": datetime.now().strftime("%d %b %Y, %H:%M"),
    }


# ---------------------------------------------------------------- share links

def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_share_link(job_id, user, days):
    token = secrets.token_urlsafe(32)
    now = datetime.now().replace(microsecond=0)
    expires = (now + timedelta(days=days)).isoformat(sep=" ") if days else None
    db = get_db()
    db.execute(
        "INSERT INTO share_links (job_id, account_id, token_hash, token_hint, created_by, created_at, expires_at)"
        " VALUES (?, (SELECT account_id FROM jobs WHERE id = ?), ?, ?, ?, ?, ?)",
        (job_id, job_id, hash_token(token), token[-4:], user["name"], now.isoformat(sep=" "), expires),
    )
    db.commit()
    return token


def resolve_share_token(token):
    """Return the active share_links row for a token, or None (invalid, revoked or expired)."""
    if not isinstance(token, str) or not TOKEN_RE.match(token):
        return None
    link = get_db().execute(
        "SELECT * FROM share_links WHERE token_hash = ? AND revoked_at IS NULL", (hash_token(token),)
    ).fetchone()
    if link is None:
        return None
    if link["expires_at"] and link["expires_at"] <= now_iso():
        return None
    return link


def list_share_links(job_id):
    return get_db().execute(
        "SELECT * FROM share_links WHERE job_id = ? ORDER BY created_at DESC, id DESC", (job_id,)
    ).fetchall()


def revoke_share_link(job_id, link_id):
    db = get_db()
    cur = db.execute(
        "UPDATE share_links SET revoked_at = ? WHERE id = ? AND job_id = ? AND revoked_at IS NULL",
        (now_iso(), link_id, job_id),
    )
    db.commit()
    return cur.rowcount == 1


# ---------------------------------------------------------------- CSV

CSV_COLUMNS = [
    ("job_id", "id"), ("title", "title"), ("status", "status"), ("appointment_date", "appointment_date"),
    ("appointment_start", "start_time"), ("appointment_end", "end_time"), ("work_started_at", "work_started_at"),
    ("submitted_at", "submitted_at"), ("assigned_worker", "assigned_worker"),
    ("customer", "customer_name"), ("property", "property_name"), ("address", "property_address"),
    ("template", "template_name"), ("tasks_total", "items_total"), ("tasks_done", "items_done"),
    ("tasks_not_completed", "items_incomplete"), ("tasks_not_checked", "items_pending"),
    ("before_photos", "before_photos"), ("after_photos", "after_photos"), ("report_notes", "report_notes"),
    ("internal_notes", "internal_notes"), ("reviewed_by", "reviewed_by"), ("completed_at", "completed_at"),
    ("created_at", "created_at"),
]


def jobs_to_csv(rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([header for header, _ in CSV_COLUMNS])
    for row in rows:
        writer.writerow([csv_safe(row[key]) for _, key in CSV_COLUMNS])
    return "﻿" + buf.getvalue()  # BOM so Excel detects UTF-8
