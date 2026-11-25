import sqlite3
from pathlib import Path

from flask import current_app, g

# Columns added after the first release. Existing databases get them on startup;
# fresh databases already have them from schema.sql.
MIGRATIONS = [
    ("users", "sender_name", "TEXT NOT NULL DEFAULT ''"),
    ("users", "timezone", "TEXT NOT NULL DEFAULT 'UTC'"),
    ("users", "session_version", "INTEGER NOT NULL DEFAULT 0"),
    ("followups", "source", "TEXT NOT NULL DEFAULT 'template'"),
    ("drafts", "written_for", "TEXT NOT NULL DEFAULT ''"),
    # Subscription: the date (YYYY-MM-DD) a paid account is switched on until. Empty = not paid yet.
    ("users", "paid_until", "TEXT"),
]


def connect(path):
    conn = sqlite3.connect(path, timeout=10)
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


def init_db(app):
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    schema = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
    conn = connect(app.config["DATABASE"])
    try:
        conn.execute("PRAGMA journal_mode = WAL")  # readers don't block the writer
        # Add missing columns first: schema.sql indexes may reference them.
        for table, column, ddl in MIGRATIONS:
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if cols and column not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        conn.executescript(schema)
        conn.commit()
    finally:
        conn.close()
    app.teardown_appcontext(close_db)
