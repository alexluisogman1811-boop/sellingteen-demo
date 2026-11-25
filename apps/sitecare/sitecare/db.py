"""SQLite connection handling, schema and the move to multiple accounts.

Every table that holds a studio's data carries `account_id`, and every query filters by
the signed-in account. Databases from the single-owner version are converted on startup
(after a backup copy is written next to the database): all existing records go to account
#1, which keeps the old owner password.
"""
import os
import secrets
import sqlite3
from datetime import datetime

import click
from flask import current_app, g

SCHEMA_TABLES = """
CREATE TABLE IF NOT EXISTS accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    email           TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    session_version TEXT NOT NULL DEFAULT '',
    is_demo         INTEGER NOT NULL DEFAULT 0,
    paid_until      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS clients (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    contact_name  TEXT NOT NULL DEFAULT '',
    contact_email TEXT NOT NULL DEFAULT '',
    notes         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS websites (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    client_id  INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    domain     TEXT NOT NULL,
    platform   TEXT NOT NULL,
    plan       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active',
    notes      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (account_id, domain)
);

-- A recurring maintenance task definition.
CREATE TABLE IF NOT EXISTS tasks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id        INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    website_id        INTEGER NOT NULL REFERENCES websites(id) ON DELETE CASCADE,
    title             TEXT NOT NULL,
    category          TEXT NOT NULL,
    description       TEXT NOT NULL DEFAULT '',
    frequency         TEXT NOT NULL,
    interval_days     INTEGER,
    start_date        TEXT NOT NULL,
    active            INTEGER NOT NULL DEFAULT 1,
    generated_through TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One scheduled instance of a task. Results are only ever set by a person.
CREATE TABLE IF NOT EXISTS occurrences (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    task_id      INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    due_date     TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    completed_on TEXT,
    result       TEXT,
    notes        TEXT NOT NULL DEFAULT '',
    recorded_at  TEXT,
    recorded_by  TEXT NOT NULL DEFAULT '',
    UNIQUE (task_id, due_date)
);

-- Append-only trail of every record, correction, skip and reopen.
CREATE TABLE IF NOT EXISTS occurrence_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    occurrence_id INTEGER NOT NULL REFERENCES occurrences(id) ON DELETE CASCADE,
    action        TEXT NOT NULL,
    status        TEXT NOT NULL,
    completed_on  TEXT,
    result        TEXT,
    recorded_by   TEXT NOT NULL DEFAULT '',
    notes         TEXT NOT NULL DEFAULT '',
    evidence_count INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS attachments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    occurrence_id INTEGER NOT NULL REFERENCES occurrences(id) ON DELETE CASCADE,
    original_name TEXT NOT NULL,
    stored_name   TEXT NOT NULL UNIQUE,
    content_type  TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    sha256        TEXT,
    uploaded_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS issues (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id       INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    website_id       INTEGER NOT NULL REFERENCES websites(id) ON DELETE CASCADE,
    title            TEXT NOT NULL,
    description      TEXT NOT NULL DEFAULT '',
    severity         TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'open',
    reported_on      TEXT NOT NULL,
    resolved_on      TEXT,
    resolution_notes TEXT NOT NULL DEFAULT '',
    occurrence_id    INTEGER REFERENCES occurrences(id) ON DELETE SET NULL,
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- App-level settings (no longer used for credentials; kept for older databases).
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Frozen copies of client reports exactly as generated, so previous reports can be
-- re-opened (and optionally shared via an unguessable link) without changing.
CREATE TABLE IF NOT EXISTS saved_reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    client_id    INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    website_id   INTEGER REFERENCES websites(id) ON DELETE SET NULL,
    period_start TEXT NOT NULL,
    period_end   TEXT NOT NULL,
    title        TEXT NOT NULL,
    summary      TEXT NOT NULL,
    html         TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    share_token  TEXT UNIQUE,
    created_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""

SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_occ_due ON occurrences(status, due_date);
CREATE INDEX IF NOT EXISTS idx_occ_done ON occurrences(completed_on);
CREATE INDEX IF NOT EXISTS idx_tasks_site ON tasks(website_id);
CREATE INDEX IF NOT EXISTS idx_issues_site ON issues(website_id);
CREATE INDEX IF NOT EXISTS idx_events_occ ON occurrence_events(occurrence_id);
CREATE INDEX IF NOT EXISTS idx_saved_client ON saved_reports(client_id);
CREATE INDEX IF NOT EXISTS idx_clients_account ON clients(account_id);
CREATE INDEX IF NOT EXISTS idx_websites_account ON websites(account_id);
CREATE INDEX IF NOT EXISTS idx_tasks_account ON tasks(account_id);
CREATE INDEX IF NOT EXISTS idx_occ_account ON occurrences(account_id, status, due_date);
CREATE INDEX IF NOT EXISTS idx_events_account ON occurrence_events(account_id);
CREATE INDEX IF NOT EXISTS idx_attachments_account ON attachments(account_id);
CREATE INDEX IF NOT EXISTS idx_issues_account ON issues(account_id);
CREATE INDEX IF NOT EXISTS idx_saved_account ON saved_reports(account_id);
"""

# Columns added after the first release. Databases created by v1 get them on startup.
MIGRATIONS = (
    ("occurrences", "recorded_by", "TEXT NOT NULL DEFAULT ''"),
    ("attachments", "sha256", "TEXT"),
    ("issues", "occurrence_id", "INTEGER REFERENCES occurrences(id) ON DELETE SET NULL"),
)

# Tables holding one account's data, children before parents (delete order).
ACCOUNT_TABLES = ("saved_reports", "occurrence_events", "attachments", "occurrences", "issues", "tasks",
                  "websites", "clients")

# Sign-in email given to the single owner when an older database is converted.
LEGACY_OWNER_EMAIL = "owner@sitecare.local"
# A hash no password can ever match (used when an old database had data but no password).
UNUSABLE_PASSWORD = "!disabled"


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def _columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}  # constant table names


def _table_exists(conn, table):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is not None


def needs_account_migration(conn):
    return _table_exists(conn, "clients") and "account_id" not in _columns(conn, "clients")


def backup_database(conn, path):
    """Write a complete copy of the database before it is converted. Returns the copy's path."""
    folder = os.path.join(os.path.dirname(os.path.abspath(path)), "backups")
    os.makedirs(folder, exist_ok=True)
    stem = os.path.splitext(os.path.basename(path))[0]
    dest = os.path.join(folder, f"{stem}-before-accounts-{datetime.now():%Y%m%d-%H%M%S}.sqlite3")
    target = sqlite3.connect(dest)
    try:
        conn.backup(target)
    finally:
        target.close()
    return dest


def _migrate_to_accounts(conn, owner_email=None):
    """Give every row an account. Runs in one transaction; foreign keys are checked at the end."""
    conn.commit()
    conn.isolation_level = None  # manage the transaction by hand (DDL included)
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN")
        has_data = any(
            _table_exists(conn, t) and conn.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone()
            for t in ACCOUNT_TABLES
        )
        owner_hash = None
        if _table_exists(conn, "app_settings"):
            row = conn.execute("SELECT value FROM app_settings WHERE key = 'owner_password'").fetchone()
            owner_hash = row["value"] if row else None
        account_id = None
        if has_data or owner_hash:
            email = (owner_email or LEGACY_OWNER_EMAIL).strip().lower()
            account_id = conn.execute(
                "INSERT INTO accounts (name, email, password_hash, session_version) VALUES (?, ?, ?, ?)",
                ("My studio", email, owner_hash or UNUSABLE_PASSWORD, secrets.token_hex(8)),
            ).lastrowid
        for table in ACCOUNT_TABLES:
            if table == "websites" or not _table_exists(conn, table) or "account_id" in _columns(conn, table):
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE")
            conn.execute(f"UPDATE {table} SET account_id = ?", (account_id,))
        # Websites are rebuilt: a domain must now be unique per account, not across every account.
        websites_ddl = SCHEMA_TABLES.split("CREATE TABLE IF NOT EXISTS websites")[1].split(";")[0]
        conn.execute("CREATE TABLE websites_new" + websites_ddl)
        conn.execute(
            "INSERT INTO websites_new (id, account_id, client_id, name, domain, platform, plan, status, notes, created_at) "
            "SELECT id, ?, client_id, name, domain, platform, plan, status, notes, created_at FROM websites",
            (account_id,),
        )
        conn.execute("DROP TABLE websites")
        conn.execute("ALTER TABLE websites_new RENAME TO websites")
        if _table_exists(conn, "app_settings"):
            conn.execute("DELETE FROM app_settings WHERE key IN ('owner_password', 'auth_version')")
        problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise sqlite3.IntegrityError(f"Foreign key problems after migration: {len(problems)} rows")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.isolation_level = ""


def init_schema(path, owner_email=None):
    """Create or upgrade the database. Returns the backup path if an old database was converted."""
    conn = connect(path)
    backup = None
    try:
        legacy = needs_account_migration(conn)
        if legacy:
            backup = backup_database(conn, path)
        conn.executescript(SCHEMA_TABLES)
        for table, column, ddl in MIGRATIONS:
            if column not in _columns(conn, table):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")  # constants only
        conn.commit()
        if legacy:
            _migrate_to_accounts(conn, owner_email)
        conn.executescript(SCHEMA_INDEXES)
        conn.commit()
    finally:
        conn.close()
    return backup


def wipe_account(db, account_id, upload_folder):
    """Delete every record and evidence file belonging to ONE account (the account itself stays)."""
    names = [r["stored_name"] for r in db.execute(
        "SELECT stored_name FROM attachments WHERE account_id = ?", (account_id,))]
    for table in ACCOUNT_TABLES:
        db.execute(f"DELETE FROM {table} WHERE account_id = ?", (account_id,))  # constant table names
    db.commit()
    for name in names:
        path = os.path.join(upload_folder, os.path.basename(name))
        if os.path.isfile(path):
            os.remove(path)


def resolve_account(db, email):
    """CLI helper: the account for `email`, or the only account when no email is given."""
    if email:
        row = db.execute("SELECT * FROM accounts WHERE email = ?", (email.strip().lower(),)).fetchone()
        if row is None:
            raise click.ClickException(f"No account with the email {email}.")
        return row
    rows = db.execute("SELECT * FROM accounts ORDER BY id LIMIT 2").fetchall()
    if len(rows) != 1:
        raise click.ClickException("Give the account's email, e.g. ... owner@example.com")
    return rows[0]


def init_app(app):
    backup = init_schema(app.config["DATABASE"], app.config.get("OWNER_EMAIL"))
    if backup:
        app.config["MIGRATION_BACKUP"] = backup
        app.logger.warning("Converted the database to accounts. Backup of the old database: %s", backup)
    app.teardown_appcontext(close_db)

    @app.cli.command("reset-db")
    @click.argument("email", required=False)
    def reset_db_command(email):
        """Delete one account's data and evidence files (the account stays)."""
        account = resolve_account(get_db(), email)
        wipe_account(get_db(), account["id"], app.config["UPLOAD_FOLDER"])
        click.echo(f"All SiteCare Desk data for {account['email']} deleted.")

    @app.cli.command("set-password")
    @click.argument("email", required=False)
    @click.option("--password", prompt="New password", hide_input=True, confirmation_prompt=True)
    def set_password_command(email, password):
        """Set (or reset) an account's password. Signs out its existing sessions."""
        from .auth import set_password

        account = resolve_account(get_db(), email)
        try:
            set_password(get_db(), account["id"], password)
        except ValueError as exc:
            raise click.ClickException(str(exc))
        click.echo(f"Password saved for {account['email']}. Existing sessions were signed out.")

    @app.cli.command("load-demo")
    @click.argument("email", required=False)
    def load_demo_command(email):
        """Replace one account's data with demo data."""
        from .demo import load_demo
        from .helpers import today

        account = resolve_account(get_db(), email)
        load_demo(get_db(), app.config["UPLOAD_FOLDER"], today(), account["id"])
        click.echo(f"Demo data loaded into {account['email']}.")
