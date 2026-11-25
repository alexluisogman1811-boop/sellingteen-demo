-- ContentQueue schema. Idempotent: safe to run on every start.
-- Every content table carries account_id; every query filters by the signed-in account.
-- Indexes on account_id are created in db.migrate(), after older databases are upgraded.

CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    business_name TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,             -- salted hash, never the password
    timezone      TEXT NOT NULL DEFAULT '',  -- IANA name, e.g. Asia/Bangkok; '' = server default
    is_demo       INTEGER NOT NULL DEFAULT 0,
    paid_until    TEXT,                      -- YYYY-MM-DD the subscription is switched on until
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaigns (
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

CREATE TABLE IF NOT EXISTS categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
    name        TEXT NOT NULL COLLATE NOCASE,
    description TEXT NOT NULL DEFAULT '',
    is_demo     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    UNIQUE (account_id, name)
);

CREATE TABLE IF NOT EXISTS posts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id     INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
    title          TEXT NOT NULL,
    caption        TEXT NOT NULL DEFAULT '',
    hashtags       TEXT NOT NULL DEFAULT '',
    platform       TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'draft'
                   CHECK (status IN ('draft', 'review', 'scheduled', 'published', 'archived')),
    campaign_id    INTEGER REFERENCES campaigns(id) ON DELETE SET NULL,
    category_id    INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    planned_date   TEXT,  -- YYYY-MM-DD (the account's local time)
    planned_time   TEXT,  -- HH:MM, optional
    notes          TEXT NOT NULL DEFAULT '',
    image_filename TEXT,
    published_at   TEXT,  -- set only when the user confirms manual publishing
    published_url  TEXT NOT NULL DEFAULT '',
    is_demo        INTEGER NOT NULL DEFAULT 0,
    version        INTEGER NOT NULL DEFAULT 1,  -- bumped on every change; detects stale edits
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_posts_planned ON posts (planned_date, planned_time);
CREATE INDEX IF NOT EXISTS idx_posts_status ON posts (status);

CREATE TABLE IF NOT EXISTS templates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    body       TEXT NOT NULL,
    hashtags   TEXT NOT NULL DEFAULT '',
    platform   TEXT,
    is_demo    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ideas (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    notes       TEXT NOT NULL DEFAULT '',
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    status      TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'used')),
    is_demo     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
