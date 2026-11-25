"""Shared queries and display helpers."""
import json
from datetime import date

from .db import get_db

JOB_STATUSES = {"draft": "Draft", "in_progress": "In Progress", "completed": "Completed"}
REPORT_STATUSES = {"draft": "Draft", "generated": "Generated", "shared": "Shared"}
PHOTO_STAGES = {"before": "Before", "during": "During", "after": "After"}
SERVICE_TYPES = [
    "Plumbing", "Electrical", "HVAC", "Cleaning", "Landscaping", "Painting",
    "Handyman", "Carpentry", "Roofing", "General Contracting", "Other",
]

# A report status is derived: no report -> draft, report -> generated, report + live link -> shared.
REPORT_STATUS_SQL = """
    CASE
      WHEN r.id IS NULL THEN 'draft'
      WHEN EXISTS (SELECT 1 FROM share_links s WHERE s.report_id = r.id AND s.revoked_at IS NULL) THEN 'shared'
      ELSE 'generated'
    END
"""


def get_report(job_id):
    row = get_db().execute("SELECT * FROM reports WHERE job_id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    report = dict(row)
    report["content"] = json.loads(row["content_json"])
    return report


def get_active_share(report_id):
    return get_db().execute(
        "SELECT * FROM share_links WHERE report_id = ? AND revoked_at IS NULL ORDER BY id DESC LIMIT 1",
        (report_id,),
    ).fetchone()


def report_status(report):
    if report is None:
        return "draft"
    return "shared" if get_active_share(report["id"]) else "generated"


def get_photos(job_id):
    return get_db().execute(
        "SELECT * FROM photos WHERE job_id = ? ORDER BY position, id", (job_id,)
    ).fetchall()


def photos_by_stage(photos):
    grouped = {stage: [] for stage in PHOTO_STAGES}
    for p in photos:
        grouped[p["stage"]].append(p)
    return grouped


def format_date(value):
    if not value:
        return ""
    try:
        d = date.fromisoformat(value[:10])
    except ValueError:
        return value
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def format_datetime(value):
    if not value:
        return ""
    return format_date(value[:10])
