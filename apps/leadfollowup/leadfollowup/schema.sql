PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    business_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    is_demo INTEGER NOT NULL DEFAULT 0   -- created by `flask seed-demo`; safe to reset
);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    phone TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    service TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'New',
    next_follow_up TEXT,              -- ISO date (YYYY-MM-DD) or NULL
    created_at TEXT NOT NULL,         -- ISO UTC timestamp
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leads_user_followup ON leads (user_id, next_follow_up);
CREATE INDEX IF NOT EXISTS idx_leads_user_updated ON leads (user_id, updated_at);

-- Contact history. method = 'Note' for internal notes, otherwise how the
-- customer was contacted. contact_date is the business-local date of contact.
-- (method/contact_date are added to older databases by db.migrate.)
CREATE TABLE IF NOT EXISTS lead_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    method TEXT NOT NULL DEFAULT 'Note',
    contact_date TEXT
);

CREATE INDEX IF NOT EXISTS idx_notes_lead ON lead_notes (lead_id);
