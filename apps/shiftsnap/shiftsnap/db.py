import logging
import sqlite3
from datetime import datetime

import click
from flask.cli import with_appcontext
from flask import current_app, g

log = logging.getLogger(__name__)

# Tables that hold a business's data and get an account_id when an older database is upgraded.
ACCOUNT_TABLES = ("employees", "availability", "shifts", "users")
MIGRATED_ACCOUNT_NAME = "My business"


def get_db() -> sqlite3.Connection:
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


def _tables(db) -> set[str]:
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _columns(db, table: str) -> set[str]:
    # `table` is always one of this module's constants, never user input.
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}


def needs_account_migration(db) -> bool:
    """True for a database from the single-business version (tables without account_id)."""
    return "employees" in _tables(db) and "account_id" not in _columns(db, "employees")


def backup_database(path: str) -> str:
    """Copy the database (consistently, via SQLite's backup API) next to itself before upgrading."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = f"{path}.before-multibusiness-{stamp}.bak"
    src = sqlite3.connect(path)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return dest


def migrate_to_accounts(db) -> int | None:
    """Upgrade a single-business database in place: all existing data goes to one new account.

    Runs in one transaction (all or nothing). Returns the new account's id, or None when the old
    database had no data (then the first sign-up simply creates the first account).
    """
    tables = _tables(db)
    has_data = any(db.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone()
                   for t in ("employees", "shifts", "users", "settings") if t in tables)
    if db.in_transaction:
        db.commit()
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("CREATE TABLE IF NOT EXISTS accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                   "name TEXT NOT NULL, email TEXT UNIQUE COLLATE NOCASE, "
                   "is_demo INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0, 1)), paid_until TEXT, "
                   "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        account_id = None
        if has_data:
            account_id = db.execute("INSERT INTO accounts (name) VALUES (?)",
                                    (MIGRATED_ACCOUNT_NAME,)).lastrowid
        for table in ACCOUNT_TABLES:
            if table in tables and "account_id" not in _columns(db, table):
                db.execute(f"ALTER TABLE {table} ADD COLUMN account_id INTEGER "
                           "REFERENCES accounts(id) ON DELETE CASCADE")
                if account_id is not None:
                    db.execute(f"UPDATE {table} SET account_id = ?", (account_id,))
        if "login_failures" in tables and "account_id" not in _columns(db, "login_failures"):
            db.execute("ALTER TABLE login_failures ADD COLUMN account_id INTEGER "
                       "REFERENCES accounts(id) ON DELETE CASCADE")
            db.execute("UPDATE login_failures SET account_id = "
                       "(SELECT u.account_id FROM users u WHERE u.username = login_failures.username)")
        if "settings" in tables and "account_id" not in _columns(db, "settings"):
            # The key changes from (key) to (account_id, key), which needs a rebuilt table.
            db.execute("CREATE TABLE settings_new (account_id INTEGER NOT NULL REFERENCES accounts(id) "
                       "ON DELETE CASCADE, key TEXT NOT NULL, value TEXT NOT NULL, "
                       "PRIMARY KEY (account_id, key))")
            if account_id is not None:
                db.execute("INSERT INTO settings_new (account_id, key, value) "
                           "SELECT ?, key, value FROM settings", (account_id,))
            db.execute("DROP TABLE settings")
            db.execute("ALTER TABLE settings_new RENAME TO settings")
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return account_id


def init_db():
    db = get_db()
    if needs_account_migration(db):
        path = current_app.config["DATABASE"]
        backup = backup_database(path)
        log.warning("Upgrading ShiftSnap database to multi-business. Backup saved to %s", backup)
        account_id = migrate_to_accounts(db)
        current_app.config["LAST_MIGRATION"] = {"backup": backup, "account_id": account_id}
    with current_app.open_resource("schema.sql") as f:
        db.executescript(f.read().decode("utf-8"))
    db.commit()


@click.command("init-db")
@with_appcontext
def init_db_command():
    """Create tables if they do not exist (and upgrade an older database)."""
    init_db()
    click.echo("Database ready.")


def init_app(app):
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)
    with app.app_context():
        try:
            init_db()
        except sqlite3.OperationalError as exc:
            raise RuntimeError(
                f"ShiftSnap could not open its database at {app.config['DATABASE']!r} ({exc}). "
                "Check that the folder exists and is writable, or set SHIFTSNAP_DATABASE to "
                "another path.") from exc
