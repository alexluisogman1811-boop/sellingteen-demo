"""SQLite connection handling, schema and migrations."""
import os
import sqlite3
from datetime import datetime

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    name                 TEXT NOT NULL UNIQUE COLLATE NOCASE,
    sku                  TEXT UNIQUE COLLATE NOCASE,
    default_license_type TEXT NOT NULL DEFAULT 'fixed'
                         CHECK (default_license_type IN ('lifetime', 'fixed', 'manual')),
    default_term_days    INTEGER CHECK (default_term_days IS NULL OR default_term_days > 0),
    notes                TEXT NOT NULL DEFAULT '',
    created_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    email      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    company    TEXT NOT NULL DEFAULT '',
    notes      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_ref   TEXT NOT NULL UNIQUE COLLATE NOCASE,
    customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
    order_date  TEXT NOT NULL,
    amount      TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS licenses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      INTEGER NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    customer_id     INTEGER NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
    order_id        INTEGER REFERENCES orders(id) ON DELETE RESTRICT,
    key_hash        TEXT NOT NULL UNIQUE,
    key_hint        TEXT NOT NULL,
    license_type    TEXT NOT NULL CHECK (license_type IN ('lifetime', 'fixed', 'manual')),
    issue_date      TEXT NOT NULL,
    expiration_date TEXT,
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'suspended', 'expired', 'revoked', 'replaced')),
    replaces_id     INTEGER REFERENCES licenses(id) ON DELETE SET NULL,
    replaced_by_id  INTEGER REFERENCES licenses(id) ON DELETE SET NULL,
    notes           TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_licenses_expiration ON licenses(expiration_date);
CREATE INDEX IF NOT EXISTS idx_licenses_customer ON licenses(customer_id);
CREATE INDEX IF NOT EXISTS idx_licenses_order ON licenses(order_id);

CREATE TABLE IF NOT EXISTS audit_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    license_id  INTEGER REFERENCES licenses(id) ON DELETE SET NULL,
    entity_type TEXT NOT NULL,
    entity_id   INTEGER,
    action      TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_license ON audit_events(license_id);

CREATE TABLE IF NOT EXISTS reminder_templates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    subject    TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

DEFAULT_TEMPLATES = [
    (
        "Renewal reminder (upcoming)",
        "Your {product_name} license expires on {expiration_date}",
        "Hi {customer_name},\n\n"
        "This is a friendly reminder that your {product_name} license "
        "(order {order_ref}, key ending {license_hint}) expires on {expiration_date} "
        "— {days_left} days from today.\n\n"
        "Reply to this email if you'd like to renew and we'll send you the details.\n\n"
        "Thanks,\n{business_name}",
    ),
    (
        "Expired license follow-up",
        "Your {product_name} license has expired",
        "Hi {customer_name},\n\n"
        "Your {product_name} license (order {order_ref}, key ending {license_hint}) "
        "expired on {expiration_date}.\n\n"
        "If you'd like to keep using it, just reply and we'll arrange a renewal.\n\n"
        "Best regards,\n{business_name}",
    ),
]

# Schema changes after the first release. Each entry upgrades from
# user_version == index to index + 1 and must be safe on databases created
# by any earlier version.
RENEWALS_TABLE = """
CREATE TABLE IF NOT EXISTS renewals (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    license_id          INTEGER REFERENCES licenses(id) ON DELETE SET NULL,
    previous_expiration TEXT,
    new_expiration      TEXT NOT NULL,
    period              TEXT NOT NULL DEFAULT '',
    previous_status     TEXT NOT NULL DEFAULT '',
    note                TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_renewals_license ON renewals(license_id);
"""

RENEWED_DETAIL_RE = r"^Expiration (\S+) → (\d{4}-\d{2}-\d{2})(?:\. (.*))?$"


def _migrate_1(conn):
    """Structured renewal history and revocation fields (backfilled from the audit log)."""
    import re

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(licenses)")}
    if "revoked_at" not in cols:
        conn.execute("ALTER TABLE licenses ADD COLUMN revoked_at TEXT")
    if "revocation_reason" not in cols:
        conn.execute("ALTER TABLE licenses ADD COLUMN revocation_reason TEXT NOT NULL DEFAULT ''")
    conn.executescript(RENEWALS_TABLE)
    pattern = re.compile(RENEWED_DETAIL_RE, re.S)
    for ev in conn.execute(
        "SELECT license_id, detail, created_at FROM audit_events "
        "WHERE action = 'renewed' AND license_id IS NOT NULL ORDER BY id"
    ).fetchall():
        m = pattern.match(ev["detail"])
        if m:
            prev = None if m.group(1) == "none" else m.group(1)
            conn.execute(
                "INSERT INTO renewals (license_id, previous_expiration, new_expiration, period, note, created_at) "
                "VALUES (?, ?, ?, 'backfilled', ?, ?)",
                (ev["license_id"], prev, m.group(2), m.group(3) or "", ev["created_at"]),
            )
    for ev in conn.execute(
        "SELECT license_id, detail, created_at FROM audit_events "
        "WHERE action = 'revoked' AND license_id IS NOT NULL ORDER BY id"
    ).fetchall():
        conn.execute(
            "UPDATE licenses SET revoked_at = ?, revocation_reason = ? WHERE id = ? AND revoked_at IS NULL",
            (ev["created_at"], ev["detail"], ev["license_id"]),
        )


ORDER_ITEMS_TABLE = """
CREATE TABLE IF NOT EXISTS order_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    quantity   INTEGER NOT NULL CHECK (quantity BETWEEN 1 AND 1000),
    created_at TEXT NOT NULL,
    UNIQUE (order_id, product_id)
);
CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);
"""


def _add_column(conn, table, name, ddl):
    if name not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _migrate_2(conn):
    """Order status, owner-recorded payment status, order items (seats) and license delivery tracking.

    Existing orders get payment status 'not_recorded' (never 'paid': nothing was
    verified) and existing licenses get delivery status 'pending' because the
    app has no record of whether they were sent.
    """
    _add_column(conn, "orders", "order_status",
                "TEXT NOT NULL DEFAULT 'open' CHECK (order_status IN ('open', 'completed', 'cancelled'))")
    _add_column(conn, "orders", "payment_status",
                "TEXT NOT NULL DEFAULT 'not_recorded' CHECK (payment_status IN "
                "('not_recorded', 'awaiting', 'marked_paid', 'refunded', 'not_required'))")
    _add_column(conn, "orders", "payment_reference", "TEXT NOT NULL DEFAULT ''")
    _add_column(conn, "orders", "payment_recorded_at", "TEXT")
    _add_column(conn, "licenses", "delivery_status",
                "TEXT NOT NULL DEFAULT 'pending' CHECK (delivery_status IN ('pending', 'delivered'))")
    _add_column(conn, "licenses", "delivered_at", "TEXT")
    _add_column(conn, "licenses", "delivery_method", "TEXT NOT NULL DEFAULT ''")
    _add_column(conn, "licenses", "delivery_reference", "TEXT NOT NULL DEFAULT ''")
    conn.executescript(ORDER_ITEMS_TABLE)
    if conn.execute("SELECT COUNT(*) FROM licenses").fetchone()[0]:
        conn.execute(
            "INSERT INTO audit_events (license_id, entity_type, entity_id, action, detail, created_at) "
            "VALUES (NULL, 'system', NULL, 'upgraded', ?, ?)",
            ("Delivery and payment tracking added. Existing licenses are marked 'delivery pending' and existing "
             "orders 'payment not recorded' because earlier versions did not track them. Review and update them.",
             now_iso()),
        )


# --------------------------------------------------------------------------- multi-account (version 3)

# Final table definitions. Every business table carries account_id. Links
# between records use composite foreign keys (id, account_id), so the database
# itself refuses a license, order or item that points at another account's
# product, customer or order. Uniqueness (names, SKUs, emails, order refs,
# license keys) is per account.
ACCOUNTS_DDL = [
    """CREATE TABLE accounts (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        business_name TEXT NOT NULL,
        email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        auth_version  INTEGER NOT NULL DEFAULT 1,
        is_demo       INTEGER NOT NULL DEFAULT 0,
        paid_until    TEXT,
        created_at    TEXT NOT NULL
    )""",
    """CREATE TABLE products_v3 (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id           INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        name                 TEXT NOT NULL COLLATE NOCASE,
        sku                  TEXT COLLATE NOCASE,
        default_license_type TEXT NOT NULL DEFAULT 'fixed'
                             CHECK (default_license_type IN ('lifetime', 'fixed', 'manual')),
        default_term_days    INTEGER CHECK (default_term_days IS NULL OR default_term_days > 0),
        notes                TEXT NOT NULL DEFAULT '',
        created_at           TEXT NOT NULL,
        UNIQUE (account_id, name), UNIQUE (account_id, sku), UNIQUE (id, account_id)
    )""",
    """CREATE TABLE customers_v3 (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        name       TEXT NOT NULL,
        email      TEXT NOT NULL COLLATE NOCASE,
        company    TEXT NOT NULL DEFAULT '',
        notes      TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        UNIQUE (account_id, email), UNIQUE (id, account_id)
    )""",
    """CREATE TABLE orders_v3 (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id          INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        order_ref           TEXT NOT NULL COLLATE NOCASE,
        customer_id         INTEGER NOT NULL,
        order_date          TEXT NOT NULL,
        amount              TEXT NOT NULL DEFAULT '',
        notes               TEXT NOT NULL DEFAULT '',
        created_at          TEXT NOT NULL,
        order_status        TEXT NOT NULL DEFAULT 'open' CHECK (order_status IN ('open', 'completed', 'cancelled')),
        payment_status      TEXT NOT NULL DEFAULT 'not_recorded' CHECK (payment_status IN
                            ('not_recorded', 'awaiting', 'marked_paid', 'refunded', 'not_required')),
        payment_reference   TEXT NOT NULL DEFAULT '',
        payment_recorded_at TEXT,
        FOREIGN KEY (customer_id, account_id) REFERENCES customers(id, account_id) ON DELETE RESTRICT,
        UNIQUE (account_id, order_ref), UNIQUE (id, account_id)
    )""",
    """CREATE TABLE order_items_v3 (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        order_id   INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        quantity   INTEGER NOT NULL CHECK (quantity BETWEEN 1 AND 1000),
        created_at TEXT NOT NULL,
        FOREIGN KEY (order_id, account_id) REFERENCES orders(id, account_id) ON DELETE CASCADE,
        FOREIGN KEY (product_id, account_id) REFERENCES products(id, account_id) ON DELETE RESTRICT,
        UNIQUE (order_id, product_id)
    )""",
    """CREATE TABLE licenses_v3 (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id         INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        product_id         INTEGER NOT NULL,
        customer_id        INTEGER NOT NULL,
        order_id           INTEGER,
        key_hash           TEXT NOT NULL,
        key_hint           TEXT NOT NULL,
        license_type       TEXT NOT NULL CHECK (license_type IN ('lifetime', 'fixed', 'manual')),
        issue_date         TEXT NOT NULL,
        expiration_date    TEXT,
        status             TEXT NOT NULL DEFAULT 'active'
                           CHECK (status IN ('active', 'suspended', 'expired', 'revoked', 'replaced')),
        replaces_id        INTEGER REFERENCES licenses(id) ON DELETE SET NULL,
        replaced_by_id     INTEGER REFERENCES licenses(id) ON DELETE SET NULL,
        notes              TEXT NOT NULL DEFAULT '',
        created_at         TEXT NOT NULL,
        updated_at         TEXT NOT NULL,
        revoked_at         TEXT,
        revocation_reason  TEXT NOT NULL DEFAULT '',
        delivery_status    TEXT NOT NULL DEFAULT 'pending' CHECK (delivery_status IN ('pending', 'delivered')),
        delivered_at       TEXT,
        delivery_method    TEXT NOT NULL DEFAULT '',
        delivery_reference TEXT NOT NULL DEFAULT '',
        FOREIGN KEY (product_id, account_id) REFERENCES products(id, account_id) ON DELETE RESTRICT,
        FOREIGN KEY (customer_id, account_id) REFERENCES customers(id, account_id) ON DELETE RESTRICT,
        FOREIGN KEY (order_id, account_id) REFERENCES orders(id, account_id) ON DELETE RESTRICT,
        UNIQUE (account_id, key_hash), UNIQUE (id, account_id)
    )""",
    """CREATE TABLE renewals_v3 (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id          INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        license_id          INTEGER REFERENCES licenses(id) ON DELETE SET NULL,
        previous_expiration TEXT,
        new_expiration      TEXT NOT NULL,
        period              TEXT NOT NULL DEFAULT '',
        previous_status     TEXT NOT NULL DEFAULT '',
        note                TEXT NOT NULL DEFAULT '',
        created_at          TEXT NOT NULL
    )""",
    """CREATE TABLE audit_events_v3 (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        license_id  INTEGER REFERENCES licenses(id) ON DELETE SET NULL,
        entity_type TEXT NOT NULL,
        entity_id   INTEGER,
        action      TEXT NOT NULL,
        detail      TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL
    )""",
    """CREATE TABLE reminder_templates_v3 (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        name       TEXT NOT NULL COLLATE NOCASE,
        subject    TEXT NOT NULL,
        body       TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (account_id, name)
    )""",
]
ACCOUNTS_INDEXES = [
    "CREATE INDEX idx_orders_account_customer ON orders(account_id, customer_id)",
    "CREATE INDEX idx_order_items_order ON order_items(order_id)",
    "CREATE INDEX idx_licenses_account_expiration ON licenses(account_id, expiration_date)",
    "CREATE INDEX idx_licenses_account_customer ON licenses(account_id, customer_id)",
    "CREATE INDEX idx_licenses_order ON licenses(order_id)",
    "CREATE INDEX idx_renewals_license ON renewals(license_id)",
    "CREATE INDEX idx_audit_account_license ON audit_events(account_id, license_id)",
    "CREATE INDEX idx_audit_account_entity ON audit_events(account_id, entity_type, entity_id)",
]
# Rebuilt tables, parents first, with the columns copied from the old table.
ACCOUNT_TABLES = {
    "products": "id, name, sku, default_license_type, default_term_days, notes, created_at",
    "customers": "id, name, email, company, notes, created_at",
    "orders": "id, order_ref, customer_id, order_date, amount, notes, created_at, order_status, payment_status, "
              "payment_reference, payment_recorded_at",
    "order_items": "id, order_id, product_id, quantity, created_at",
    "licenses": "id, product_id, customer_id, order_id, key_hash, key_hint, license_type, issue_date, "
                "expiration_date, status, replaces_id, replaced_by_id, notes, created_at, updated_at, revoked_at, "
                "revocation_reason, delivery_status, delivered_at, delivery_method, delivery_reference",
    "renewals": "id, license_id, previous_expiration, new_expiration, period, previous_status, note, created_at",
    "audit_events": "id, license_id, entity_type, entity_id, action, detail, created_at",
    "reminder_templates": "id, name, subject, body, created_at",
}
LEGACY_EMAIL = "owner@licenseledger.local"
ACCOUNTS_VERSION = 3


def _has_single_user_data(conn):
    """True if this database was used by the single-user version (owner set up or any records)."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'accounts'").fetchone():
        return False
    if conn.execute("SELECT 1 FROM settings WHERE key = 'owner_password_hash'").fetchone():
        return True
    return any(conn.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone()
               for t in ("products", "customers", "orders", "licenses"))


def _migrate_3(conn, legacy_email=None):
    """Split the single-user ledger into accounts. All existing records move to
    the first account (the original owner): same login password, business
    name from Settings, and the email legacy_email (config LEGACY_OWNER_EMAIL,
    default LEGACY_EMAIL), which the owner can change in Settings. init_db
    backs the file up first."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'accounts'").fetchone():
        return  # already migrated
    legacy = _has_single_user_data(conn)
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")  # only effective outside a transaction
    try:
        conn.execute("BEGIN")
        for ddl in ACCOUNTS_DDL:
            conn.execute(ddl)
        if legacy:
            setting = lambda k: (conn.execute("SELECT value FROM settings WHERE key = ?", (k,)).fetchone() or [None])[0]
            email = (legacy_email or LEGACY_EMAIL).strip().lower()
            # An owner who never set a password gets an unusable hash ("!" never matches).
            conn.execute(
                "INSERT INTO accounts (id, business_name, email, password_hash, auth_version, created_at) "
                "VALUES (1, ?, ?, ?, ?, ?)",
                (setting("business_name") or "My business", email, setting("owner_password_hash") or "!",
                 int(setting("auth_version") or 1), now_iso()),
            )
            for table, cols in ACCOUNT_TABLES.items():
                conn.execute(f"INSERT INTO {table}_v3 (account_id, {cols}) SELECT 1, {cols} FROM {table}")
        for table in reversed(list(ACCOUNT_TABLES)):
            conn.execute(f"DROP TABLE {table}")
        for table in ACCOUNT_TABLES:
            conn.execute(f"ALTER TABLE {table}_v3 RENAME TO {table}")
        for ddl in ACCOUNTS_INDEXES:
            conn.execute(ddl)
        conn.execute("DELETE FROM settings WHERE key IN ('owner_password_hash', 'business_name', 'auth_version')")
        problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise sqlite3.IntegrityError(f"Account migration found {len(problems)} broken link(s); nothing changed.")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


MIGRATIONS = [_migrate_1, _migrate_2, _migrate_3]
SCHEMA_VERSION = len(MIGRATIONS)

# Business tables, children first (deleting an account's data goes in this order).
DATA_TABLES = ["audit_events", "renewals", "licenses", "order_items", "orders", "customers", "products"]


def now_iso():
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def connect(path):
    conn = sqlite3.connect(path, detect_types=0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(path, backup_dir=None, legacy_email=None):
    """Create or upgrade the database. Before the multi-account upgrade of a
    database that already holds data, a full backup copy is written first
    (a timestamped file in backup_dir); nothing is changed if that copy fails."""
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < ACCOUNTS_VERSION and _has_single_user_data(conn):
            backup_file(path, backup_dir or os.path.join(os.path.dirname(os.path.abspath(path)), "backups"),
                        "before-accounts")
        for index in range(version, SCHEMA_VERSION):
            if MIGRATIONS[index] is _migrate_3:
                _migrate_3(conn, legacy_email)
            else:
                MIGRATIONS[index](conn)
            conn.execute(f"PRAGMA user_version = {index + 1}")
            conn.commit()
    finally:
        conn.close()


def backup_file(db_path, backup_dir, label):
    """Copy the database with SQLite's online backup API. Returns the new file's path."""
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = os.path.join(backup_dir, f"licenseledger-{label}-{stamp}.db")
    src, dst = sqlite3.connect(db_path), sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


def seed_templates(conn, account_id):
    for name, subject, body in DEFAULT_TEMPLATES:
        conn.execute(
            "INSERT INTO reminder_templates (account_id, name, subject, body, created_at) VALUES (?, ?, ?, ?, ?)",
            (account_id, name, subject, body, now_iso()),
        )


def get_setting(db, key, default=None):
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(db, key, value):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
