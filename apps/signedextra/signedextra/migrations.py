"""Versioned schema migrations.

schema.sql is the baseline (version 1) and is only ever run with CREATE ... IF NOT EXISTS.
Every later change to the database goes through MIGRATIONS below:

- the current version is stored in the schema_migrations table;
- before an existing database is changed, a copy is saved to <instance>/backups/;
- each migration runs in its own transaction and is recorded when it succeeds;
- migrations never touch audit_events, and the append-only triggers are checked afterwards.

To add a change: append (next_version, "short name", function) to MIGRATIONS. Never edit or
reorder a migration that has already shipped.
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

BASELINE_VERSION = 1
AUDIT_TRIGGERS = ("audit_no_update", "audit_no_delete")


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn, table, column, definition):
    # Guard makes a half-applied database safe to migrate again.
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _v2_company_billing(conn):
    _add_column(conn, "companies", "paid_until", "TEXT")


def _v3_password_resets(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS password_resets (
            id          INTEGER PRIMARY KEY,
            company_id  INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash  TEXT NOT NULL UNIQUE,
            created_by  TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL,
            used_at     TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_password_resets_user ON password_resets(user_id)")


def _v4_rate_events(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rate_events (
            id          INTEGER PRIMARY KEY,
            bucket      TEXT NOT NULL,
            key         TEXT NOT NULL,
            created_at  TEXT NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rate_events ON rate_events(bucket, key, created_at)")


MIGRATIONS = [
    (2, "company paid_until (trial and subscription)", _v2_company_billing),
    (3, "password_resets table", _v3_password_resets),
    (4, "rate_events table (per-IP rate limits)", _v4_rate_events),
]
LATEST_VERSION = MIGRATIONS[-1][0] if MIGRATIONS else BASELINE_VERSION


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def current_version(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)""")
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return row[0] or 0


def _has_user_data(conn):
    try:
        return conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] > 0
    except sqlite3.OperationalError:
        return False


def backup_database(conn, db_path, backup_dir, label):
    """Copy the live database with SQLite's online backup (safe in WAL mode). Returns the path."""
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"{Path(db_path).stem}-{label}-{stamp}.sqlite3"
    n = 1
    while target.exists():
        target = backup_dir / f"{Path(db_path).stem}-{label}-{stamp}-{n}.sqlite3"
        n += 1
    dest = sqlite3.connect(target)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return target


def check_audit_triggers(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    missing = [t for t in AUDIT_TRIGGERS if t not in names]
    if missing:
        raise RuntimeError(f"Audit log protection is missing ({', '.join(missing)}); refusing to start.")


def migrate(conn, db_path, backup_dir):
    """Bring the database up to LATEST_VERSION. Returns (from_version, to_version, backup_path or None)."""
    start = current_version(conn)
    if start == 0:
        # Databases created before migrations existed (or brand new ones) start at the baseline.
        conn.execute("INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                     (BASELINE_VERSION, "baseline schema.sql", _now()))
        conn.commit()
        start = BASELINE_VERSION
    pending = [m for m in MIGRATIONS if m[0] > start]
    backup = None
    if pending and _has_user_data(conn):
        backup = backup_database(conn, db_path, backup_dir, f"before-v{pending[-1][0]}")
    previous = conn.isolation_level
    conn.isolation_level = None  # explicit transactions below
    try:
        for version, name, func in pending:
            conn.execute("BEGIN IMMEDIATE")
            try:
                func(conn)
                conn.execute("INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                             (version, name, _now()))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
    finally:
        conn.isolation_level = previous
    check_audit_triggers(conn)
    return start, current_version(conn), backup
