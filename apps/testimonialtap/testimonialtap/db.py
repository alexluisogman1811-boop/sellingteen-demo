"""SQLite access helpers. All queries use bound parameters.

Multi-account rule: every owner-side query filters by the logged-in account's id
(``current_account_id()``). Public pages look projects up by their unguessable token only.
"""
import os
import secrets
import sqlite3
from datetime import datetime, timezone

from flask import abort, current_app, g
from werkzeug.security import generate_password_hash

# Placeholder login for data that existed before accounts were added (see _assign_legacy_data).
LEGACY_OWNER_EMAIL = "owner@testimonialtap.invalid"
ACCOUNT_TABLES = ("projects", "testimonials", "activity")


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def get_db():
    if "db" not in g:
        conn = sqlite3.connect(current_app.config["DATABASE"], timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


def close_db(exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def _columns(db, table):
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}


def _needs_account_migration(db):
    """True for a database created before accounts existed (tables present, no account_id)."""
    for table in ACCOUNT_TABLES:
        cols = _columns(db, table)
        if cols and "account_id" not in cols:
            return True
    return False


def backup_database(path, label):
    """Copy the database file safely (SQLite online backup) next to the original. Returns the path."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = f"{path}.{label}-{stamp}.bak"
    src = sqlite3.connect(path)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return target


def init_db(app):
    path = app.config["DATABASE"]
    with app.app_context():
        db = get_db()
        if path != ":memory:" and os.path.exists(path) and _needs_account_migration(db):
            # Never change an existing single-owner database without a copy first.
            app.config["MIGRATION_BACKUP"] = backup_database(path, "before-accounts")
            app.logger.warning("Database backed up before adding accounts: %s",
                               os.path.basename(app.config["MIGRATION_BACKUP"]))
        with app.open_resource("schema.sql") as f:
            db.executescript(f.read().decode("utf-8"))
        _migrate(db)
        account_id = _assign_legacy_data(db)
        db.commit()
        if account_id:
            app.logger.warning("Existing projects and testimonials were assigned to account #%s", account_id)
        close_db()


def _migrate(db):
    """Bring databases created by earlier versions up to the current schema."""
    if "submission_key" not in _columns(db, "testimonials"):
        db.execute("ALTER TABLE testimonials ADD COLUMN submission_key TEXT")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_testimonials_submission_key "
               "ON testimonials(submission_key)")
    for table in ACCOUNT_TABLES:
        if "account_id" not in _columns(db, table):
            db.execute(f"ALTER TABLE {table} ADD COLUMN account_id INTEGER "
                       "REFERENCES accounts(id) ON DELETE CASCADE")
        db.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_account ON {table}(account_id)")


def _setting(db, key):
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _assign_legacy_data(db):
    """Give data from the single-owner version (no account_id) to the first account.

    If no account exists yet, one is created from the old owner settings: same password,
    the old business name, and the login email TESTIMONIALTAP_OWNER_EMAIL (or a placeholder
    the owner can change on the Profile page). Returns the account id, or None if nothing to do.
    """
    orphans = any(db.execute(f"SELECT 1 FROM {t} WHERE account_id IS NULL LIMIT 1").fetchone()
                  for t in ACCOUNT_TABLES)
    old_hash = _setting(db, "owner_password_hash")
    has_accounts = db.execute("SELECT 1 FROM accounts LIMIT 1").fetchone() is not None
    if not orphans and not (old_hash and not has_accounts):
        return None
    first = db.execute("SELECT id FROM accounts ORDER BY id LIMIT 1").fetchone()
    if first:
        account_id = first["id"]
    else:
        email = (os.environ.get("TESTIMONIALTAP_OWNER_EMAIL") or LEGACY_OWNER_EMAIL).strip().lower()
        cur = db.execute(
            "INSERT INTO accounts (business_name, email, password_hash, website, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (_setting(db, "profile_business_name") or "", email,
             old_hash or generate_password_hash(secrets.token_urlsafe(32)),
             _setting(db, "profile_website") or "", utcnow()),
        )
        account_id = cur.lastrowid
    db.execute("UPDATE projects SET account_id = ? WHERE account_id IS NULL", (account_id,))
    db.execute("UPDATE testimonials SET account_id = COALESCE("
               "(SELECT p.account_id FROM projects p WHERE p.id = testimonials.project_id), ?) "
               "WHERE account_id IS NULL", (account_id,))
    db.execute("UPDATE activity SET account_id = ? WHERE account_id IS NULL", (account_id,))
    # The owner settings now live on the account (the backup keeps the originals).
    db.execute("DELETE FROM settings WHERE key IN "
               "('owner_password_hash', 'profile_business_name', 'profile_website')")
    return account_id


def get_setting(key, default=None):
    row = get_db().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    db = get_db()
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    db.commit()


# --------------------------------------------------------------------------- accounts

def current_account_id():
    """The logged-in account's id. Owner routes are login_required, so this is always set there."""
    account = g.get("account")
    if account is None:
        abort(403)
    return account["id"]


def get_account(account_id):
    return get_db().execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()


def get_account_by_email(email):
    return get_db().execute("SELECT * FROM accounts WHERE email = ?",
                            ((email or "").strip().lower(),)).fetchone()


def get_profile(account_id=None):
    """Business name and website for an account (default: the logged-in one)."""
    row = get_account(account_id if account_id is not None else current_account_id())
    return {"business_name": row["business_name"] if row else "",
            "website": row["website"] if row else ""}


def log_activity(kind, message, project_id=None, testimonial_id=None, is_demo=False, account_id=None):
    get_db().execute(
        "INSERT INTO activity (account_id, kind, message, project_id, testimonial_id, is_demo, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (account_id if account_id is not None else current_account_id(),
         kind, message, project_id, testimonial_id, 1 if is_demo else 0, utcnow()),
    )


def project_or_404(project_id):
    """A project of the logged-in account. Another account's id gives 404, never its data."""
    row = get_db().execute("SELECT * FROM projects WHERE id = ? AND account_id = ?",
                           (project_id, current_account_id())).fetchone()
    if row is None:
        abort(404)
    return row


def testimonial_or_404(testimonial_id):
    """A testimonial of the logged-in account. Another account's id gives 404, never its data."""
    row = get_db().execute(
        "SELECT t.*, p.name AS project_name FROM testimonials t "
        "JOIN projects p ON p.id = t.project_id "
        "WHERE t.id = ? AND t.account_id = ? AND p.account_id = t.account_id",
        (testimonial_id, current_account_id()),
    ).fetchone()
    if row is None:
        abort(404)
    return row


# Only approved testimonials with explicit permission are ever shown publicly.
PUBLISHABLE_SQL = "t.status = 'approved' AND t.permission_granted = 1"


def publishable_for_project(project_id):
    return get_db().execute(
        # Only fields covered by the publication-permission statement (no website).
        "SELECT t.customer_name, t.business, t.feedback, t.rating, t.is_demo, "
        "t.submitted_at FROM testimonials t "
        f"WHERE t.project_id = ? AND {PUBLISHABLE_SQL} "
        "ORDER BY COALESCE(t.status_changed_at, t.submitted_at) DESC, t.id DESC",
        (project_id,),
    ).fetchall()
