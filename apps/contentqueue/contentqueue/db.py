"""SQLite connection handling. All queries in the app use ? placeholders."""
import os
import sqlite3
from datetime import datetime

import click
from flask import current_app, g

from . import timeutil

# Tables that hold a business's content. Each row belongs to exactly one account.
SCOPED_TABLES = ("posts", "campaigns", "categories", "templates", "ideas")

# Campaign/category names used to be unique across the whole database; they are now
# unique per account. SQLite can't change a column constraint in place, so these two
# tables are rebuilt (same ids, same data) during the upgrade.
_REBUILD = {
    "campaigns": """
        CREATE TABLE campaigns_new (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id  INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
            name        TEXT NOT NULL COLLATE NOCASE,
            description TEXT NOT NULL DEFAULT '',
            color       TEXT NOT NULL DEFAULT 'blue',
            start_date  TEXT,
            end_date    TEXT,
            is_demo     INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL,
            UNIQUE (account_id, name)
        );
        INSERT INTO campaigns_new (id, name, description, color, start_date, end_date, is_demo, created_at)
            SELECT id, name, description, color, start_date, end_date, is_demo, created_at FROM campaigns;
        DROP TABLE campaigns;
        ALTER TABLE campaigns_new RENAME TO campaigns;
    """,
    "categories": """
        CREATE TABLE categories_new (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id  INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
            name        TEXT NOT NULL COLLATE NOCASE,
            description TEXT NOT NULL DEFAULT '',
            is_demo     INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL,
            UNIQUE (account_id, name)
        );
        INSERT INTO categories_new (id, name, description, is_demo, created_at)
            SELECT id, name, description, is_demo, created_at FROM categories;
        DROP TABLE categories;
        ALTER TABLE categories_new RENAME TO categories;
    """,
}


def get_db():
    if "db" not in g:
        # Wait up to 10s for another request's write instead of failing with "database is locked".
        conn = sqlite3.connect(current_app.config["DATABASE"], timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    conn = get_db()
    # WAL lets reads continue while another request writes (stored in the database file).
    conn.execute("PRAGMA journal_mode = WAL")
    with current_app.open_resource("schema.sql") as fh:
        conn.executescript(fh.read().decode("utf-8"))
    migrate(conn)
    conn.commit()


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}  # table is a code constant


def backup_database(conn, label, folder=None):
    """Copy the whole database before a risky change. Returns the file path.

    By default the copy goes NEXT TO the database file, named
    <database name>.<label>-<YYYYmmdd-HHMMSS>.sqlite3, so it is easy to find and restore."""
    database = os.path.abspath(current_app.config["DATABASE"])
    folder = folder or os.path.dirname(database)
    os.makedirs(folder, exist_ok=True)
    base = os.path.splitext(os.path.basename(database))[0]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(folder, f"{base}.{label}-{stamp}.sqlite3")
    n = 1
    while os.path.exists(path):
        n += 1
        path = os.path.join(folder, f"{base}.{label}-{stamp}-{n}.sqlite3")
    conn.commit()
    dest = sqlite3.connect(path)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return path


def migrate(conn):
    """Bring databases created by earlier versions up to date. Data is always kept."""
    if "version" not in _columns(conn, "posts"):
        conn.execute("ALTER TABLE posts ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        conn.commit()

    missing = [t for t in SCOPED_TABLES if "account_id" not in _columns(conn, t)]
    if missing:
        # Single-business database from before accounts existed. FIRST copy the database file
        # (next to it, timestamped), then add account_id. Existing rows belong to the first
        # (owner) account: right away if one exists, otherwise the first account that signs
        # up claims them (see auth.signup and the `assign-existing-data EMAIL` command).
        path = backup_database(conn, "before-accounts")
        current_app.logger.warning("Upgrading database to accounts; backup saved to %s", path)
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")  # table rebuilds must not cascade
        try:
            script = ["BEGIN;"]
            for table in missing:
                if table in _REBUILD:
                    script.append(_REBUILD[table])
                else:
                    script.append(f"ALTER TABLE {table} ADD COLUMN account_id INTEGER "
                                  "REFERENCES accounts(id) ON DELETE CASCADE;")
            script.append("COMMIT;")
            conn.executescript("\n".join(script))
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
        problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise RuntimeError(f"Database upgrade left {len(problems)} broken references; restore {path}.")

    # Old single-owner rows still without an account go to the first (owner) account, if there is one.
    owner = conn.execute("SELECT MIN(id) FROM accounts").fetchone()[0]
    if owner is not None and any(
            conn.execute(f"SELECT 1 FROM {t} WHERE account_id IS NULL LIMIT 1").fetchone() for t in SCOPED_TABLES):
        assign_unowned_data(conn, owner)
        conn.commit()

    if "timezone" not in _columns(conn, "accounts"):
        conn.execute("ALTER TABLE accounts ADD COLUMN timezone TEXT NOT NULL DEFAULT ''")
    for table in SCOPED_TABLES:
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_account ON {table} (account_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_account_planned ON posts (account_id, planned_date)")
    conn.commit()


def assign_unowned_data(conn, account_id):
    """Give rows from the single-business era (account_id NULL) to an account."""
    counts = {}
    for table in SCOPED_TABLES:
        counts[table] = conn.execute(
            f"UPDATE {table} SET account_id = ? WHERE account_id IS NULL", (account_id,)
        ).rowcount
    return counts


def now_iso():
    return timeutil.now().strftime("%Y-%m-%d %H:%M:%S")


def _account_by_email(email):
    row = get_db().execute("SELECT id FROM accounts WHERE email = ?", (email.strip().lower(),)).fetchone()
    if row is None:
        raise click.ClickException(f"No account with the email {email}.")
    return row["id"]


@click.command("init-db")
def init_db_command():
    """Create tables if they do not exist (and upgrade older databases)."""
    init_db()
    click.echo("Database ready.")


@click.command("seed-demo")
@click.argument("email")
def seed_demo_command(email):
    """Load demo campaigns, posts, templates and ideas into one account."""
    from .demo import load_demo

    if load_demo(get_db(), _account_by_email(email)):
        click.echo("Demo data loaded.")
    else:
        click.echo("Demo data is already present in that account; nothing to do.")


@click.command("assign-existing-data")
@click.argument("email")
def assign_existing_data_command(email):
    """Give content created before accounts existed to the account with this email."""
    conn = get_db()
    counts = assign_unowned_data(conn, _account_by_email(email))
    conn.commit()
    click.echo(", ".join(f"{t}: {n}" for t, n in counts.items()))


def init_app(app):
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)
    app.cli.add_command(seed_demo_command)
    app.cli.add_command(assign_existing_data_command)
    with app.app_context():
        init_db()
