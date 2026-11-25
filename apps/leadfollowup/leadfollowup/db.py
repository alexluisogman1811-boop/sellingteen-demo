"""SQLite access helpers. One connection per request, stored on flask.g."""
import logging
import os
import sqlite3
from datetime import datetime, timezone

import click
from flask import current_app, g
from flask.cli import with_appcontext

log = logging.getLogger(__name__)

# Columns added after the first release: (table, column, definition).
# Migrations only ever ADD columns, so existing data is never dropped.
MIGRATIONS = [
    ("lead_notes", "method", "TEXT NOT NULL DEFAULT 'Note'"),
    ("lead_notes", "contact_date", "TEXT"),
    ("users", "is_demo", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "paid_until", "TEXT"),  # ISO date a paid subscription runs until (billing.py); NULL = never paid
]


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


def backup_db(conn: sqlite3.Connection, path: str) -> str:
    """Write a consistent copy of the database next to it, in backups/."""
    folder = os.path.join(os.path.dirname(os.path.abspath(path)), "backups")
    os.makedirs(folder, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(folder, f"{os.path.splitext(os.path.basename(path))[0]}-{stamp}.db")
    with sqlite3.connect(dest) as target:
        conn.backup(target)
    return dest


def migrate(conn: sqlite3.Connection, path: str):
    pending = []
    for table, column, definition in MIGRATIONS:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            pending.append((table, column, definition))
    if not pending:
        return
    has_data = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0
    if has_data:
        dest = backup_db(conn, path)
        log.warning("Database backed up to %s before migration", dest)
    for table, column, definition in pending:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    conn.commit()


def init_db():
    path = current_app.config["DATABASE"]
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    conn = get_db()
    with current_app.open_resource("schema.sql") as f:
        conn.executescript(f.read().decode("utf-8"))
    migrate(conn, path)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_lead_contact ON lead_notes (lead_id, contact_date)")
    conn.commit()


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@click.command("init-db")
@with_appcontext
def init_db_command():
    """Create the database tables and apply migrations (safe to run repeatedly)."""
    init_db()
    click.echo("Database ready.")


@click.command("backup-db")
@with_appcontext
def backup_db_command():
    """Save a copy of the database to instance/backups/."""
    click.echo(f"Backup written to {backup_db(get_db(), current_app.config['DATABASE'])}")


def init_app(app):
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)
    app.cli.add_command(backup_db_command)
    with app.app_context():
        init_db()
