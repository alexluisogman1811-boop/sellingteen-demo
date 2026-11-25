"""Upgrade databases created before accounts existed (one business per install) to the multi-business layout.

Safety:
- The database is first copied to `<database>.pre-accounts-<timestamp>.bak` with SQLite's backup API
  (safe while the app runs in WAL mode), and the copy must pass `PRAGMA integrity_check`.
- All changes then happen in ONE transaction: one account is created for the existing business and every
  existing row is assigned to it. If anything fails, nothing is changed.
"""
import sqlite3
from datetime import datetime

TENANT_TABLES = ("users", "customers", "properties", "templates", "template_items", "jobs", "job_items",
                 "photos", "share_links", "job_events")

ACCOUNTS_TABLE = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL COLLATE NOCASE,
    is_demo INTEGER NOT NULL DEFAULT 0,
    paid_until TEXT,
    created_at TEXT NOT NULL
)"""


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}  # table names come from TENANT_TABLES


def needs_accounts_migration(conn):
    return "users" in _tables(conn) and "account_id" not in _columns(conn, "users")


def backup_database(conn, db_path):
    dest_path = f"{db_path}.pre-accounts-{datetime.now().strftime('%Y%m%d-%H%M%S')}.bak"
    dest = sqlite3.connect(dest_path)
    try:
        conn.backup(dest)
        ok = dest.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        dest.close()
    if ok != "ok":
        raise RuntimeError("The database backup failed its integrity check, so the upgrade was not started.")
    return dest_path


def migrate_to_accounts(conn, db_path, business_name):
    """Returns (backup_path, account_id or None if the old database held no data)."""
    backup_path = backup_database(conn, db_path)
    tables = _tables(conn)
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(ACCOUNTS_TABLE)
        has_data = any(conn.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone() for t in TENANT_TABLES if t in tables)
        account_id = None
        if has_data:
            owner = (conn.execute("SELECT email FROM users WHERE role = 'manager' ORDER BY id LIMIT 1").fetchone()
                     or conn.execute("SELECT email FROM users ORDER BY id LIMIT 1").fetchone())
            # The free trial of the existing business starts on the day of the upgrade.
            account_id = conn.execute(
                "INSERT INTO accounts (name, email, created_at) VALUES (?, ?, ?)",
                (business_name, owner[0] if owner else "", datetime.now().replace(microsecond=0).isoformat(sep=" ")),
            ).lastrowid
        for table in TENANT_TABLES:
            if table in tables and "account_id" not in _columns(conn, table):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN account_id INTEGER REFERENCES accounts(id)")
                if account_id is not None:
                    conn.execute(f"UPDATE {table} SET account_id = ?", (account_id,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return backup_path, account_id
