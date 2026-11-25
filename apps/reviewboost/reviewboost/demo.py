"""Demo data: fictional electrician business and customers.

`POST /demo` gives every visitor their own private, freshly seeded demo account, so
visitors never see each other's changes. Demo accounts are deleted after a day.
All names are fictional; phone numbers use the reserved 555-01xx range.
"""
import secrets
from datetime import date, timedelta

from flask import Blueprint, abort, current_app, flash, redirect, render_template, url_for
from werkzeug.security import generate_password_hash

from .ai import AIError, generate_review_request
from .auth import DEFAULT_TEMPLATES, start_session
from .db import get_db

bp = Blueprint("demo", __name__)

DEMO_BUSINESS_NAME = "Maple Street Electric"
DEMO_REVIEW_LINK = "https://example.com/reviews/maple-street-electric"
MAX_DEMOS_PER_DAY = 500

# (name, phone, email, service, days_ago, status, has_message)
DEMO_CUSTOMERS = [
    ("Maria Gonzalez", "(512) 555-0101", "maria.gonzalez@example.com", "EV charger install", 1, "not_contacted", False),
    ("Tom Becker", "(512) 555-0102", "", "Outlet and switch repair", 2, "not_contacted", False),
    ("Dr. Priya Patel", "(512) 555-0103", "priya.patel@example.com", "Panel upgrade", 3, "not_contacted", True),
    ("Kevin O'Connor", "(512) 555-0104", "", "Ceiling fan installation", 5, "contacted", True),
    ("Lisa Chen", "(512) 555-0105", "lisa.chen@example.com", "Recessed lighting install", 7, "contacted", True),
    ("James Walker", "(512) 555-0106", "", "Smoke detector replacement", 9, "contacted", True),
    ("Angela Brooks", "(512) 555-0107", "angela.brooks@example.com", "Whole-home surge protector", 12, "review_received", True),
    ("Daniel Kim", "(512) 555-0108", "", "Generator hookup", 15, "review_received", True),
    ("Rosa Martinez", "(512) 555-0109", "rosa.martinez@example.com", "Kitchen lighting upgrade", 20, "review_received", True),
]


def seed_customers(db, business_id, business_name=DEMO_BUSINESS_NAME, review_link=DEMO_REVIEW_LINK):
    """Replace a business's customers and templates with the demo set."""
    db.execute("DELETE FROM customers WHERE business_id = ?", (business_id,))
    db.execute("DELETE FROM message_templates WHERE business_id = ?", (business_id,))
    db.executemany(
        "INSERT INTO message_templates (business_id, name, body) VALUES (?, ?, ?)",
        [(business_id, n, b) for n, b in DEFAULT_TEMPLATES],
    )
    business = {"name": business_name, "review_link": review_link}
    today = date.today()
    for i, (name, phone, email, service, days_ago, status, has_message) in enumerate(DEMO_CUSTOMERS):
        customer = {"name": name, "phone": phone, "email": email, "service": service,
                    "job_date": (today - timedelta(days=days_ago)).isoformat()}
        message = None
        if has_message:
            try:
                message, _ = generate_review_request({"AI_MOCK": "1"}, business, customer, seed=i)
            except AIError:
                message = None
        db.execute(
            """INSERT INTO customers (business_id, name, phone, email, service, job_date, status,
                   message, message_generated_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, CASE WHEN ? IS NULL THEN NULL ELSE datetime('now', ?) END,
                       datetime('now', ?), datetime('now', ?))""",
            (business_id, name, phone, email, service, customer["job_date"], status, message,
             message, f"-{days_ago} days", f"-{days_ago} days", f"-{days_ago} days"),
        )


TEMP_DEMO_EMAIL_DOMAIN = "@demo.invalid"


def create_demo_business(db):
    # Clean up old visitor demos first (customers cascade). The seeded demo login is kept.
    temp = f"is_demo = 1 AND email LIKE '%{TEMP_DEMO_EMAIL_DOMAIN}'"
    db.execute(f"DELETE FROM businesses WHERE {temp} AND created_at < datetime('now', '-1 day')")
    recent = db.execute(f"SELECT COUNT(*) FROM businesses WHERE {temp}").fetchone()[0]
    if recent >= MAX_DEMOS_PER_DAY:
        return None
    token = secrets.token_hex(8)
    cur = db.execute(
        "INSERT INTO businesses (name, email, password_hash, review_link, is_demo) VALUES (?, ?, ?, ?, 1)",
        # Random, never-shown password: demo accounts can only be entered via the demo button.
        (DEMO_BUSINESS_NAME, f"demo-{token}{TEMP_DEMO_EMAIL_DOMAIN}",
         generate_password_hash(secrets.token_urlsafe(32)), DEMO_REVIEW_LINK),
    )
    seed_customers(db, cur.lastrowid)
    return cur.lastrowid


@bp.route("/demo", methods=["POST"])
def start_demo():
    if not current_app.config.get("DEMO_MODE"):
        abort(404)
    db = get_db()
    business_id = create_demo_business(db)
    db.commit()
    if business_id is None:
        flash("The demo is busy right now. Please try again later, or create your own account.", "error")
        return redirect(url_for("customers.index"))
    start_session(business_id)
    return redirect(url_for("customers.dashboard"))


@bp.route("/demo", methods=["GET"])
def demo_info():
    # Someone typed /demo into the address bar: send them to the page with the demo button.
    return redirect(url_for("customers.index"))


def landing_example():
    """Sample message for the landing page, built by the same mock generator the app uses."""
    business = {"name": DEMO_BUSINESS_NAME, "review_link": DEMO_REVIEW_LINK}
    customer = {"name": "Maria Gonzalez", "phone": "", "email": "", "service": "EV charger install",
                "job_date": (date.today() - timedelta(days=1)).isoformat()}
    return generate_review_request({"AI_MOCK": "1"}, business, customer, seed=2)[0]


def render_landing():
    return render_template("landing.html", example=landing_example(),
                           demo_enabled=current_app.config.get("DEMO_MODE"))
