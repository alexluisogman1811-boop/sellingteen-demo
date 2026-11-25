"""SQLite connection handling. All queries elsewhere use ? placeholders."""
import os
import sqlite3

from flask import current_app, g

from . import migrations


def get_db():
    if "db" not in g:
        # Wait for concurrent writers instead of failing with "database is locked".
        conn = sqlite3.connect(current_app.config["DATABASE"], timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


def close_db(exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    conn = get_db()
    # WAL lets workers read reports while someone else is saving.
    conn.execute("PRAGMA journal_mode = WAL")
    if migrations.needs_accounts_migration(conn):
        backup_path, account_id = migrations.migrate_to_accounts(
            conn, current_app.config["DATABASE"], current_app.config["COMPANY_NAME"])
        current_app.logger.warning("Upgraded the database to separate business accounts (existing data is now "
                                   "account %s). Backup saved as %s", account_id, os.path.basename(backup_path))
    with current_app.open_resource("schema.sql") as fh:
        conn.executescript(fh.read().decode("utf-8"))
    conn.commit()


def init_app(app):
    app.teardown_appcontext(close_db)
