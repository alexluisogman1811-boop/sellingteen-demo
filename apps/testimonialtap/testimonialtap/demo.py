"""Clearly labelled sample data for trying the app.

Demo rows use obvious placeholder identities ("Sample Client A") and text that
states it is not a real review. They are flagged is_demo=1, shown with a DEMO
badge everywhere (including public pages) and removed by the reset action.
"""
from .db import get_db, log_activity, utcnow
from .security import new_public_token

DEMO_NOTE = "[Demo data] Placeholder text, not a real customer review. "

DEMO_PROJECTS = [
    {
        "name": "[DEMO] Website redesign",
        "client_name": "Sample Client A (demo)",
        "description": "Sample project created by the demo loader.",
        "intro_message": "This is a demo form. Submissions here are marked as demo data "
                         "and are deleted when demo data is reset.",
        "testimonials": [
            ("Sample Person 1", "Example Co. (demo)", "shows how an approved testimonial with "
             "publication permission appears on the public page.", 5, "https://example.com",
             "approved", "submitter"),
            ("Sample Person 2", "", "shows an approved testimonial without publication "
             "permission. It is hidden from the public page.", None, "", "approved", ""),
            ("Sample Person 3", "Example Studio (demo)", "shows a pending submission waiting "
             "for review.", 4, "", "pending", "submitter"),
        ],
    },
    {
        "name": "[DEMO] Monthly SEO retainer",
        "client_name": "Sample Client B (demo)",
        "description": "Second sample project.",
        "intro_message": "",
        "testimonials": [
            ("Sample Person 4", "Example Org (demo)", "shows a testimonial whose permission "
             "was recorded by the owner after an email exchange.", 3, "", "approved", "owner"),
            ("Sample Person 5", "", "shows a rejected submission.", None, "", "rejected", ""),
            ("Sample Person 6", "", "shows an archived testimonial.", 4, "", "archived", "submitter"),
        ],
    },
]


def has_demo_data(account_id):
    db = get_db()
    return bool(
        db.execute("SELECT 1 FROM projects WHERE is_demo = 1 AND account_id = ? LIMIT 1", (account_id,)).fetchone()
        or db.execute("SELECT 1 FROM testimonials WHERE is_demo = 1 AND account_id = ? LIMIT 1",
                      (account_id,)).fetchone()
    )


def reset_demo_data(account_id, reload=True):
    """Delete this account's demo rows, then optionally load fresh ones.

    Never touches real data, and never touches another account's demo data.
    """
    db = get_db()
    for table in ("testimonials", "projects", "activity"):
        db.execute(f"DELETE FROM {table} WHERE is_demo = 1 AND account_id = ?", (account_id,))
    if reload:
        _load(db, account_id)
    db.commit()


def _load(db, account_id):
    now = utcnow()
    for spec in DEMO_PROJECTS:
        cur = db.execute(
            "INSERT INTO projects (account_id, name, client_name, description, intro_message, submit_token, "
            "wall_token, accepting, is_demo, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?)",
            (account_id, spec["name"], spec["client_name"], spec["description"], spec["intro_message"],
             new_public_token(), new_public_token(), now, now),
        )
        project_id = cur.lastrowid
        for name, business, text, rating, website, status, source in spec["testimonials"]:
            note = {
                "submitter": "Demo data: permission checkbox ticked on the sample form.",
                "owner": "Demo data: example of permission recorded by the owner.",
            }.get(source, "")
            t = db.execute(
                "INSERT INTO testimonials (account_id, project_id, customer_name, business, feedback, rating, "
                "website, status, permission_granted, permission_source, permission_note, permission_at, is_demo, "
                "submitted_at, status_changed_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (account_id, project_id, name + " (demo)", business, DEMO_NOTE + "This sample " + text, rating, website,
                 status, 1 if source else 0, source, note, now if source else None, now,
                 None if status == "pending" else now, now),
            )
            log_activity("demo", f"Demo testimonial from {name} (demo) added as {status}",
                         project_id=project_id, testimonial_id=t.lastrowid, is_demo=True,
                         account_id=account_id)
