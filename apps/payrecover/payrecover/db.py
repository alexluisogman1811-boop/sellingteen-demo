import sqlite3
from datetime import date

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    business_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    company TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_customers_user ON customers(user_id);

CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    number TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    issue_date TEXT NOT NULL,
    due_date TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('draft', 'sent', 'paid')),
    sent_on TEXT,
    paid_on TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (user_id, number)
);
CREATE INDEX IF NOT EXISTS idx_invoices_user ON invoices(user_id);

CREATE TABLE IF NOT EXISTS followups (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    stage INTEGER NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('sent', 'skipped')),
    message TEXT NOT NULL DEFAULT '',
    on_date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (invoice_id, stage)
);
CREATE INDEX IF NOT EXISTS idx_followups_invoice ON followups(invoice_id);
"""


def get_db():
    if "db" not in g:
        conn = sqlite3.connect(current_app.config["DATABASE"])
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    # Subscription: the date (YYYY-MM-DD) a paid account is switched on until. Empty = not paid yet.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    if "paid_until" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN paid_until TEXT")
        conn.commit()


def init_app(app):
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db()


def today():
    """Today's date, or the fixed date configured for demos/tests."""
    fixed = current_app.config.get("FIXED_TODAY")
    if fixed:
        return fixed if isinstance(fixed, date) else date.fromisoformat(fixed)
    return date.today()
