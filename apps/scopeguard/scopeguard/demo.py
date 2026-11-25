"""Sample data, per-account reset and CLI commands. Everything here works on one account at a time."""
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone

import click

from . import services as s
from .db import TENANT_TABLES, get_db, seed_templates

ACTOR = "demo-seed"
DEMO_TABLES_IN_DELETE_ORDER = (
    "approvals", "events", "share_links", "revision_requests", "change_requests",
    "scope_versions", "milestones", "exclusions", "deliverables",
)


def _d(days):
    return (date.today() + timedelta(days=days)).isoformat()


def _client(db, account_id, name, email, company):
    return db.execute(
        "INSERT INTO clients (account_id, name, email, company, notes, is_demo, created_at) VALUES (?, ?, ?, ?, ?, 1, ?)",
        (account_id, name, email, company, "Sample client created by demo mode.", s.now()),
    ).lastrowid


def _project(db, client_id, name, description, deadline, allowance=2, rate=75, status="active"):
    ts = s.now()
    pid = db.execute(
        "INSERT INTO projects (account_id, client_id, name, description, status, start_date, deadline,"
        " revision_allowance, hourly_rate, currency, is_demo, created_at, updated_at)"
        " VALUES ((SELECT account_id FROM clients WHERE id = ?), ?, ?, ?, ?, ?, ?, ?, ?, 'USD', 1, ?, ?)",
        (client_id, client_id, name, description, status, _d(-20), deadline, allowance, rate, ts, ts),
    ).lastrowid
    s.log_event(db, pid, "project_created", f"Project “{name}” created (sample data).", ACTOR)
    return pid


def _items(db, pid, deliverables, exclusions, milestones):
    ids = []
    for title, desc, due in deliverables:
        ids.append(db.execute(
            "INSERT INTO deliverables (account_id, project_id, title, description, due_date, created_at)"
            " VALUES ((SELECT account_id FROM projects WHERE id = ?), ?, ?, ?, ?, ?)",
            (pid, pid, title, desc, due, s.now()),
        ).lastrowid)
    for text in exclusions:
        db.execute("INSERT INTO exclusions (account_id, project_id, text, created_at) VALUES ((SELECT account_id FROM projects WHERE id = ?), ?, ?, ?)",
                   (pid, pid, text, s.now()))
    for title, due in milestones:
        db.execute("INSERT INTO milestones (account_id, project_id, title, due_date, created_at)"
                   " VALUES ((SELECT account_id FROM projects WHERE id = ?), ?, ?, ?, ?)", (pid, pid, title, due, s.now()))
    return ids


def _sample_decision(db, pid, subject_type, subject_id, decision, comment=None):
    s.record_decision(
        db, project_id=pid, subject_type=subject_type, subject_id=subject_id, decision=decision,
        approver_name="Sample Client (demo data)", approver_email=None, method="demo_sample",
        comment=comment,
    )
    s.log_event(db, pid, "decision_recorded", f"Sample decision: {s.DECISION_LABELS[decision]} (demo data).", ACTOR)


def _cr(db, pid, title, desc, reason, hours, cost, status="submitted"):
    ts = s.now()
    cid = db.execute(
        "INSERT INTO change_requests (account_id, project_id, title, description, reason, estimated_hours,"
        " proposed_cost, status, created_at, updated_at, submitted_at, completed_at)"
        " VALUES ((SELECT account_id FROM projects WHERE id = ?), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (pid, pid, title, desc, reason, hours, cost, status, ts, ts, None if status == "draft" else ts,
         ts if status == "completed" else None),
    ).lastrowid
    s.log_event(db, pid, "change_request_created", f"Change request “{title}” created (sample data).", ACTOR)
    return cid


def seed_demo(db, account_id):
    # 1. Active website project: v1 approved, v2 awaiting review, change requests, revisions over allowance.
    c1 = _client(db, account_id, "Maya Torres", "maya@example.com", "Northwind Bakery (sample)")
    p1 = _project(db, c1, "Bakery website redesign",
                  "Five-page marketing site with online order enquiry form.", _d(30))
    d1 = _items(
        db, p1,
        [("Homepage design", "Desktop + mobile mockups, one concept.", _d(5)),
         ("Four inner pages", "About, Menu, Catering, Contact.", _d(15)),
         ("Order enquiry form", "Form that emails the shop; no payments.", _d(20))],
        ["Copywriting and photography", "Online payments / e-commerce", "Ongoing hosting and maintenance"],
        [("Design sign-off", _d(7)), ("Development complete", _d(21)), ("Launch", _d(30))],
    )
    s.publish_version(db, p1, "Initial agreed scope.", ACTOR)
    _sample_decision(db, p1, "scope_version", s.latest_version(db, p1)["id"], "approved")
    db.execute("INSERT INTO deliverables (account_id, project_id, title, description, due_date, created_at)"
               " VALUES ((SELECT account_id FROM projects WHERE id = ?), ?, 'Newsletter signup block', 'Embed of existing Mailchimp form.', ?, ?)",
               (p1, p1, _d(20), s.now()))
    s.publish_version(db, p1, "Added newsletter signup block requested on the kickoff call.", ACTOR)
    _cr(db, p1, "Blog section", "Blog listing + post template with CMS editing.",
        "Requested by the client after launch planning; blogs were not in the original brief.", 12, 900)
    cr2 = _cr(db, p1, "Extra homepage concept", "A second, alternative homepage concept.",
              "Client wants to compare two directions before sign-off.", 6, 450)
    _cr(db, p1, "Gift card page", "Static page explaining gift cards.",
        "Mentioned on the last call; not yet estimated.", None, None, status="draft")
    _sample_decision(db, p1, "change_request", cr2, "approved", "Go ahead with the second concept.")
    db.execute("UPDATE change_requests SET status = 'approved' WHERE id = ? AND account_id = ?", (cr2, account_id))
    for desc, status in (("Make the logo bigger", "done"), ("Swap hero photo", "done"), ("Try a darker palette", "open")):
        db.execute(
            "INSERT INTO revision_requests (account_id, project_id, deliverable_id, description, requested_by, status,"
            " created_at) VALUES ((SELECT account_id FROM projects WHERE id = ?), ?, ?, ?, 'Maya', ?, ?)", (p1, p1, d1[0], desc, status, s.now()))

    # 2. Branding project where the client requested changes to the scope.
    c2 = _client(db, account_id, "Sam Okafor", "sam@example.org", "Okafor Studio (sample)")
    p2 = _project(db, c2, "Brand identity refresh", "Logo refresh and a short brand guide.", _d(45), allowance=3, rate=90)
    _items(db, p2,
           [("Logo refresh", "Three directions, one refined.", _d(14)),
            ("Brand guide (8 pages)", "Colour, type, logo usage.", _d(30))],
           ["Printed collateral", "Website changes"],
           [("Logo direction chosen", _d(14))])
    s.publish_version(db, p2, "First draft of scope.", ACTOR)
    _sample_decision(db, p2, "scope_version", s.latest_version(db, p2)["id"], "changes_requested",
                     "Could the brand guide include social media templates?")

    # 3. Completed project.
    p3 = _project(db, c2, "Business card design", "Single-sided business card.", _d(-5), status="completed")
    _items(db, p3, [("Business card artwork", "Print-ready PDF.", _d(-6))], ["Printing"], [])
    s.publish_version(db, p3, "Agreed scope.", ACTOR)
    _sample_decision(db, p3, "scope_version", s.latest_version(db, p3)["id"], "approved")
    db.commit()


def remove_demo(db, account_id):
    ids = [r[0] for r in db.execute("SELECT id FROM projects WHERE is_demo = 1 AND account_id = ?", (account_id,))]
    for pid in ids:
        for table in DEMO_TABLES_IN_DELETE_ORDER:
            db.execute(f"DELETE FROM {table} WHERE project_id = ? AND account_id = ?", (pid, account_id))
        db.execute("DELETE FROM projects WHERE id = ? AND account_id = ?", (pid, account_id))
    db.execute("DELETE FROM clients WHERE is_demo = 1 AND account_id = ? AND id NOT IN (SELECT client_id FROM projects)",
               (account_id,))
    db.commit()
    return len(ids)


def backup_database(db, backup_dir, label="backup"):
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = os.path.join(backup_dir, f"scopeguard-{label}-{stamp}.db")
    target = sqlite3.connect(path)
    with target:
        db.backup(target)
    target.close()
    return path


# Delete order: rows that point at others first.
RESET_ORDER = ("approvals", "events", "share_links", "revision_requests", "change_requests", "scope_versions",
               "milestones", "exclusions", "deliverables", "projects", "clients", "message_templates")
assert set(RESET_ORDER) == set(TENANT_TABLES)


def reset_account(db, account_id, backup_dir):
    """Back up the database, then remove ONE account's records. Its login and settings are kept.
    Other accounts are never touched. Returns the backup path."""
    path = backup_database(db, backup_dir, label=f"before-reset-account{account_id}")
    try:
        # Lets the append-only triggers allow deleting this account's history, inside this transaction only.
        db.execute("INSERT OR IGNORE INTO account_purges (account_id) VALUES (?)", (account_id,))
        for table in RESET_ORDER:
            db.execute(f"DELETE FROM {table} WHERE account_id = ?", (account_id,))
        db.execute("DELETE FROM account_purges WHERE account_id = ?", (account_id,))
        seed_templates(db, account_id)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return path


def _account_id(email):
    row = get_db().execute("SELECT id FROM accounts WHERE email = ?", (email.strip().lower(),)).fetchone()
    if row is None:
        raise click.ClickException(f"No account with the email {email}.")
    return row["id"]


def register_cli(app):
    @app.cli.command("seed-demo")
    @click.argument("email")
    def seed_demo_command(email):
        """Load labelled sample data into one account."""
        seed_demo(get_db(), _account_id(email))
        click.echo("Sample data loaded.")

    @app.cli.command("remove-demo")
    @click.argument("email")
    def remove_demo_command(email):
        """Remove one account's sample projects and clients only."""
        click.echo(f"Removed {remove_demo(get_db(), _account_id(email))} sample project(s).")
