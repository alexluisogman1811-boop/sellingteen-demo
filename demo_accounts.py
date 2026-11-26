"""Example accounts for the DEMO site only (run by render_start.py when DEMO_MODE=1).

Each app gets a separate, clearly labelled example account full of fictional data, so visitors can
look around a working app without signing up. Real customer accounts are never touched: every app
keeps its own database, and these accounts use the reserved example.com address.

    Login for each:  demo@example.com / demo-example-123
"""
import secrets
from datetime import date, datetime, timedelta, timezone

from werkzeug.security import generate_password_hash

EMAIL = "demo@example.com"
PASSWORD = "demo-example-123"


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _d(days):
    return (date.today() + timedelta(days=days)).isoformat()


def _app_db(app):
    """The app's own sqlite connection (each of these apps exposes get_db in its db module)."""
    import importlib
    mod = importlib.import_module(app.import_name + ".db")
    return mod.get_db()


# ---------------------------------------------------------------- InvoiceChaser
def seed_invoicechaser(app):
    with app.app_context():
        db = _app_db(app)
        if db.execute("SELECT 1 FROM users WHERE email = ?", (EMAIL,)).fetchone():
            return "already there"
        uid = db.execute("INSERT INTO users (email, password_hash, business_name, sender_name, currency) VALUES (?, ?, ?, ?, 'USD')",
                         (EMAIL, generate_password_hash(PASSWORD), "Bright Spark Design (Example)", "Alex")).lastrowid
        rows = [("Harbor Cafe", "Mia", 450_00, -40, -12, None), ("Oak & Pine Studio", "Sam", 1_200_00, -20, -3, None),
                ("Northside Dental", "Dr. Lee", 780_00, -10, 5, None), ("Green Leaf Florist", "Nina", 320_00, -45, -15, _d(-2))]
        for i, (cust, contact, cents, issued, due, paid) in enumerate(rows, 1):
            cid = db.execute("INSERT INTO customers (user_id, name, contact_name, email) VALUES (?, ?, ?, ?)",
                             (uid, cust, contact, f"{contact.split()[-1].lower()}@example.com")).lastrowid
            db.execute("INSERT INTO invoices (user_id, customer_id, number, amount_cents, issue_date, due_date, paid_at, notes) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (uid, cid, f"INV-10{i:02d}", cents, _d(issued), _d(due), paid,
                                                          "Example invoice"))
        db.commit()
        return "created"


# ---------------------------------------------------------------- PayRecover
def seed_payrecover(app):
    with app.app_context():
        db = _app_db(app)
        if db.execute("SELECT 1 FROM users WHERE email = ?", (EMAIL,)).fetchone():
            return "already there"
        now = _now()
        uid = db.execute("INSERT INTO users (email, password_hash, business_name, created_at) VALUES (?, ?, ?, ?)",
                         (EMAIL, generate_password_hash(PASSWORD), "Clearflow Plumbing (Example)", now)).lastrowid
        rows = [("Daniel Park", "Park Apartments", 380_00, -30, -9, "sent", None), ("Rosa Martinez", "", 145_00, -14, -1, "sent", None),
                ("Tom Becker", "Becker Bakery", 920_00, -7, 7, "sent", None), ("Lena Fischer", "", 210_00, -25, -10, "paid", _d(-3)),
                ("Chris Young", "", 99_00, 0, 14, "draft", None)]
        for i, (name, company, cents, issued, due, status, paid) in enumerate(rows, 1):
            cid = db.execute("INSERT INTO customers (user_id, name, email, company, created_at) VALUES (?, ?, ?, ?, ?)",
                             (uid, name, f"{name.split()[0].lower()}@example.com", company, now)).lastrowid
            db.execute("INSERT INTO invoices (user_id, customer_id, number, amount_cents, issue_date, due_date, notes, status, "
                       "sent_on, paid_on, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (uid, cid, f"PR-20{i:02d}", cents, _d(issued), _d(due), "Example invoice", status,
                        _d(issued) if status != "draft" else None, paid, now))
        db.commit()
        return "created"


# ---------------------------------------------------------------- ContractorDocs
def seed_contractordocs(app):
    import os
    import importlib
    with app.app_context():
        db = _app_db(app)
        if db.execute("SELECT 1 FROM users WHERE email = ?", (EMAIL,)).fetchone():
            return "already there"
        status = importlib.import_module(app.import_name + ".status")
        make_pdf = importlib.import_module(app.import_name + ".samplepdf").make_pdf
        folder = app.config["UPLOAD_FOLDER"]
        os.makedirs(folder, exist_ok=True)
        uid = db.execute("INSERT INTO users (email, password_hash, company_name, required_types) VALUES (?, ?, ?, ?)",
                         (EMAIL, generate_password_hash(PASSWORD), "Summit Builders (Example)",
                          "|".join(status.DEFAULT_REQUIRED_TYPES))).lastrowid
        subs = {
            "Rivera Electric": ("Electrical", [("Certificate of Insurance", 200), ("Workers' Compensation", 12), ("W-9", None)]),
            "Blue Ridge Plumbing": ("Plumbing", [("Certificate of Insurance", -5), ("W-9", None)]),
            "Apex Roofing": ("Roofing", [("Certificate of Insurance", 90), ("Workers' Compensation", 150), ("W-9", None)]),
            "Lakeside Drywall": ("Drywall", []),
        }
        for name, (trade, docs) in subs.items():
            sid = db.execute("INSERT INTO subcontractors (user_id, name, trade, contact_email) VALUES (?, ?, ?, ?)",
                             (uid, name, trade, "office@example.com")).lastrowid
            for doc_type, days in docs:
                stored = secrets.token_hex(16) + ".pdf"
                exp = _d(days) if days is not None else None
                lines = ["EXAMPLE DOCUMENT - fictional sample data", f"{doc_type}", f"Insured: {name}",
                         f"Expiration date: {exp}" if exp else "No expiration date"]
                with open(os.path.join(folder, stored), "wb") as fh:
                    fh.write(make_pdf(lines))
                db.execute("INSERT INTO documents (user_id, subcontractor_id, name, doc_type, original_filename, stored_filename, "
                           "mime_type, uploaded_at, expiration_date, no_expiration, needs_review, review_reason, extraction_source) "
                           "VALUES (?, ?, ?, ?, ?, ?, 'application/pdf', ?, ?, ?, 0, '', 'manual')",
                           (uid, sid, f"{doc_type} (example)", doc_type, f"example-{doc_type.lower().replace(' ', '-')}.pdf",
                            stored, _now(), exp, 1 if exp is None else 0))
        db.commit()
        return "created"


# ---------------------------------------------------------------- LeadFollowUp (uses the app's own demo seeder)
def seed_leadfollowup(app):
    import importlib
    with app.app_context():
        demo = importlib.import_module(app.import_name + ".demo")
        try:
            demo.seed_demo(EMAIL, PASSWORD, reset=True)
        except Exception as exc:  # e.g. the address belongs to a real account: never touch it
            return f"skipped ({exc})"
        return "created"


# ---------------------------------------------------------------- QuoteFlow (a ready business profile with prices)
def seed_quoteflow(app):
    with app.app_context():
        db = _app_db(app)
        if db.execute("SELECT 1 FROM users WHERE email = ?", (EMAIL,)).fetchone():
            return "already there"
        uid = db.execute("INSERT INTO users (email, password_hash, onboarded) VALUES (?, ?, 1)",
                         (EMAIL, generate_password_hash(PASSWORD))).lastrowid
        db.execute(
            "INSERT INTO business_profiles (user_id, business_name, description, services, starting_prices, service_area, "
            "opening_hours, contact_info, currency, policies, basic_price, deep_price, bedrooms_included, bathrooms_included, "
            "extra_bedroom_price, extra_bathroom_price, addons) VALUES (?, ?, ?, ?, '', ?, ?, ?, 'USD', ?, ?, ?, ?, ?, ?, ?, ?)",
            (uid, "Sparkle Home Cleaning (Example)", "Home cleaning for apartments and houses. Fictional example business.",
             "Regular cleaning\nDeep cleaning\nMove-out cleaning", "Downtown and nearby areas",
             "Mon-Sat 8:00-18:00", "hello@example.com",
             "Free cancellation up to 24 hours before the visit.\nA regular clean takes about 2-3 hours.",
             "120", "220", "2", "1", "25", "20", "Inside oven: 45\nInside fridge: 30\nInterior windows: 40"))
        db.commit()
        return "created"


SEEDERS = {"invoicechaser": seed_invoicechaser, "payrecover": seed_payrecover,
           "contractordocs": seed_contractordocs, "leadfollowup": seed_leadfollowup, "quoteflow": seed_quoteflow}


def seed_all(build_app):
    results = {}
    for slug, fn in SEEDERS.items():
        try:
            results[slug] = fn(build_app(slug))
        except Exception as exc:  # the demo must start even if one example fails
            results[slug] = f"failed: {exc}"
    return results
