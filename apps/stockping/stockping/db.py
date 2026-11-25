"""SQLite connection handling, schema, migrations and transactions.

Every shop is one row in ``accounts``. Every other table carries ``account_id``, and the
composite foreign keys below make the database itself reject a product that points at
another account's supplier, or a movement that points at another account's product.
"""
import logging
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from flask import current_app, g
from werkzeug.security import generate_password_hash

log = logging.getLogger(__name__)

# Owner of data that existed before accounts were added (see migrate_to_accounts).
LEGACY_EMAIL = "existing-shop@stockping.invalid"
LEGACY_SHOP_NAME = "My shop"

# Data tables, written with a {name} placeholder so the migration can build a copy
# (e.g. "products_new") with exactly the same definition before swapping it in.
TABLES = {
    "suppliers": """
CREATE TABLE IF NOT EXISTS {name} (
    id            INTEGER PRIMARY KEY,
    account_id    INTEGER NOT NULL REFERENCES accounts(id),
    name          TEXT NOT NULL COLLATE NOCASE,
    contact_name  TEXT NOT NULL DEFAULT '',
    email         TEXT NOT NULL DEFAULT '',
    phone         TEXT NOT NULL DEFAULT '',
    notes         TEXT NOT NULL DEFAULT '',
    is_demo       INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0, 1)),
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (account_id, name),
    UNIQUE (account_id, id)
)""",
    "products": """
CREATE TABLE IF NOT EXISTS {name} (
    id                 INTEGER PRIMARY KEY,
    account_id         INTEGER NOT NULL REFERENCES accounts(id),
    sku                TEXT NOT NULL COLLATE NOCASE,
    name               TEXT NOT NULL,
    category           TEXT NOT NULL DEFAULT '',
    supplier_id        INTEGER,
    cost_cents         INTEGER NOT NULL CHECK (cost_cents >= 0),
    price_cents        INTEGER NOT NULL CHECK (price_cents >= 0),
    quantity           INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    reorder_threshold  INTEGER NOT NULL DEFAULT 0 CHECK (reorder_threshold >= 0),
    description        TEXT NOT NULL DEFAULT '',
    is_active          INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    is_demo            INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0, 1)),
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (account_id, sku),
    UNIQUE (account_id, id),
    FOREIGN KEY (account_id, supplier_id) REFERENCES suppliers(account_id, id)
)""",
    "stock_movements": """
CREATE TABLE IF NOT EXISTS {name} (
    id               INTEGER PRIMARY KEY,
    account_id       INTEGER NOT NULL REFERENCES accounts(id),
    product_id       INTEGER NOT NULL,
    movement_type    TEXT NOT NULL CHECK (movement_type IN ('initial', 'in', 'out', 'adjustment')),
    quantity_change  INTEGER NOT NULL,
    quantity_before  INTEGER NOT NULL CHECK (quantity_before >= 0),
    quantity_after   INTEGER NOT NULL CHECK (quantity_after >= 0),
    movement_date    TEXT NOT NULL,
    reason           TEXT NOT NULL DEFAULT '',
    notes            TEXT NOT NULL DEFAULT '',
    source           TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'import', 'demo')),
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    request_id       TEXT,   -- one-time form token; blocks double-submitted movements
    CHECK (quantity_after = quantity_before + quantity_change),
    FOREIGN KEY (account_id, product_id) REFERENCES products(account_id, id)
)""",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id             INTEGER PRIMARY KEY,
    shop_name      TEXT NOT NULL,
    email          TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash  TEXT NOT NULL,
    is_demo        INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0, 1)),
    paid_until     TEXT,   -- YYYY-MM-DD the subscription is switched on until (empty = not paid)
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
""" + ";\n".join(t.format(name=n) for n, t in TABLES.items()) + """;

CREATE INDEX IF NOT EXISTS idx_products_category ON products(account_id, category);
CREATE INDEX IF NOT EXISTS idx_products_supplier ON products(supplier_id);
CREATE INDEX IF NOT EXISTS idx_movements_product ON stock_movements(product_id);
CREATE INDEX IF NOT EXISTS idx_movements_account ON stock_movements(account_id, movement_date);

-- The movement history is an append-only audit log. Rows can only be removed
-- together with demo products (see services.remove_demo_data).
CREATE TRIGGER IF NOT EXISTS stock_movements_no_update
BEFORE UPDATE ON stock_movements
BEGIN
    SELECT RAISE(ABORT, 'stock movements are append-only');
END;

CREATE TRIGGER IF NOT EXISTS stock_movements_no_delete
BEFORE DELETE ON stock_movements
WHEN (SELECT is_demo FROM products WHERE id = OLD.product_id) IS NOT 1
BEGIN
    SELECT RAISE(ABORT, 'stock movements of real products cannot be deleted');
END;

-- Real products are archived, never hard-deleted, so their history survives.
CREATE TRIGGER IF NOT EXISTS products_no_delete
BEFORE DELETE ON products
WHEN OLD.is_demo = 0
BEGIN
    SELECT RAISE(ABORT, 'real products cannot be deleted; archive them instead');
END;

CREATE TABLE IF NOT EXISTS import_previews (
    token       TEXT PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts(id),
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class AccountDB:
    """A database connection bound to one shop's account.

    Every function in services.py takes one of these and filters each query by
    ``account_id``. Everything else (execute, in_transaction, ...) is the connection's own.
    """

    def __init__(self, conn, account_id):
        if not isinstance(account_id, int) or isinstance(account_id, bool) or account_id < 1:
            raise ValueError("AccountDB needs a positive integer account id")
        self.conn = conn
        self.account_id = account_id

    def __getattr__(self, name):
        return getattr(self.conn, name)


def connect(path):
    # isolation_level=None: autocommit unless we open a transaction explicitly.
    conn = sqlite3.connect(path, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # SQLite's LIKE only ignores case for ASCII; fold() gives Unicode-aware search
    # ("CAFÉ" finds "Café").
    conn.create_function("fold", 1, lambda s: s.casefold() if isinstance(s, str) else s,
                         deterministic=True)
    return conn


def _columns(conn, table):
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def needs_account_migration(conn):
    """True for a database from the single-shop version (data tables without account_id)."""
    columns = _columns(conn, "products")
    return bool(columns) and "account_id" not in columns


def backup_database(conn, label):
    """Copy the whole database (with SQLite's online backup) to backups/ next to it."""
    path = Path(conn.execute("PRAGMA database_list").fetchone()["file"])
    folder = path.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{path.stem}-before-{label}-{datetime.now():%Y%m%d-%H%M%S}{path.suffix or '.sqlite3'}"
    out = sqlite3.connect(dest)
    try:
        conn.backup(out)
    finally:
        out.close()
    return dest


def migrate_to_accounts(conn):
    """Move a single-shop database to the accounts schema, keeping every row.

    1. Backs up the database file first (backups/<name>-before-accounts-<time>.sqlite3).
    2. In ONE transaction: creates the accounts table; if there is any existing data,
       creates account #1 ("My shop", LEGACY_EMAIL, an unusable random password) and gives
       it every supplier, product and movement; rebuilds the tables with account_id.
    3. Before committing, checks row counts, total stock and foreign keys are unchanged;
       any difference rolls everything back.
    The owner then sets a real email and password with:
        flask --app stockping set-login existing-shop@stockping.invalid --new-email you@shop.com
    """
    backup = backup_database(conn, "accounts")
    data_tables = ["suppliers", "products", "stock_movements"]
    present = [t for t in data_tables if _columns(conn, t)]

    def totals():
        result = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in present}
        if "products" in present:
            result["quantity"] = conn.execute("SELECT COALESCE(SUM(quantity), 0) FROM products").fetchone()[0]
        if "stock_movements" in present:
            result["ledger"] = conn.execute(
                "SELECT COALESCE(SUM(quantity_change), 0) FROM stock_movements").fetchone()[0]
        return result

    before = totals()
    conn.execute("PRAGMA foreign_keys = OFF")   # required while tables are rebuilt
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(SCHEMA.split(";", 1)[0])   # CREATE TABLE accounts
        legacy_id = None
        if any(before.get(t) for t in present):
            legacy_id = conn.execute(
                "INSERT INTO accounts (shop_name, email, password_hash) VALUES (?, ?, ?)",
                (LEGACY_SHOP_NAME, LEGACY_EMAIL,
                 generate_password_hash(secrets.token_urlsafe(32)))).lastrowid
        for table in data_tables:
            conn.execute(TABLES[table].format(name=f"{table}_new"))
            if table in present and legacy_id is not None:
                new_cols = set(_columns(conn, f"{table}_new"))
                shared = [c for c in _columns(conn, table) if c in new_cols and c != "account_id"]
                cols = ", ".join(shared)
                conn.execute(f"INSERT INTO {table}_new (account_id, {cols}) "
                             f"SELECT ?, {cols} FROM {table} ORDER BY id", (legacy_id,))
        for table in reversed(data_tables):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        for table in data_tables:
            conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
        # Unconfirmed CSV import previews are temporary; they are not carried over.
        conn.execute("DROP TABLE IF EXISTS import_previews")
        after = totals()
        problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if after != before or problems:
            raise RuntimeError(f"account migration check failed: before={before} after={after} "
                               f"foreign_key_problems={len(problems)}")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        conn.execute("PRAGMA foreign_keys = ON")
        raise
    conn.execute("PRAGMA foreign_keys = ON")
    log.warning("StockPing: moved existing data to account %s (%s). Backup: %s. Set a login with: "
                "flask --app stockping set-login %s --new-email you@shop.com",
                legacy_id, LEGACY_EMAIL, backup, LEGACY_EMAIL)
    return {"backup": str(backup), "legacy_account_id": legacy_id, "totals": before}


def init_db(conn):
    conn.execute("PRAGMA journal_mode = WAL")
    if needs_account_migration(conn):
        migrate_to_accounts(conn)
    conn.executescript(SCHEMA)
    migrate(conn)


def migrate(conn):
    """Bring databases created by earlier versions up to date, keeping all rows."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(stock_movements)")}
    if "request_id" not in columns:
        conn.execute("ALTER TABLE stock_movements ADD COLUMN request_id TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_movements_request "
                 "ON stock_movements(request_id) WHERE request_id IS NOT NULL")


PROTECTING_TRIGGERS = ("stock_movements_no_update", "stock_movements_no_delete", "products_no_delete")


def delete_account(conn, account_id):
    """Permanently delete one account and every row it owns (the "Delete my account" button).

    The audit-log triggers normally block deleting real products and movements. They are
    dropped and re-created inside the same write transaction, so for every other request
    (and if anything fails, after the rollback) they are always in place.
    Returns the number of rows deleted per table.
    """
    trigger_sql = {row["name"]: row["sql"] for row in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name IN (?, ?, ?)",
        PROTECTING_TRIGGERS)}
    counts = {}
    with transaction(conn):
        for name in trigger_sql:
            conn.execute(f"DROP TRIGGER {name}")
        for table in ("import_previews", "stock_movements", "products", "suppliers"):
            counts[table] = conn.execute(f"DELETE FROM {table} WHERE account_id = ?",
                                         (account_id,)).rowcount
        counts["accounts"] = conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,)).rowcount
        for sql in trigger_sql.values():
            conn.execute(sql)
    return counts


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def account_db():
    """The logged-in shop's AccountDB. Views call this; login_required guarantees g.account."""
    return AccountDB(get_db(), g.account["id"])


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


@contextmanager
def transaction(conn):
    """Run a block in one write transaction (BEGIN IMMEDIATE takes the write
    lock up front, so read-then-update sequences cannot interleave). Nested use
    joins the outer transaction."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def init_app(app):
    app.teardown_appcontext(close_db)
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(app.config["DATABASE"])
    try:
        init_db(conn)
    finally:
        conn.close()
