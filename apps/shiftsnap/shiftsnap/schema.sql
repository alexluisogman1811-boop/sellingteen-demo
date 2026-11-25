-- One row per business that signed up. Every other table belongs to exactly one account.
-- email may be NULL only for the account created when an older single-business database
-- was upgraded (its manager sets it on the Account page).
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT UNIQUE COLLATE NOCASE,
    is_demo INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0, 1)),
    paid_until TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    track_availability INTEGER NOT NULL DEFAULT 0 CHECK (track_availability IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS availability (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    UNIQUE (employee_id, weekday)
);

CREATE TABLE IF NOT EXISTS shifts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    employee_id INTEGER REFERENCES employees(id) ON DELETE SET NULL,
    shift_date TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    role TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_shifts_date ON shifts(shift_date);
CREATE INDEX IF NOT EXISTS idx_shifts_employee ON shifts(employee_id, shift_date);

-- Integrity triggers: a last line of defence behind server-side validation. Triggers (unlike
-- new CHECK constraints) also apply to databases created by earlier versions of ShiftSnap.
-- Times must be HH:MM (00:00-23:59); dates must be real calendar dates (date(x) = x
-- rejects e.g. 2026-02-30, which SQLite would otherwise normalise to 2026-03-02).
CREATE TRIGGER IF NOT EXISTS shifts_valid_insert BEFORE INSERT ON shifts
WHEN NEW.shift_date NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
  OR date(NEW.shift_date) IS NOT NEW.shift_date
  OR NEW.start_time NOT GLOB '[0-2][0-9]:[0-5][0-9]' OR NEW.start_time > '23:59'
  OR NEW.end_time NOT GLOB '[0-2][0-9]:[0-5][0-9]' OR NEW.end_time > '23:59'
  OR NEW.start_time = NEW.end_time
  OR trim(NEW.role) = ''
BEGIN
    SELECT RAISE(ABORT, 'invalid shift: bad date, time or empty role');
END;

CREATE TRIGGER IF NOT EXISTS shifts_valid_update BEFORE UPDATE ON shifts
WHEN NEW.shift_date NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
  OR date(NEW.shift_date) IS NOT NEW.shift_date
  OR NEW.start_time NOT GLOB '[0-2][0-9]:[0-5][0-9]' OR NEW.start_time > '23:59'
  OR NEW.end_time NOT GLOB '[0-2][0-9]:[0-5][0-9]' OR NEW.end_time > '23:59'
  OR NEW.start_time = NEW.end_time
  OR trim(NEW.role) = ''
BEGIN
    SELECT RAISE(ABORT, 'invalid shift: bad date, time or empty role');
END;

CREATE TRIGGER IF NOT EXISTS availability_valid_insert BEFORE INSERT ON availability
WHEN NEW.start_time NOT GLOB '[0-2][0-9]:[0-5][0-9]' OR NEW.start_time > '23:59'
  OR NEW.end_time NOT GLOB '[0-2][0-9]:[0-5][0-9]' OR NEW.end_time > '23:59'
BEGIN
    SELECT RAISE(ABORT, 'invalid availability time');
END;

CREATE TRIGGER IF NOT EXISTS employees_valid_insert BEFORE INSERT ON employees
WHEN trim(NEW.name) = ''
BEGIN
    SELECT RAISE(ABORT, 'employee name is required');
END;

CREATE TRIGGER IF NOT EXISTS employees_valid_update BEFORE UPDATE ON employees
WHEN trim(NEW.name) = ''
BEGIN
    SELECT RAISE(ABORT, 'employee name is required');
END;

-- Sign-ins. Managers can do everything in their business; employee sign-ins are linked to one
-- employee record and are read-only. Deleting the employee deletes their sign-in. Usernames are
-- unique across ShiftSnap because people sign in with just a username (or email) and password.
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('manager', 'employee')),
    employee_id INTEGER UNIQUE REFERENCES employees(id) ON DELETE CASCADE,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    session_version INTEGER NOT NULL DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 0 CHECK (must_change_password IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login_at TEXT,
    CHECK (role = 'manager' OR employee_id IS NOT NULL)
);

-- Failed sign-ins (for throttling). account_id is set when the username belongs to a business.
CREATE TABLE IF NOT EXISTS login_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
    username TEXT NOT NULL COLLATE NOCASE,
    ip TEXT NOT NULL,
    failed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_failures ON login_failures(username, failed_at);

-- Sign-ups per IP address (to slow down automated account creation).
CREATE TABLE IF NOT EXISTS signup_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ip TEXT NOT NULL,
    attempted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signup_attempts ON signup_attempts(ip, attempted_at);

CREATE TABLE IF NOT EXISTS settings (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (account_id, key)
);

CREATE INDEX IF NOT EXISTS idx_employees_account ON employees(account_id);
CREATE INDEX IF NOT EXISTS idx_availability_account ON availability(account_id);
CREATE INDEX IF NOT EXISTS idx_shifts_account_date ON shifts(account_id, shift_date);
CREATE INDEX IF NOT EXISTS idx_users_account ON users(account_id);

-- Business isolation in the database itself: every row needs an account, a row can never move
-- to another account, and a shift / availability window / sign-in can only point at an employee
-- of the same business. (Triggers, so databases upgraded from older versions get them too.)
CREATE TRIGGER IF NOT EXISTS employees_account_insert BEFORE INSERT ON employees
WHEN NEW.account_id IS NULL
BEGIN
    SELECT RAISE(ABORT, 'employee must belong to a business');
END;

CREATE TRIGGER IF NOT EXISTS employees_account_update BEFORE UPDATE ON employees
WHEN NEW.account_id IS NOT OLD.account_id
BEGIN
    SELECT RAISE(ABORT, 'an employee cannot move to another business');
END;

CREATE TRIGGER IF NOT EXISTS shifts_account_insert BEFORE INSERT ON shifts
WHEN NEW.account_id IS NULL
  OR (NEW.employee_id IS NOT NULL
      AND (SELECT account_id FROM employees WHERE id = NEW.employee_id) IS NOT NEW.account_id)
BEGIN
    SELECT RAISE(ABORT, 'shift must belong to the same business as its employee');
END;

CREATE TRIGGER IF NOT EXISTS shifts_account_update BEFORE UPDATE ON shifts
WHEN NEW.account_id IS NOT OLD.account_id
  OR (NEW.employee_id IS NOT NULL
      AND (SELECT account_id FROM employees WHERE id = NEW.employee_id) IS NOT NEW.account_id)
BEGIN
    SELECT RAISE(ABORT, 'shift must belong to the same business as its employee');
END;

CREATE TRIGGER IF NOT EXISTS availability_account_insert BEFORE INSERT ON availability
WHEN NEW.account_id IS NULL
  OR (SELECT account_id FROM employees WHERE id = NEW.employee_id) IS NOT NEW.account_id
BEGIN
    SELECT RAISE(ABORT, 'availability must belong to the same business as its employee');
END;

CREATE TRIGGER IF NOT EXISTS users_account_insert BEFORE INSERT ON users
WHEN NEW.account_id IS NULL
  OR (NEW.employee_id IS NOT NULL
      AND (SELECT account_id FROM employees WHERE id = NEW.employee_id) IS NOT NEW.account_id)
BEGIN
    SELECT RAISE(ABORT, 'sign-in must belong to the same business as its employee');
END;

CREATE TRIGGER IF NOT EXISTS users_account_update BEFORE UPDATE ON users
WHEN NEW.account_id IS NOT OLD.account_id
  OR (NEW.employee_id IS NOT NULL
      AND (SELECT account_id FROM employees WHERE id = NEW.employee_id) IS NOT NEW.account_id)
BEGIN
    SELECT RAISE(ABORT, 'sign-in must belong to the same business as its employee');
END;
