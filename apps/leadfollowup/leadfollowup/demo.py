"""Demo data for showing the product.

Demo leads only ever go into a dedicated demo account (users.is_demo = 1),
created by an explicit command. Real accounts are never touched: the command
refuses to use an email that belongs to a normal account, and --reset only
wipes accounts flagged as demo.
"""
import secrets
from datetime import timedelta

import click
from flask import current_app
from flask.cli import with_appcontext
from werkzeug.security import generate_password_hash

from .db import get_db, utcnow_iso
from .followups import today

DEMO_EMAIL = "demo@leadfollowup.test"
DEMO_BUSINESS = "Sunny Home Services"  # the header badge marks the account as a demo

# (name, phone, email, source, service, notes, status, follow-up offset in days or None,
#  [(days ago, method, summary), ...])
DEMO_LEADS = [
    ("Anna Lee", "081 234 5678", "anna.lee@example.com", "Facebook", "Move-out cleaning",
     "2-bedroom condo, moving out end of the month.", "Interested", -3,
     [(6, "Facebook", "Answered her questions about what's included. She'll confirm her moving date.")]),
    ("Khun Nok", "089 555 0101", "", "WhatsApp", "Sofa and carpet cleaning",
     "L-shaped sofa plus living-room carpet.", "Contacted", -1,
     [(4, "WhatsApp", "Sent availability for next week. No reply yet.")]),
    ("Somchai Wongsakul", "086 777 1234", "", "Phone", "Aircon servicing (3 units)",
     "Asked about servicing all three units in one visit.", "Contacted", 0,
     [(3, "Phone call", "Explained the visit takes about 2 hours. Asked to call back today.")]),
    ("Ploy Chaiyaporn", "", "ploy.c@example.com", "LINE", "Condo deep cleaning",
     "Found us through the LINE official account.", "New", 0, []),
    ("Mark Thompson", "082 345 6789", "mark.t@example.com", "Website", "Weekly office cleaning",
     "Small office, about 10 desks.", "Interested", 2,
     [(2, "Email", "Sent details about weekly visits. He's checking with his manager.")]),
    ("David Chen", "083 456 7890", "", "Referral", "Post-renovation cleaning",
     "Referred by Siriporn. Renovation finishes next week.", "Interested", 7, []),
    ("", "084 111 2222", "", "Phone", "",
     "Missed call, no voicemail left.", "New", None, []),
    ("Siriporn Kittisak", "085 222 3333", "siriporn@example.com", "Website", "Monthly home cleaning",
     "Regular customer, first Saturday of each month.", "Won", None,
     [(10, "In person", "First clean completed. Booked monthly visits.")]),
    ("James Wilson", "087 333 4444", "", "Phone", "One-off window cleaning",
     "", "Lost", None,
     [(8, "Phone call", "Decided to go with another provider.")]),
]


def seed_demo(email: str = DEMO_EMAIL, password: str | None = None, reset: bool = False) -> dict:
    """Create (or recreate) the demo account. Returns {'email', 'password', 'leads'}."""
    db = get_db()
    email = email.strip().lower()
    existing = db.execute("SELECT id, is_demo FROM users WHERE email = ?", (email,)).fetchone()
    if existing and not existing["is_demo"]:
        raise click.ClickException(f"{email} belongs to a real account. Demo data is never added to real accounts.")
    if existing and not reset:
        raise click.ClickException(f"Demo account {email} already exists. Use --reset to recreate it.")
    if existing:
        db.execute("DELETE FROM users WHERE id = ? AND is_demo = 1", (existing["id"],))  # cascades to its leads

    password = password or secrets.token_urlsafe(9)
    now = utcnow_iso()
    user_id = db.execute(
        "INSERT INTO users (email, password_hash, business_name, created_at, is_demo) VALUES (?, ?, ?, ?, 1)",
        (email, generate_password_hash(password), DEMO_BUSINESS, now),
    ).lastrowid

    day = today()
    for name, phone, lead_email, source, service, notes, status, due, history in DEMO_LEADS:
        lead_id = db.execute(
            """INSERT INTO leads (user_id, name, phone, email, source, service, notes, status,
                                  next_follow_up, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, name, phone, lead_email, source, service, notes, status,
             (day + timedelta(days=due)).isoformat() if due is not None else None, now, now),
        ).lastrowid
        for days_ago, method, summary in history:
            db.execute(
                "INSERT INTO lead_notes (lead_id, body, created_at, method, contact_date) VALUES (?, ?, ?, ?, ?)",
                (lead_id, summary, now, method, (day - timedelta(days=days_ago)).isoformat()),
            )
    db.commit()
    return {"email": email, "password": password, "leads": len(DEMO_LEADS)}


@click.command("seed-demo")
@click.option("--email", default=DEMO_EMAIL, show_default=True, help="Login email for the demo account.")
@click.option("--password", default=None, help="Password to use (a random one is generated if omitted).")
@click.option("--reset", is_flag=True, help="Delete and recreate an existing demo account.")
@with_appcontext
def seed_demo_command(email, password, reset):
    """Create a separate demo account filled with example leads."""
    result = seed_demo(email, password, reset)
    click.echo(f"Demo account ready with {result['leads']} example leads.")
    click.echo(f"  Email:    {result['email']}")
    if password:
        click.echo("  Password: (the one you provided)")
    else:
        click.echo(f"  Password: {result['password']}")
    click.echo(f"  Dates are relative to today ({today().isoformat()}, {current_app.config['APP_TIMEZONE']}).")


def init_app(app):
    app.cli.add_command(seed_demo_command)
