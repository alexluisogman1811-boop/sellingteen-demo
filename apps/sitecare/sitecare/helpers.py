"""Small shared helpers."""
import os
from datetime import date, timedelta

from flask import abort, current_app, g

from .scheduling import ensure_occurrences


def today():
    fixed = current_app.config.get("FIXED_TODAY")
    return fixed if fixed else date.today()


def horizon():
    return today() + timedelta(days=current_app.config["SCHEDULE_HORIZON_DAYS"])


def aid():
    """The signed-in account's id. Every query on studio data filters by it."""
    return g.account["id"]


def sync_schedule(db, until=None):
    ensure_occurrences(db, until or horizon(), account_id=aid())


def like(term):
    """Build a LIKE pattern that treats % and _ in user input literally (use with ESCAPE '\\')."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def fetch_or_404(db, sql, params):
    row = db.execute(sql, params).fetchone()
    if row is None:
        abort(404)
    return row


def int_arg(args, name):
    """Return a positive int query arg or None (bad values are ignored, not errors)."""
    value = str(args.get(name) or "")
    return int(value) if value.isdigit() else None


def attachment_files(db, where, params):
    """Stored filenames for this account's attachments matching a join filter (before a cascade delete)."""
    rows = db.execute(
        "SELECT a.stored_name FROM attachments a "
        "JOIN occurrences o ON o.id = a.occurrence_id "
        "JOIN tasks t ON t.id = o.task_id "
        "JOIN websites w ON w.id = t.website_id "
        f"WHERE a.account_id = ? AND ({where})",
        (aid(), *params),
    ).fetchall()
    return [r["stored_name"] for r in rows]


def remove_files(names):
    folder = current_app.config["UPLOAD_FOLDER"]
    for name in names:
        path = os.path.join(folder, os.path.basename(name))
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            current_app.logger.warning("Could not remove evidence file %s", name)


def verification_key(occ):
    """How a recorded result is known. There are no integrations, so never 'verified'."""
    return "manual_evidence" if occ["attachment_count"] else "manual"


def log_event(db, occ_id, action):
    """Append a snapshot of the occurrence to its audit trail (caller commits)."""
    db.execute(
        "INSERT INTO occurrence_events (account_id, occurrence_id, action, status, completed_on, result, recorded_by, "
        "notes, evidence_count) "
        "SELECT o.account_id, o.id, ?, o.status, o.completed_on, o.result, o.recorded_by, o.notes, "
        "(SELECT COUNT(*) FROM attachments a WHERE a.occurrence_id = o.id) FROM occurrences o WHERE o.id = ?",
        (action, occ_id),
    )


OCCURRENCE_SELECT = """
SELECT o.*, t.title, t.category, t.frequency, t.interval_days, t.website_id,
       w.name AS website_name, w.domain, w.client_id, c.name AS client_name,
       (SELECT COUNT(*) FROM attachments a WHERE a.occurrence_id = o.id) AS attachment_count
FROM occurrences o
JOIN tasks t ON t.id = o.task_id
JOIN websites w ON w.id = t.website_id
JOIN clients c ON c.id = w.client_id
"""
