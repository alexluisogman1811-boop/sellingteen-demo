"""SQLite access. One connection per request, stored on flask.g."""
import json
import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS business_profiles (
    user_id        INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    business_name  TEXT NOT NULL DEFAULT '',
    description    TEXT NOT NULL DEFAULT '',
    services       TEXT NOT NULL DEFAULT '',
    starting_prices TEXT NOT NULL DEFAULT '',
    service_area   TEXT NOT NULL DEFAULT '',
    opening_hours  TEXT NOT NULL DEFAULT '',
    contact_info   TEXT NOT NULL DEFAULT '',
    currency       TEXT NOT NULL DEFAULT 'USD',
    policies       TEXT NOT NULL DEFAULT '',
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS quotes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    customer_request TEXT NOT NULL,
    result_json      TEXT NOT NULL,
    reply_text       TEXT NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_quotes_user ON quotes(user_id, created_at DESC);

-- Generated quotes per user per calendar month ('YYYY-MM', UTC).
CREATE TABLE IF NOT EXISTS usage (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    month   TEXT NOT NULL,
    count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, month)
);

-- First-party analytics (see analytics.py).
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_events_name ON events(name, created_at);
"""

# Columns added after the first release. Applied with ALTER TABLE so existing
# databases upgrade in place on startup.
ADDED_COLUMNS = {
    # Subscription state. Only Stripe IDs and status - never card details.
    "users": {
        "stripe_customer_id": "TEXT NOT NULL DEFAULT ''",
        "stripe_subscription_id": "TEXT NOT NULL DEFAULT ''",
        "subscription_status": "TEXT NOT NULL DEFAULT ''",
        "subscription_period_end": "INTEGER NOT NULL DEFAULT 0",
        "cancel_at_period_end": "INTEGER NOT NULL DEFAULT 0",
        "subscription_event_at": "INTEGER NOT NULL DEFAULT 0",
        "payment_failed": "INTEGER NOT NULL DEFAULT 0",
        "payment_event_at": "INTEGER NOT NULL DEFAULT 0",
        "onboarded": "INTEGER NOT NULL DEFAULT 0",
        # Manual Pro activation (billing.py `flask activate`): YYYY-MM-DD, NULL = never paid.
        "paid_until": "TEXT",
    },
    "business_profiles": {
        "basic_price": "TEXT NOT NULL DEFAULT ''",
        "deep_price": "TEXT NOT NULL DEFAULT ''",
        "bedrooms_included": "TEXT NOT NULL DEFAULT ''",
        "bathrooms_included": "TEXT NOT NULL DEFAULT ''",
        "extra_bedroom_price": "TEXT NOT NULL DEFAULT ''",
        "extra_bathroom_price": "TEXT NOT NULL DEFAULT ''",
        "addons": "TEXT NOT NULL DEFAULT ''",
    },
    "quotes": {
        "price": "TEXT NOT NULL DEFAULT ''",
        "duration": "TEXT NOT NULL DEFAULT ''",
        "services": "TEXT NOT NULL DEFAULT ''",
        "status": "TEXT NOT NULL DEFAULT 'draft'",
    },
}

QUOTE_STATUSES = {"draft": "Draft", "saved": "Saved", "sent": "Sent"}

PROFILE_FIELDS = [
    "business_name",
    "description",
    "services",
    "starting_prices",
    "service_area",
    "opening_hours",
    "contact_info",
    "currency",
    "policies",
    *ADDED_COLUMNS["business_profiles"],
]


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE_PATH"], timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(app):
    app.teardown_appcontext(close_db)
    with app.app_context():
        db = get_db()
        # WAL lets reads continue while a write is in progress (several server threads).
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript(SCHEMA)
        _migrate(db)
        db.commit()


def _migrate(db):
    added = set()
    for table, columns in ADDED_COLUMNS.items():
        existing = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns.items():
            if name not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                added.add(name)
    if "onboarded" in added:
        # Businesses that already filled in a profile don't need onboarding.
        db.execute("UPDATE users SET onboarded = 1 WHERE id IN "
                   "(SELECT user_id FROM business_profiles WHERE business_name != '')")
    if "price" not in added:
        return
    # One-time backfill for quotes created before price/duration/services were columns.
    for q in db.execute("SELECT id, result_json FROM quotes").fetchall():
        r = json.loads(q["result_json"])
        db.execute(
            "UPDATE quotes SET price = ?, duration = ?, services = ? WHERE id = ?",
            (r.get("suggested_price", ""), r.get("estimated_time", ""),
             "\n".join(r.get("services_included", [])), q["id"]),
        )


def get_profile(user_id):
    row = get_db().execute(
        "SELECT * FROM business_profiles WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is None:
        return {f: ("USD" if f == "currency" else "") for f in PROFILE_FIELDS}
    return {f: row[f] for f in PROFILE_FIELDS}


def save_profile(user_id, data):
    values = [data.get(f, "").strip() for f in PROFILE_FIELDS]
    db = get_db()
    db.execute(
        f"""INSERT INTO business_profiles (user_id, {", ".join(PROFILE_FIELDS)}, updated_at)
            VALUES (?, {", ".join("?" for _ in PROFILE_FIELDS)}, datetime('now'))
            ON CONFLICT(user_id) DO UPDATE SET
            {", ".join(f"{f} = excluded.{f}" for f in PROFILE_FIELDS)},
            updated_at = datetime('now')""",
        [user_id, *values],
    )
    db.commit()
