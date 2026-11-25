import sqlite3
from datetime import datetime, timezone

import click
from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id         INTEGER PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS businesses (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    name         TEXT NOT NULL DEFAULT '',
    contact_name TEXT NOT NULL DEFAULT '',
    phone        TEXT NOT NULL DEFAULT '',
    email        TEXT NOT NULL DEFAULT '',
    address      TEXT NOT NULL DEFAULT '',
    logo_file    TEXT,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id             INTEGER PRIMARY KEY,
    business_id    INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    customer_name  TEXT NOT NULL,
    customer_phone TEXT NOT NULL DEFAULT '',
    customer_email TEXT NOT NULL DEFAULT '',
    title          TEXT NOT NULL,
    address        TEXT NOT NULL DEFAULT '',
    service_type   TEXT NOT NULL DEFAULT '',
    job_date       TEXT NOT NULL DEFAULT '',
    technician     TEXT NOT NULL DEFAULT '',
    notes          TEXT NOT NULL DEFAULT '',
    materials      TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'draft'
                   CHECK (status IN ('draft', 'in_progress', 'completed')),
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_business_created ON jobs(business_id, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_business_status  ON jobs(business_id, status);
CREATE INDEX IF NOT EXISTS idx_jobs_business_customer ON jobs(business_id, customer_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_jobs_business_title   ON jobs(business_id, title COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS photos (
    id         INTEGER PRIMARY KEY,
    job_id     INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    file_key   TEXT NOT NULL UNIQUE,
    ext        TEXT NOT NULL CHECK (ext IN ('jpg', 'png')),
    stage      TEXT NOT NULL CHECK (stage IN ('before', 'during', 'after')),
    position   INTEGER NOT NULL,
    width      INTEGER NOT NULL,
    height     INTEGER NOT NULL,
    size_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_photos_job ON photos(job_id, position);

CREATE TABLE IF NOT EXISTS reports (
    id               INTEGER PRIMARY KEY,
    job_id           INTEGER NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
    content_json     TEXT NOT NULL,
    provider         TEXT NOT NULL,
    generation_count INTEGER NOT NULL DEFAULT 1,
    generated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS share_links (
    id         INTEGER PRIMARY KEY,
    report_id  INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    token      TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_share_links_report ON share_links(report_id);
"""


def iso(dt):
    """Fixed-width UTC timestamp so string comparison matches time order."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def now_iso():
    return iso(datetime.now(timezone.utc))


def get_db():
    if "db" not in g:
        conn = sqlite3.connect(current_app.config["DATABASE"], timeout=10)
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

    @app.cli.command("init-db")
    def init_db_command():
        """Create database tables if they do not exist."""
        init_db()
        click.echo("Database ready.")
