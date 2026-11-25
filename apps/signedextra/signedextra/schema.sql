-- SignedExtra schema (SQLite). Money is stored in integer cents,
-- quantities in integer hundredths, percentages in basis points (1% = 100).

CREATE TABLE IF NOT EXISTS companies (
    id                  INTEGER PRIMARY KEY,
    name                TEXT NOT NULL,
    address             TEXT NOT NULL DEFAULT '',
    phone               TEXT NOT NULL DEFAULT '',
    email               TEXT NOT NULL DEFAULT '',
    default_markup_bp   INTEGER NOT NULL DEFAULT 1500 CHECK (default_markup_bp BETWEEN 0 AND 10000),
    notice_hours        INTEGER NOT NULL DEFAULT 48 CHECK (notice_hours BETWEEN 1 AND 720),
    foreman_sees_prices INTEGER NOT NULL DEFAULT 0 CHECK (foreman_sees_prices IN (0, 1)),
    is_demo             INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0, 1)),
    next_ticket_no      INTEGER NOT NULL DEFAULT 1,
    next_invoice_no     INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    company_id    INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT,
    role          TEXT NOT NULL CHECK (role IN ('owner', 'office', 'foreman')),
    active        INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    session_epoch INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_company ON users(company_id);

CREATE TABLE IF NOT EXISTS invites (
    id          INTEGER PRIMARY KEY,
    company_id  INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  TEXT NOT NULL UNIQUE,
    expires_at  TEXT NOT NULL,
    used_at     TEXT
);

CREATE TABLE IF NOT EXISTS login_attempts (
    id         INTEGER PRIMARY KEY,
    key        TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_attempts_key ON login_attempts(key, created_at);

CREATE TABLE IF NOT EXISTS labor_rates (
    id          INTEGER PRIMARY KEY,
    company_id  INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    label       TEXT NOT NULL,
    rate_cents  INTEGER NOT NULL CHECK (rate_cents >= 0),
    active      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    UNIQUE (company_id, label)
);

CREATE TABLE IF NOT EXISTS customers (
    id          INTEGER PRIMARY KEY,
    company_id  INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    address     TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_customers_company ON customers(company_id);

CREATE TABLE IF NOT EXISTS customer_contacts (
    id              INTEGER PRIMARY KEY,
    company_id      INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    customer_id     INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    email           TEXT NOT NULL DEFAULT '',
    phone           TEXT NOT NULL DEFAULT '',
    receives_notices INTEGER NOT NULL DEFAULT 1 CHECK (receives_notices IN (0, 1))
);
CREATE INDEX IF NOT EXISTS idx_contacts_customer ON customer_contacts(customer_id);

CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY,
    company_id      INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    customer_id     INTEGER NOT NULL REFERENCES customers(id),
    name            TEXT NOT NULL,
    address         TEXT NOT NULL DEFAULT '',
    gc_reference    TEXT NOT NULL DEFAULT '',
    markup_bp       INTEGER CHECK (markup_bp IS NULL OR markup_bp BETWEEN 0 AND 10000),
    notice_hours    INTEGER CHECK (notice_hours IS NULL OR notice_hours BETWEEN 1 AND 720),
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'closed')),
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company_id);

CREATE TABLE IF NOT EXISTS job_members (
    job_id   INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (job_id, user_id)
);

CREATE TABLE IF NOT EXISTS tickets (
    id                  INTEGER PRIMARY KEY,
    company_id          INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    job_id              INTEGER NOT NULL REFERENCES jobs(id),
    number              INTEGER NOT NULL,
    status              TEXT NOT NULL DEFAULT 'draft' CHECK (status IN (
                            'draft', 'awaiting_signature', 'signed', 'declined',
                            'notice_pending', 'noticed', 'invoiced', 'paid',
                            'disputed', 'void')),
    description         TEXT NOT NULL,
    requested_by_name   TEXT NOT NULL,
    requested_by_role   TEXT NOT NULL DEFAULT '',
    work_date           TEXT NOT NULL,
    markup_bp           INTEGER NOT NULL,
    labor_cents         INTEGER NOT NULL DEFAULT 0,
    material_cents      INTEGER NOT NULL DEFAULT 0,
    markup_cents        INTEGER NOT NULL DEFAULT 0,
    total_cents         INTEGER NOT NULL DEFAULT 0,
    version             INTEGER NOT NULL DEFAULT 1,
    content_hash        TEXT NOT NULL DEFAULT '',
    invoice_id          INTEGER REFERENCES invoices(id),
    void_reason         TEXT NOT NULL DEFAULT '',
    created_by          INTEGER NOT NULL REFERENCES users(id),
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    locked_at           TEXT,
    UNIQUE (company_id, number)
);
CREATE INDEX IF NOT EXISTS idx_tickets_company_status ON tickets(company_id, status);
CREATE INDEX IF NOT EXISTS idx_tickets_job ON tickets(job_id);

CREATE TABLE IF NOT EXISTS ticket_lines (
    id              INTEGER PRIMARY KEY,
    ticket_id       INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL CHECK (kind IN ('labor', 'material', 'equipment')),
    description     TEXT NOT NULL,
    labor_rate_id   INTEGER REFERENCES labor_rates(id),
    qty_x100        INTEGER NOT NULL CHECK (qty_x100 > 0),
    unit            TEXT NOT NULL DEFAULT '',
    -- NULL rate means "not priced yet"; the office prices it before invoicing.
    rate_cents      INTEGER CHECK (rate_cents IS NULL OR rate_cents >= 0),
    amount_cents    INTEGER NOT NULL DEFAULT 0 CHECK (amount_cents >= 0),
    position        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_lines_ticket ON ticket_lines(ticket_id);

CREATE TABLE IF NOT EXISTS ticket_versions (
    id          INTEGER PRIMARY KEY,
    ticket_id   INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    version     INTEGER NOT NULL,
    snapshot    TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    edited_by   INTEGER NOT NULL REFERENCES users(id),
    edited_at   TEXT NOT NULL,
    UNIQUE (ticket_id, version)
);

CREATE TABLE IF NOT EXISTS attachments (
    id           INTEGER PRIMARY KEY,
    company_id   INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    ticket_id    INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    stored_name  TEXT NOT NULL UNIQUE,
    content_type TEXT NOT NULL,
    size_bytes   INTEGER NOT NULL,
    sha256       TEXT NOT NULL,
    uploaded_by  INTEGER NOT NULL REFERENCES users(id),
    uploaded_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attachments_ticket ON attachments(ticket_id);

CREATE TABLE IF NOT EXISTS signature_requests (
    id           INTEGER PRIMARY KEY,
    company_id   INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    ticket_id    INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    token_hash   TEXT NOT NULL UNIQUE,
    version      INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    created_by   INTEGER NOT NULL REFERENCES users(id),
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    used_at      TEXT,
    revoked_at   TEXT
);

CREATE TABLE IF NOT EXISTS signatures (
    id              INTEGER PRIMARY KEY,
    company_id      INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    ticket_id       INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    request_id      INTEGER REFERENCES signature_requests(id),
    method          TEXT NOT NULL CHECK (method IN ('on_device', 'link')),
    outcome         TEXT NOT NULL CHECK (outcome IN ('signed', 'declined')),
    signer_name     TEXT NOT NULL,
    signer_title    TEXT NOT NULL DEFAULT '',
    signer_contact  TEXT NOT NULL DEFAULT '',
    decline_reason  TEXT NOT NULL DEFAULT '',
    signature_png   BLOB,
    version         INTEGER NOT NULL,
    content_hash    TEXT NOT NULL,
    ip_address      TEXT NOT NULL DEFAULT '',
    user_agent      TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signatures_ticket ON signatures(ticket_id);

CREATE TABLE IF NOT EXISTS notices (
    id            INTEGER PRIMARY KEY,
    company_id    INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    ticket_id     INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    reason        TEXT NOT NULL CHECK (reason IN ('refused', 'no_response', 'declined')),
    recipients    TEXT NOT NULL,
    subject       TEXT NOT NULL,
    body          TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'prepared' CHECK (status IN ('prepared', 'sent')),
    sent_method   TEXT NOT NULL DEFAULT '',
    sent_at       TEXT,
    marked_by     INTEGER REFERENCES users(id),
    created_by    INTEGER NOT NULL REFERENCES users(id),
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoices (
    id           INTEGER PRIMARY KEY,
    company_id   INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    job_id       INTEGER NOT NULL REFERENCES jobs(id),
    number       INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'sent', 'paid', 'disputed')),
    total_cents  INTEGER NOT NULL,
    notes        TEXT NOT NULL DEFAULT '',
    created_by   INTEGER NOT NULL REFERENCES users(id),
    created_at   TEXT NOT NULL,
    sent_at      TEXT,
    paid_at      TEXT,
    UNIQUE (company_id, number)
);

-- Append-only audit trail. Triggers below reject UPDATE and DELETE.
CREATE TABLE IF NOT EXISTS audit_events (
    id          INTEGER PRIMARY KEY,
    company_id  INTEGER NOT NULL,
    ticket_id   INTEGER,
    user_id     INTEGER,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    details     TEXT NOT NULL DEFAULT '{}',
    ip_address  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_ticket ON audit_events(ticket_id);

CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only');
END;
