"""Demo data. Every demo record is labelled as sample data, never as a real verification."""
import random
from datetime import date, timedelta

from .db import wipe_account
from .scheduling import ensure_occurrences

DEMO_NOTE = "Demo sample record - not a real check."

CLIENTS = [
    ("Bright Bakery Co.", "Dana Lee", "dana@brightbakery.example", "Prefers updates on Tuesday mornings."),
    ("Northwind Legal", "Sam Ortiz", "sam@northwind-legal.example", "Contact form leads are business-critical."),
    ("Greenleaf Studio", "Ari Patel", "ari@greenleaf.example", ""),
]

# (client index, name, domain, platform, plan, status, notes)
WEBSITES = [
    (0, "Bright Bakery shop", "shop.brightbakery.example", "woocommerce", "premium", "active", "Online orders - test checkout monthly."),
    (0, "Bright Bakery blog", "blog.brightbakery.example", "wordpress", "basic", "active", ""),
    (1, "Northwind Legal", "northwind-legal.example", "wordpress", "standard", "active", "Three contact forms + newsletter signup."),
    (2, "Greenleaf portfolio", "greenleaf.example", "webflow", "basic", "active", "Hosting backups managed by Webflow; we verify restore points."),
    (2, "Greenleaf (old site)", "old.greenleaf.example", "static", "none", "archived", "Kept for reference only."),
]

# (website index, title, category, frequency, interval_days, days before today the schedule started)
TASKS = [
    (0, "Verify nightly backup exists", "backup_verification", "weekly", None, 70),
    (0, "Plugin & core updates", "software_updates", "weekly", None, 66),
    (0, "Test checkout and order emails", "form_testing", "monthly", None, 80),
    (0, "Performance check (PageSpeed)", "performance_check", "monthly", None, 75),
    (1, "Plugin & core updates", "software_updates", "custom", 14, 60),
    (1, "Verify backup restore point", "backup_verification", "monthly", None, 64),
    (2, "Test all contact forms", "form_testing", "weekly", None, 63),
    (2, "WordPress updates", "software_updates", "weekly", None, 68),
    (2, "Review security plugin log", "security_review", "monthly", None, 72),
    (3, "Check Webflow backup restore points", "backup_verification", "monthly", None, 78),
    (3, "Broken link & SEO check", "seo_check", "custom", 21, 55),
]

ISSUES = [
    (0, "Checkout page slow on mobile", "LCP around 4s on product pages.", "medium", "in_progress", 12, None, ""),
    (2, "Contact form email going to spam", "Submissions land in client's junk folder.", "high", "resolved", 30, 26, "Added SPF/DKIM records; client confirmed delivery."),
    (2, "Outdated theme version", "Theme vendor released a security fix.", "critical", "open", 3, None, ""),
    (1, "Broken image on About page", "", "low", "resolved", 40, 38, "Re-uploaded image."),
    (3, "Footer link to old domain", "", "low", "open", 8, None, ""),
]


def load_demo(db, upload_folder, today, account_id):
    """Replace ONE account's data with demo data."""
    wipe_account(db, account_id, upload_folder)
    rng = random.Random(42)
    client_ids = []
    for name, contact, email, notes in CLIENTS:
        cur = db.execute(
            "INSERT INTO clients (account_id, name, contact_name, contact_email, notes) VALUES (?, ?, ?, ?, ?)",
            (account_id, name, contact, email, f"[Demo] {notes}".strip()),
        )
        client_ids.append(cur.lastrowid)
    site_ids = []
    for ci, name, domain, platform, plan, status, notes in WEBSITES:
        cur = db.execute(
            "INSERT INTO websites (account_id, client_id, name, domain, platform, plan, status, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (account_id, client_ids[ci], name, domain, platform, plan, status, f"[Demo] {notes}".strip()),
        )
        site_ids.append(cur.lastrowid)
    for si, title, category, frequency, interval, ago in TASKS:
        db.execute(
            "INSERT INTO tasks (account_id, website_id, title, category, description, frequency, interval_days, "
            "start_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (account_id, site_ids[si], title, category, "Demo task.", frequency, interval,
             (today - timedelta(days=ago)).isoformat()),
        )
    db.commit()
    ensure_occurrences(db, today + timedelta(days=60), account_id=account_id)

    # Record most past occurrences as done; leave the most recent few unrecorded so
    # the dashboard has realistic overdue items.
    past = db.execute(
        "SELECT id, due_date FROM occurrences WHERE account_id = ? AND due_date < ? ORDER BY due_date",
        (account_id, today.isoformat()),
    ).fetchall()
    for occ in past:
        roll = rng.random()
        due = occ["due_date"]
        recent = due >= (today - timedelta(days=6)).isoformat()
        if recent and roll < 0.6:
            continue  # left pending -> overdue
        if roll < 0.05:
            db.execute(
                "UPDATE occurrences SET status='skipped', notes=?, recorded_by='Demo data', "
                "recorded_at=datetime('now', 'localtime') WHERE id=? AND account_id=?",
                (f"Client asked to skip this cycle. {DEMO_NOTE}", occ["id"], account_id),
            )
            continue
        result = "issues_found" if roll > 0.9 else "passed"
        note = DEMO_NOTE if result == "passed" else f"Found a problem, logged as an issue. {DEMO_NOTE}"
        done_on = min(today, date.fromisoformat(due) + timedelta(days=rng.choice([0, 0, 1, 2])))
        db.execute(
            "UPDATE occurrences SET status='completed', completed_on=?, result=?, notes=?, recorded_by='Demo data', "
            "recorded_at=datetime('now', 'localtime') WHERE id=? AND account_id=?",
            (done_on.isoformat(), result, note, occ["id"], account_id),
        )
    db.execute(
        "INSERT INTO occurrence_events (account_id, occurrence_id, action, status, completed_on, result, recorded_by, notes) "
        "SELECT account_id, id, 'recorded', status, completed_on, result, recorded_by, notes FROM occurrences "
        "WHERE account_id = ? AND status != 'pending'",
        (account_id,),
    )
    for si, title, desc, severity, status, ago, resolved_ago, resolution in ISSUES:
        db.execute(
            "INSERT INTO issues (account_id, website_id, title, description, severity, status, reported_on, "
            "resolved_on, resolution_notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                account_id, site_ids[si], title, f"{desc} [Demo]".strip(), severity, status,
                (today - timedelta(days=ago)).isoformat(),
                (today - timedelta(days=resolved_ago)).isoformat() if resolved_ago is not None else None,
                resolution,
            ),
        )
    db.commit()


