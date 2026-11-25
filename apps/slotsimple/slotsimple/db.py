"""SQLite access helpers.

Connections run in autocommit mode (isolation_level=None) so that every write
goes through the explicit ``transaction()`` context manager. Booking writes use
``BEGIN IMMEDIATE`` which takes SQLite's write lock up front: availability is
re-checked inside that lock, so two concurrent requests for the same slot are
serialised and only the first can succeed.

Multi-business: every business-data table has ``account_id`` and every helper
here takes the account id explicitly.
"""
import logging
import re
import secrets
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

from flask import current_app, g
from werkzeug.security import generate_password_hash

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2

DEFAULT_SETTINGS = {
    "business_name": "My Business",
    "timezone": "UTC",
    "contact_email": "",
    "contact_phone": "",
    "address": "",
    "about": "",
    "default_duration": "30",
    "slot_interval": "15",
    "buffer_minutes": "0",
    "min_notice_minutes": "120",
    "max_advance_days": "60",
    "auto_confirm": "0",
    "customer_cancel_hours": "24",
    "currency_symbol": "$",
}

# Tables rebuilt by the v1 -> v2 migration, in dependency order.
ACCOUNT_TABLES = ("settings", "opening_hours", "availability_rules", "blocked_periods",
                  "services", "bookings", "booking_events")


def connect(path, timeout=15.0):
    conn = sqlite3.connect(path, timeout=timeout, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"], current_app.config.get("DB_TIMEOUT", 15.0))
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@contextmanager
def transaction(db, immediate=True):
    db.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield db
    except BaseException:
        db.execute("ROLLBACK")
        raise
    else:
        db.execute("COMMIT")


def _statements():
    text = resources.files("slotsimple").joinpath("schema.sql").read_text(encoding="utf-8")
    chunks = [s.strip() for s in text.split("-- ;")]
    # Keep chunks that contain SQL, not just comments.
    return [c for c in chunks if any(line.strip() and not line.strip().startswith("--") for line in c.splitlines())]


def _table_ddl(name):
    for stmt in _statements():
        if f"CREATE TABLE IF NOT EXISTS {name} (" in stmt:
            return stmt
    raise KeyError(name)


def _tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def init_db(db, db_path=None):
    """Create or upgrade the schema. Safe to run on every start; never deletes data."""
    db.execute("PRAGMA journal_mode = WAL")
    tables = _tables(db)
    if "bookings" in tables and "accounts" not in tables:
        backup = backup_database(db, db_path)
        migrate_v1_to_v2(db)
        log.warning("Upgraded the database for multiple businesses. Backup of the old file: %s", backup)
    for stmt in _statements():
        db.execute(stmt)
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def backup_database(db, db_path):
    """Copy the live database (consistently, via SQLite's backup API) next to it
    before any destructive change. Returns the backup path, or None for in-memory DBs."""
    if not db_path or db_path == ":memory:":
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = Path(f"{db_path}.before-accounts-{stamp}.bak")
    out = sqlite3.connect(dest)
    try:
        db.backup(out)
    finally:
        out.close()
    return str(dest)


def migrate_v1_to_v2(db):
    """Move a single-business (v1) database into account #1.

    Every table is rebuilt with an account_id column (SQLite's documented
    create-copy-drop-rename procedure) inside ONE transaction, with foreign
    keys checked before commit: it either fully succeeds or changes nothing."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute("PRAGMA foreign_keys = OFF")  # must be set outside the transaction
    try:
        with transaction(db):
            tables = _tables(db)
            old_settings = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM settings")} \
                if "settings" in tables else {}
            admin = db.execute("SELECT * FROM admin_users ORDER BY id LIMIT 1").fetchone() \
                if "admin_users" in tables else None

            contact = (old_settings.get("contact_email") or "").strip().lower()
            if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", contact):
                email = contact
            else:
                username = re.sub(r"[^a-z0-9._-]", "", (admin["username"] if admin else "owner").lower()) or "owner"
                email = f"{username}@slotsimple.local"
            password_hash = admin["password_hash"] if admin else generate_password_hash(secrets.token_urlsafe(24))
            slug = slugify(old_settings.get("business_name") or "my-business")

            db.execute(_table_ddl("accounts"))
            db.execute("INSERT INTO accounts(id, slug, email, password_hash, created_at) VALUES (1, ?, ?, ?, ?)",
                       (slug, email, password_hash, now))

            for name in ACCOUNT_TABLES:
                if name not in tables:
                    continue
                new_name = f"new_{name}"
                db.execute(_table_ddl(name).replace(f"CREATE TABLE IF NOT EXISTS {name} (",
                                                    f"CREATE TABLE {new_name} ("))
                new_cols = [r["name"] for r in db.execute(f"PRAGMA table_info({new_name})")]
                old_cols = {r["name"] for r in db.execute(f"PRAGMA table_info({name})")}
                common = [c for c in new_cols if c in old_cols and c != "account_id"]
                cols = ", ".join(common)
                # Table/column names come from our own schema, never from input.
                db.execute(f"INSERT INTO {new_name} (account_id, {cols}) SELECT 1, {cols} FROM {name}")
                db.execute(f"DROP TABLE {name}")
                db.execute(f"ALTER TABLE {new_name} RENAME TO {name}")
            if "admin_users" in tables:
                db.execute("DROP TABLE admin_users")
            problems = db.execute("PRAGMA foreign_key_check").fetchall()
            if problems:
                raise RuntimeError(f"Migration aborted: {len(problems)} broken references; nothing was changed.")
    finally:
        db.execute("PRAGMA foreign_keys = ON")


def slugify(text):
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:40].strip("-")
    if not re.search(r"[a-z]", slug):
        slug = f"business-{slug}".strip("-")
    return slug


def unique_slug(db, base, exclude_id=None):
    base = slugify(base)
    candidate, n = base, 2
    while db.execute("SELECT 1 FROM accounts WHERE slug = ? AND id != ?",
                     (candidate, exclude_id or -1)).fetchone():
        candidate = f"{base[:36]}-{n}"
        n += 1
    return candidate


def seed_account_defaults(db, account_id, business_name):
    """Default settings and opening hours for a new account (inside a transaction)."""
    values = dict(DEFAULT_SETTINGS, business_name=business_name)
    for key, value in values.items():
        db.execute("INSERT OR IGNORE INTO settings(account_id, key, value) VALUES (?, ?, ?)",
                   (account_id, key, value))
    for weekday in range(7):
        db.execute(
            "INSERT OR IGNORE INTO opening_hours(account_id, weekday, is_open, open_time, close_time) "
            "VALUES (?, ?, ?, '09:00', '17:00')",
            (account_id, weekday, 1 if weekday < 5 else 0),
        )


def get_settings(db, account_id):
    settings = dict(DEFAULT_SETTINGS)
    settings.update({r["key"]: r["value"] for r in db.execute(
        "SELECT key, value FROM settings WHERE account_id = ?", (account_id,))})
    return settings


def set_settings(db, account_id, values):
    for key, value in values.items():
        if key not in DEFAULT_SETTINGS:
            raise KeyError(key)
        db.execute(
            "INSERT INTO settings(account_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(account_id, key) DO UPDATE SET value = excluded.value",
            (account_id, key, str(value)),
        )
