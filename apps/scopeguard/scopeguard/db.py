"""SQLite access, schema and default data."""
import sqlite3

from flask import current_app, g

# Tables only. Indexes and triggers live in POST_SCHEMA, which runs after any upgrade so that
# columns added by migrate() exist before anything refers to them.
SCHEMA = """
-- One row per signed-up freelancer or studio. Every other row belongs to exactly one account.
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    legacy_username TEXT,
    is_demo INTEGER NOT NULL DEFAULT 0,
    paid_until TEXT,
    created_at TEXT NOT NULL
);

-- While an account's own data is being reset, its history rows may be deleted (see triggers).
CREATE TABLE IF NOT EXISTS account_purges (
    account_id INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS settings (
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (account_id, key)
);

CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY,
    account_id INTEGER REFERENCES accounts(id),
    name TEXT NOT NULL,
    email TEXT,
    company TEXT,
    notes TEXT,
    is_demo INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    account_id INTEGER REFERENCES accounts(id),
    client_id INTEGER NOT NULL REFERENCES clients(id),
    name TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'on_hold', 'completed')),
    start_date TEXT,
    deadline TEXT,
    revision_allowance INTEGER NOT NULL DEFAULT 2,
    hourly_rate REAL,
    currency TEXT NOT NULL DEFAULT 'USD',
    is_demo INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deliverables (
    id INTEGER PRIMARY KEY,
    account_id INTEGER REFERENCES accounts(id),
    project_id INTEGER NOT NULL REFERENCES projects(id),
    title TEXT NOT NULL,
    description TEXT,
    revision_allowance INTEGER,
    due_date TEXT,
    archived_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exclusions (
    id INTEGER PRIMARY KEY,
    account_id INTEGER REFERENCES accounts(id),
    project_id INTEGER NOT NULL REFERENCES projects(id),
    text TEXT NOT NULL,
    archived_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS milestones (
    id INTEGER PRIMARY KEY,
    account_id INTEGER REFERENCES accounts(id),
    project_id INTEGER NOT NULL REFERENCES projects(id),
    title TEXT NOT NULL,
    due_date TEXT,
    done INTEGER NOT NULL DEFAULT 0,
    archived_at TEXT,
    created_at TEXT NOT NULL
);

-- Published scope snapshots. Immutable: see triggers below.
CREATE TABLE IF NOT EXISTS scope_versions (
    id INTEGER PRIMARY KEY,
    account_id INTEGER REFERENCES accounts(id),
    project_id INTEGER NOT NULL REFERENCES projects(id),
    version_no INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    change_note TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (project_id, version_no)
);

-- reason is NULL only for requests created before reasons were recorded (schema v1).
-- estimated_hours / proposed_cost are NULL when no estimate was given; never filled in by the app.
CREATE TABLE IF NOT EXISTS change_requests (
    id INTEGER PRIMARY KEY,
    account_id INTEGER REFERENCES accounts(id),
    project_id INTEGER NOT NULL REFERENCES projects(id),
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    reason TEXT,
    estimated_hours REAL,
    proposed_cost REAL,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'submitted', 'approved', 'rejected', 'withdrawn', 'completed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    submitted_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS revision_requests (
    id INTEGER PRIMARY KEY,
    account_id INTEGER REFERENCES accounts(id),
    project_id INTEGER NOT NULL REFERENCES projects(id),
    deliverable_id INTEGER NOT NULL REFERENCES deliverables(id),
    description TEXT NOT NULL,
    requested_by TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
    created_at TEXT NOT NULL,
    completed_at TEXT
);

-- Only a SHA-256 hash of each client review token is stored.
CREATE TABLE IF NOT EXISTS share_links (
    id INTEGER PRIMARY KEY,
    account_id INTEGER REFERENCES accounts(id),
    project_id INTEGER NOT NULL REFERENCES projects(id),
    token_hash TEXT NOT NULL UNIQUE,
    label TEXT,
    recipient_name TEXT,
    recipient_email TEXT,
    can_decide INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    last_used_at TEXT
);

-- Decisions on scope versions and change requests. Immutable: see triggers below.
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY,
    account_id INTEGER REFERENCES accounts(id),
    project_id INTEGER NOT NULL REFERENCES projects(id),
    subject_type TEXT NOT NULL CHECK (subject_type IN ('scope_version', 'change_request')),
    scope_version_id INTEGER REFERENCES scope_versions(id),
    change_request_id INTEGER REFERENCES change_requests(id),
    decision TEXT NOT NULL CHECK (decision IN ('approved', 'changes_requested', 'rejected')),
    approver_name TEXT NOT NULL,
    approver_email TEXT,
    method TEXT NOT NULL
        CHECK (method IN ('client_review_link', 'recorded_by_freelancer', 'demo_sample')),
    evidence_note TEXT,
    comment TEXT,
    ip_address TEXT,
    user_agent TEXT,
    share_link_id INTEGER REFERENCES share_links(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    account_id INTEGER REFERENCES accounts(id),
    project_id INTEGER REFERENCES projects(id),
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message_templates (
    id INTEGER PRIMARY KEY,
    account_id INTEGER REFERENCES accounts(id),
    name TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

"""

# Tables whose rows belong to an account (settings is keyed by account separately).
TENANT_TABLES = ("clients", "projects", "deliverables", "exclusions", "milestones", "scope_versions",
                 "change_requests", "revision_requests", "share_links", "approvals", "events", "message_templates")
# Tables whose rows hang off a project and must carry that project's account.
PROJECT_CHILD_TABLES = ("deliverables", "exclusions", "milestones", "scope_versions", "change_requests",
                        "revision_requests", "share_links", "approvals", "events")
IMMUTABLE_TABLES = ("scope_versions", "approvals", "events")

POST_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_projects_client ON projects(client_id);
CREATE INDEX IF NOT EXISTS idx_versions_project ON scope_versions(project_id);
CREATE INDEX IF NOT EXISTS idx_approvals_project ON approvals(project_id);
CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_id);
CREATE INDEX IF NOT EXISTS idx_clients_account ON clients(account_id);
CREATE INDEX IF NOT EXISTS idx_projects_account ON projects(account_id);
CREATE INDEX IF NOT EXISTS idx_templates_account ON message_templates(account_id);

-- History is append-only. Rows may only be deleted for sample (demo) projects, or during a
-- confirmed, backed-up reset of the owning account's own data (row in account_purges).
CREATE TRIGGER IF NOT EXISTS scope_versions_no_update BEFORE UPDATE ON scope_versions
BEGIN SELECT RAISE(ABORT, 'scope versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS scope_versions_no_delete BEFORE DELETE ON scope_versions
WHEN (SELECT is_demo FROM projects WHERE id = OLD.project_id) = 0
 AND NOT EXISTS (SELECT 1 FROM account_purges WHERE account_id = OLD.account_id)
BEGIN SELECT RAISE(ABORT, 'scope versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS approvals_no_update BEFORE UPDATE ON approvals
BEGIN SELECT RAISE(ABORT, 'approval records are immutable'); END;
CREATE TRIGGER IF NOT EXISTS approvals_no_delete BEFORE DELETE ON approvals
WHEN (SELECT is_demo FROM projects WHERE id = OLD.project_id) = 0
 AND NOT EXISTS (SELECT 1 FROM account_purges WHERE account_id = OLD.account_id)
BEGIN SELECT RAISE(ABORT, 'approval records are immutable'); END;
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'history events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
WHEN (SELECT is_demo FROM projects WHERE id = OLD.project_id) = 0
 AND NOT EXISTS (SELECT 1 FROM account_purges WHERE account_id = OLD.account_id)
BEGIN SELECT RAISE(ABORT, 'history events are immutable'); END;

-- Account integrity: every row needs an account, and it must be the same account as the row it
-- belongs to. A query that forgets the account fails loudly instead of mixing businesses' data.
CREATE TRIGGER IF NOT EXISTS clients_account_check BEFORE INSERT ON clients
WHEN NEW.account_id IS NULL
BEGIN SELECT RAISE(ABORT, 'account required'); END;
CREATE TRIGGER IF NOT EXISTS message_templates_account_check BEFORE INSERT ON message_templates
WHEN NEW.account_id IS NULL
BEGIN SELECT RAISE(ABORT, 'account required'); END;
CREATE TRIGGER IF NOT EXISTS projects_account_check BEFORE INSERT ON projects
WHEN NEW.account_id IS NULL OR NEW.account_id IS NOT (SELECT account_id FROM clients WHERE id = NEW.client_id)
BEGIN SELECT RAISE(ABORT, 'account mismatch'); END;
"""

POST_SCHEMA += "".join(
    f"""CREATE TRIGGER IF NOT EXISTS {t}_account_check BEFORE INSERT ON {t}
WHEN NEW.project_id IS NOT NULL AND NEW.account_id IS NOT (SELECT account_id FROM projects WHERE id = NEW.project_id)
BEGIN SELECT RAISE(ABORT, 'account mismatch'); END;
""" for t in PROJECT_CHILD_TABLES
)

OLD_TRIGGERS = ("scope_versions_no_update", "scope_versions_no_delete", "approvals_no_update", "approvals_no_delete",
                "events_no_update", "events_no_delete")

DEFAULT_TEMPLATES = [
    (
        "Scope review request",
        "Please review the scope for {project_name}",
        "Hi {client_name},\n\n"
        "I've documented the agreed scope for {project_name} (version {scope_version}).\n"
        "Please review the deliverables, exclusions and milestones here:\n{review_link}\n\n"
        "If everything looks right you can approve it on that page, or request changes "
        "if something needs adjusting.\n\nThanks,\n{freelancer_name}",
    ),
    (
        "Change request proposal",
        "Change request for {project_name}: {change_title}",
        "Hi {client_name},\n\n"
        "You asked for: {change_title}.\n"
        "This falls outside the agreed scope.\n"
        "Reason: {change_reason}\n"
        "Time impact: {change_hours} extra hours. Price impact: {change_cost}.\n\n"
        "You can approve or decline it here:\n{review_link}\n\n"
        "I'll start once it's approved.\n\nThanks,\n{freelancer_name}",
    ),
    (
        "Revision allowance reached",
        "Revisions on {project_name}",
        "Hi {client_name},\n\n"
        "Just a heads-up: the agreed number of revisions for {project_name} "
        "({revision_allowance} per deliverable) has now been used for at least one deliverable.\n"
        "I'm happy to keep going; further rounds would be handled as a change request "
        "so we both have a clear record.\n\nThanks,\n{freelancer_name}",
    ),
    (
        "Thanks for approving",
        "Scope approved for {project_name}",
        "Hi {client_name},\n\n"
        "Thanks for approving version {scope_version} of the scope for {project_name}. "
        "I've saved a record of the approval and will keep you posted on progress.\n\n"
        "Best,\n{freelancer_name}",
    ),
]


def get_db():
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


SCHEMA_VERSION = 3


def init_db(db=None, backup_dir=None):
    db = db or get_db()
    fresh = db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'projects'"
    ).fetchone()[0] == 0
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if not fresh and version < SCHEMA_VERSION and backup_dir:
        # FIRST, before anything in the file changes: a timestamped copy of the whole database.
        from .demo import backup_database

        backup_database(db, backup_dir, label=f"pre-migration-v{version}")
    db.executescript(SCHEMA)
    if fresh:
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    else:
        migrate(db)
    db.executescript(POST_SCHEMA)
    db.commit()


def seed_templates(db, account_id):
    """Give a new account the default message templates (it can edit or delete them)."""
    from .services import now

    db.executemany(
        "INSERT INTO message_templates (account_id, name, subject, body, updated_at) VALUES (?, ?, ?, ?, ?)",
        [(account_id, n, s, b, now()) for n, s, b in DEFAULT_TEMPLATES],
    )


# ---------------------------------------------------------------- schema upgrades


def _columns(db, table):
    return {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}


def migrate(db):
    """Upgrade an existing database in place. init_db() has already written a backup copy of the
    database file before calling this; nothing is deleted."""
    from .services import log_event

    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return

    # The append-only triggers would block filling in the new account column; POST_SCHEMA
    # recreates them (in their account-aware form) right after this upgrade.
    for name in OLD_TRIGGERS:
        db.execute(f"DROP TRIGGER IF EXISTS {name}")
    for table in TENANT_TABLES:
        if "account_id" not in _columns(db, table):
            db.execute(f"ALTER TABLE {table} ADD COLUMN account_id INTEGER REFERENCES accounts(id)")

    if "reason" not in _columns(db, "change_requests"):
        # SQLite cannot alter a CHECK constraint, so rebuild the table (documented 12-step method).
        db.execute("PRAGMA foreign_keys = OFF")
        db.execute("BEGIN")
        columns_sql = SCHEMA.split("CREATE TABLE IF NOT EXISTS change_requests", 1)[1].split(";", 1)[0]
        db.execute("CREATE TABLE change_requests_new" + columns_sql)
        db.execute(
            "INSERT INTO change_requests_new (id, project_id, title, description, reason, estimated_hours,"
            " proposed_cost, status, created_at, updated_at, submitted_at, completed_at)"
            " SELECT cr.id, cr.project_id, cr.title, cr.description, NULL, cr.estimated_hours, cr.proposed_cost,"
            "  CASE cr.status"
            "    WHEN 'proposed' THEN 'submitted'"
            # Before v2 the freelancer could set 'rejected' to withdraw a request. A rejection with
            # no client decision record was such a withdrawal, so it is labelled accurately now.
            "    WHEN 'rejected' THEN CASE WHEN EXISTS (SELECT 1 FROM approvals a"
            "         WHERE a.change_request_id = cr.id AND a.decision = 'rejected')"
            "         THEN 'rejected' ELSE 'withdrawn' END"
            "    ELSE cr.status END,"
            "  cr.created_at, cr.updated_at, cr.created_at, cr.completed_at"
            " FROM change_requests cr"
        )
        withdrawn = db.execute(
            "SELECT n.id, n.project_id, n.title FROM change_requests_new n JOIN change_requests o ON o.id = n.id"
            " WHERE o.status = 'rejected' AND n.status = 'withdrawn'"
        ).fetchall()
        db.execute("DROP TABLE change_requests")
        db.execute("ALTER TABLE change_requests_new RENAME TO change_requests")
        problems = db.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            db.execute("ROLLBACK")
            raise RuntimeError(f"Migration aborted, foreign key problems: {problems}")
        for cr in withdrawn:
            log_event(db, cr["project_id"], "migration",
                      f"Schema upgrade: change request “{cr['title']}” had been set to rejected by the freelancer "
                      "with no client decision on record; it is now labelled withdrawn.", "system")
        db.execute("COMMIT")
        db.execute("PRAGMA foreign_keys = ON")

    columns = _columns(db, "share_links")
    if "recipient_name" not in columns:
        db.execute("ALTER TABLE share_links ADD COLUMN recipient_name TEXT")
        db.execute("ALTER TABLE share_links ADD COLUMN recipient_email TEXT")
        db.execute("ALTER TABLE share_links ADD COLUMN can_decide INTEGER NOT NULL DEFAULT 1")
        # Existing links were issued to the project's client; record that explicitly.
        db.execute(
            "UPDATE share_links SET recipient_name = (SELECT c.name FROM projects p JOIN clients c"
            " ON c.id = p.client_id WHERE p.id = share_links.project_id),"
            " recipient_email = (SELECT c.email FROM projects p JOIN clients c"
            " ON c.id = p.client_id WHERE p.id = share_links.project_id)"
        )
    if version < 3:
        _assign_first_account(db, log_event)
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    log_event(db, None, "migration", f"Database upgraded from schema v{version} to v{SCHEMA_VERSION}.", "system")
    db.commit()


def _assign_first_account(db, log_event):
    """v3: the single-owner data becomes the first account. Its owner keeps their password and can
    still sign in with their old username until they set a real email on the Settings page."""
    from .services import now

    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    user = db.execute("SELECT * FROM users ORDER BY id LIMIT 1").fetchone() if "users" in tables else None
    old_settings = {}
    if "account_id" not in _columns(db, "settings"):
        old_settings = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM settings")}
    has_data = any(db.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone() for t in ("clients", "projects"))

    account_id = None
    if user is not None or has_data:
        existing = db.execute("SELECT id FROM accounts ORDER BY id LIMIT 1").fetchone()
        if existing:
            account_id = existing["id"]
        else:
            username = user["username"] if user else None
            email = username if username and "@" in username else f"owner-{user['id'] if user else 1}@scopeguard.invalid"
            name = old_settings.get("freelancer_name") or username or "ScopeGuard owner"
            # "!" is never a valid password hash, so a data-only database has no usable login until
            # an operator runs `flask set-password`.
            password_hash = user["password_hash"] if user else "!"
            # The free trial starts at the upgrade, so the existing owner is not locked out on day one.
            account_id = db.execute(
                "INSERT INTO accounts (name, email, password_hash, legacy_username, created_at) VALUES (?, ?, ?, ?, ?)",
                (name, email, password_hash, username, now()),
            ).lastrowid
        db.execute("UPDATE clients SET account_id = ? WHERE account_id IS NULL", (account_id,))
        db.execute("UPDATE message_templates SET account_id = ? WHERE account_id IS NULL", (account_id,))
        db.execute("UPDATE projects SET account_id = (SELECT account_id FROM clients WHERE clients.id = projects.client_id)"
                   " WHERE account_id IS NULL")
        for table in PROJECT_CHILD_TABLES:
            db.execute(f"UPDATE {table} SET account_id = (SELECT account_id FROM projects WHERE projects.id = {table}.project_id)"
                       " WHERE account_id IS NULL AND project_id IS NOT NULL")
        db.execute("UPDATE events SET account_id = ? WHERE account_id IS NULL", (account_id,))
        log_event(db, None, "migration",
                  f"Schema upgrade: existing data assigned to account #{account_id}.", "system")
        db.execute("UPDATE events SET account_id = ? WHERE account_id IS NULL", (account_id,))
    else:
        # Nothing to keep: default templates created by older versions without an owner.
        db.execute("DELETE FROM message_templates WHERE account_id IS NULL")

    if "account_id" not in _columns(db, "settings"):
        db.execute("ALTER TABLE settings RENAME TO settings_v2")
        db.execute("CREATE TABLE settings (account_id INTEGER NOT NULL REFERENCES accounts(id), key TEXT NOT NULL,"
                   " value TEXT NOT NULL, PRIMARY KEY (account_id, key))")
        if account_id is not None:
            db.executemany("INSERT INTO settings (account_id, key, value) VALUES (?, ?, ?)",
                           [(account_id, k, v) for k, v in old_settings.items()])
        db.execute("DROP TABLE settings_v2")
