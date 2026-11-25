-- TestimonialTap schema. Safe to run repeatedly.

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per signed-up freelancer / agency. Every project, testimonial and activity
-- row belongs to exactly one account (account_id), and every owner query filters by it.
CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    business_name TEXT NOT NULL DEFAULT '',
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    website       TEXT NOT NULL DEFAULT '',
    is_demo       INTEGER NOT NULL DEFAULT 0,
    paid_until    TEXT,                    -- set by the activate/deactivate commands
    created_at    TEXT NOT NULL            -- the 30-day free trial counts from here
);

-- account_id columns are added by db._migrate() for databases from earlier versions,
-- and their indexes are created there too (after the column exists).
CREATE TABLE IF NOT EXISTS projects (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    client_name   TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL DEFAULT '',
    intro_message TEXT NOT NULL DEFAULT '',
    submit_token  TEXT NOT NULL UNIQUE,   -- public submission link
    wall_token    TEXT NOT NULL UNIQUE,   -- public display page / embed
    accepting     INTEGER NOT NULL DEFAULT 1,
    is_demo       INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS testimonials (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id         INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
    project_id         INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    customer_name      TEXT NOT NULL,
    business           TEXT NOT NULL DEFAULT '',
    feedback           TEXT NOT NULL,
    rating             INTEGER CHECK (rating IS NULL OR rating BETWEEN 1 AND 5),
    website            TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'approved', 'rejected', 'archived')),
    -- Publication permission is recorded separately from the submission itself.
    permission_granted INTEGER NOT NULL DEFAULT 0,
    permission_source  TEXT NOT NULL DEFAULT '',  -- 'submitter' | 'owner' | ''
    permission_note    TEXT NOT NULL DEFAULT '',
    permission_at      TEXT,
    edited             INTEGER NOT NULL DEFAULT 0,
    edited_at          TEXT,
    original_json      TEXT,                      -- snapshot of the submitted wording before owner edits
    is_demo            INTEGER NOT NULL DEFAULT 0,
    submission_key     TEXT,                      -- single-use form nonce; blocks double submits
    submitted_at       TEXT NOT NULL,
    status_changed_at  TEXT,
    updated_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_testimonials_project ON testimonials(project_id);
CREATE INDEX IF NOT EXISTS idx_testimonials_status ON testimonials(status);
CREATE INDEX IF NOT EXISTS idx_testimonials_submitted ON testimonials(submitted_at);

CREATE TABLE IF NOT EXISTS activity (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id     INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL,
    message        TEXT NOT NULL,
    project_id     INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    testimonial_id INTEGER REFERENCES testimonials(id) ON DELETE SET NULL,
    is_demo        INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_activity_created ON activity(created_at);
