-- SlotSimple schema (version 2: many businesses, one row in `accounts` each).
-- Every table that holds business data carries account_id, and every query in
-- the app filters on it. All absolute instants are stored as UTC ISO-8601
-- strings ("YYYY-MM-DDTHH:MM:SSZ"); wall-clock rules (opening hours,
-- availability windows, blocked periods) are local times read in that
-- account's timezone setting.
--
-- Statements are separated by a comment line made of two dashes and a
-- semicolon, so db.py can run them one by one (the v1 -> v2 migration reuses
-- the table definitions).

CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY,
    slug          TEXT NOT NULL UNIQUE,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_demo       INTEGER NOT NULL DEFAULT 0,
    paid_until    TEXT,
    created_at    TEXT NOT NULL
);
-- ;
CREATE TABLE IF NOT EXISTS settings (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    PRIMARY KEY (account_id, key)
);
-- ;
CREATE TABLE IF NOT EXISTS opening_hours (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    weekday    INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    is_open    INTEGER NOT NULL DEFAULT 0,
    open_time  TEXT,
    close_time TEXT,
    PRIMARY KEY (account_id, weekday)
);
-- ;
CREATE TABLE IF NOT EXISTS availability_rules (
    id         INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    weekday    INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    start_time TEXT NOT NULL,
    end_time   TEXT NOT NULL CHECK (end_time > start_time)
);
-- ;
CREATE TABLE IF NOT EXISTS blocked_periods (
    id          INTEGER PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    start_local TEXT NOT NULL,
    end_local   TEXT NOT NULL CHECK (end_local > start_local),
    reason      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
-- ;
CREATE TABLE IF NOT EXISTS services (
    id               INTEGER PRIMARY KEY,
    account_id       INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    name             TEXT NOT NULL,
    description      TEXT NOT NULL DEFAULT '',
    price_cents      INTEGER NOT NULL DEFAULT 0 CHECK (price_cents >= 0),
    duration_minutes INTEGER NOT NULL CHECK (duration_minutes > 0),
    active           INTEGER NOT NULL DEFAULT 1,
    sort_order       INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
-- ;
CREATE TABLE IF NOT EXISTS bookings (
    id               INTEGER PRIMARY KEY,
    account_id       INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    reference        TEXT NOT NULL UNIQUE,
    service_id       INTEGER REFERENCES services(id) ON DELETE SET NULL,
    service_name     TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL,
    price_cents      INTEGER NOT NULL,
    customer_name    TEXT NOT NULL,
    customer_email   TEXT NOT NULL,
    notes            TEXT NOT NULL DEFAULT '',
    start_utc        TEXT NOT NULL,
    end_utc          TEXT NOT NULL CHECK (end_utc > start_utc),
    status           TEXT NOT NULL CHECK (status IN ('pending','confirmed','cancelled','completed')),
    cancel_reason    TEXT NOT NULL DEFAULT '',
    is_demo          INTEGER NOT NULL DEFAULT 0,
    request_token    TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
-- ;
CREATE TABLE IF NOT EXISTS booking_events (
    id         INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    booking_id INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    event      TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    actor      TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- ;
CREATE INDEX IF NOT EXISTS idx_rules_account ON availability_rules(account_id, weekday);
-- ;
CREATE INDEX IF NOT EXISTS idx_blocks_account ON blocked_periods(account_id, start_local);
-- ;
CREATE INDEX IF NOT EXISTS idx_services_account ON services(account_id);
-- ;
CREATE INDEX IF NOT EXISTS idx_bookings_account_start ON bookings(account_id, start_utc);
-- ;
CREATE INDEX IF NOT EXISTS idx_bookings_account_status ON bookings(account_id, status);
-- ;
CREATE INDEX IF NOT EXISTS idx_bookings_account_email ON bookings(account_id, customer_email);
-- ;
-- Last line of defence against identical double bookings within one business;
-- overlap checks with buffers are enforced in code inside a write transaction.
CREATE UNIQUE INDEX IF NOT EXISTS uq_bookings_active_start
    ON bookings(account_id, start_utc) WHERE status != 'cancelled';
-- ;
-- One booking per submitted form: makes double-clicks and retries idempotent.
CREATE UNIQUE INDEX IF NOT EXISTS uq_bookings_request_token
    ON bookings(request_token) WHERE request_token IS NOT NULL;
-- ;
CREATE INDEX IF NOT EXISTS idx_events_booking ON booking_events(account_id, booking_id);
