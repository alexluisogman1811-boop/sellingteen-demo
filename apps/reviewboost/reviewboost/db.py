import sqlite3
from pathlib import Path

from flask import current_app, g


def get_db():
    if "db" not in g:
        # Wait up to 10s for another request's write instead of failing with "database is locked".
        g.db = sqlite3.connect(current_app.config["DATABASE"], timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(app):
    path = Path(app.config["DATABASE"])
    path.parent.mkdir(parents=True, exist_ok=True)
    schema = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(path)
    try:
        # WAL lets reads continue while a write is in progress (persists in the database file).
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(schema)
        # Lightweight migrations for databases created before these columns existed.
        for table, column in (("customers", "message_stale"), ("businesses", "is_demo")):
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
        # Subscription: the date (YYYY-MM-DD) a paid account is switched on until. Empty = not paid yet.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(businesses)")}
        if "paid_until" not in columns:
            conn.execute("ALTER TABLE businesses ADD COLUMN paid_until TEXT")
        conn.commit()
    finally:
        conn.close()
    app.teardown_appcontext(close_db)
