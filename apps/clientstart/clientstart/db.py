"""SQLite connection handling and schema setup."""
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import click
from flask import current_app, g
from flask.cli import with_appcontext


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def today() -> date:
    """Single place to read "today" so tests can override it via config."""
    override = current_app.config.get("TODAY")
    if override:
        return date.fromisoformat(override) if isinstance(override, str) else override
    return date.today()


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


# Columns added after the first release: (table, column, definition).
MIGRATIONS = [
    ("clients", "portal_expires_at", "TEXT"),
    ("clients", "portal_last_viewed_at", "TEXT"),
    ("comments", "is_internal", "INTEGER NOT NULL DEFAULT 0"),
]


# Every table that holds an account's data. Each has an account_id column.
ACCOUNT_SCOPED = ("onboarding_templates", "template_items", "clients", "tasks", "uploads",
                  "comments", "activity", "reminder_templates")

# Login email given to the account that receives data from a single-owner (pre-accounts) install.
LEGACY_EMAIL_DEFAULT = "owner@clientstart.local"


def _columns(db, table):
    return {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}


def backup_database_file(reason: str) -> str:
    """Consistent copy of the database next to it (in backups/), made before risky migrations."""
    src = Path(current_app.config["DATABASE"])
    folder = src.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"clientstart-before-{reason}-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.sqlite3"
    dst = sqlite3.connect(target)
    try:
        get_db().backup(dst)
    finally:
        dst.close()
    return str(target)


def _migrate_to_accounts(db):
    """Single-owner databases -> accounts. Existing data goes to the first account.

    1. If any data table lacks account_id and the database holds data, back it up first.
    2. Add account_id columns.
    3. Rows without an account are given to the first account. If no account exists yet, one is
       created from the old single-owner password (login email: CLIENTSTART_LEGACY_EMAIL, default
       owner@clientstart.local), so old data is never claimable by a stranger who signs up first.
    """
    missing = [t for t in ACCOUNT_SCOPED if "account_id" not in _columns(db, t)]
    if missing:
        for table in missing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE")
    orphaned = any(db.execute(f"SELECT 1 FROM {t} WHERE account_id IS NULL LIMIT 1").fetchone() for t in ACCOUNT_SCOPED)
    if orphaned:
        first = db.execute("SELECT id FROM accounts ORDER BY id LIMIT 1").fetchone()
        if first is None:
            from werkzeug.security import generate_password_hash
            import secrets
            old = None
            if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'app_settings'").fetchone():
                old = db.execute("SELECT value FROM app_settings WHERE key = 'owner_password_hash'").fetchone()
            # No old password: an unusable random one; set a real one with `flask set-password EMAIL`.
            pw_hash = old["value"] if old else generate_password_hash(secrets.token_urlsafe(32))
            email = (current_app.config.get("LEGACY_EMAIL") or LEGACY_EMAIL_DEFAULT).strip().lower()
            name = current_app.config.get("LEGACY_BUSINESS_NAME") or "My business"
            account_id = db.execute(
                "INSERT INTO accounts (name, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (name, email, pw_hash, now_iso())).lastrowid
        else:
            account_id = first["id"]
        for table in ACCOUNT_SCOPED:
            db.execute(f"UPDATE {table} SET account_id = ? WHERE account_id IS NULL", (account_id,))
    for table in ACCOUNT_SCOPED:
        db.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_account ON {table}(account_id)")


def migrate():
    """Add missing columns to databases created by older versions. Idempotent."""
    db = get_db()
    db.commit()  # no open transaction, so the backup below sees a consistent, finished database
    needs_accounts = any("account_id" not in _columns(db, t) for t in ACCOUNT_SCOPED)
    if needs_accounts and any(db.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone() for t in ACCOUNT_SCOPED):
        # Safety first: copy the old single-owner database before changing anything in it.
        path = backup_database_file("accounts-migration")
        current_app.logger.warning("Backed up the database to %s before adding accounts.", path)
    for table, column, definition in MIGRATIONS:
        existing = _columns(db, table)
        if column not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    # Links issued before expiry existed get a fresh expiry window rather than never expiring.
    days = int(current_app.config["PORTAL_LINK_DAYS"])
    db.execute("UPDATE clients SET portal_expires_at = ? WHERE portal_expires_at IS NULL",
               ((today() + timedelta(days=days)).isoformat(),))
    _migrate_to_accounts(db)
    db.commit()


def init_db():
    schema = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
    get_db().executescript(schema)
    get_db().commit()
    migrate()


@click.command("init-db")
@with_appcontext
def init_db_command():
    """Create tables (safe to run repeatedly)."""
    init_db()
    click.echo("Database initialised.")


@click.command("seed-demo")
@click.argument("email")
@with_appcontext
def seed_demo_command(email):
    """Replace ONE account's data with demo data (other accounts are untouched)."""
    from .services import reset_account_data

    row = get_db().execute("SELECT id FROM accounts WHERE email = ?", (email.strip().lower(),)).fetchone()
    if row is None:
        raise click.ClickException(f"No account with the email {email}.")
    reset_account_data(row["id"], load_demo=True)
    click.echo(f"Demo data loaded into {email}.")


def backup_to(dest_root: str) -> str:
    """Write a consistent snapshot (database + uploads) to a new timestamped folder.

    Uses SQLite's online backup API, so it is safe while the app is serving requests.
    Returns the folder path.
    """
    import shutil

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = Path(dest_root) / f"clientstart-backup-{stamp}"
    target.mkdir(parents=True, exist_ok=False)
    dst = sqlite3.connect(target / "clientstart.sqlite3")
    try:
        get_db().backup(dst)
    finally:
        dst.close()
    uploads = Path(current_app.config["UPLOAD_FOLDER"])
    if uploads.is_dir():
        shutil.copytree(uploads, target / "uploads")
    return str(target)


@click.command("backup")
@click.option("--dest", required=True, type=click.Path(file_okay=False), help="Folder to write the backup into.")
@with_appcontext
def backup_command(dest):
    """Back up the database and uploaded files (safe while running)."""
    click.echo(f"Backup written to {backup_to(dest)}")


def init_app(app):
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)
    app.cli.add_command(seed_demo_command)
    app.cli.add_command(backup_command)
