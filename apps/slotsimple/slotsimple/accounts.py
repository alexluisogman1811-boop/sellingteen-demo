"""Business accounts: one login per business, all its data keyed by accounts.id."""
import re
import sqlite3

from werkzeug.security import generate_password_hash

from .db import seed_account_defaults, transaction, unique_slug
from .timeutil import to_iso

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,38}[a-z0-9])$")
MIN_PASSWORD = 8


class AccountError(ValueError):
    pass


def normalize_email(email):
    return (email or "").strip().lower()


def get_account_by_slug(db, slug):
    return db.execute("SELECT * FROM accounts WHERE slug = ?", (slug,)).fetchone()


def get_account_by_email(db, email):
    return db.execute("SELECT * FROM accounts WHERE email = ?", (normalize_email(email),)).fetchone()


def create_account(db, business_name, email, password, now, is_demo=False, slug=None):
    """Create an account with default settings and opening hours. Returns the row."""
    email = normalize_email(email)
    if len(password) < MIN_PASSWORD:
        raise AccountError(f"Password must be at least {MIN_PASSWORD} characters.")
    with transaction(db):
        if db.execute("SELECT 1 FROM accounts WHERE email = ?", (email,)).fetchone():
            raise AccountError("An account with this email already exists. Try logging in instead.")
        try:
            cur = db.execute(
                "INSERT INTO accounts(slug, email, password_hash, is_demo, created_at) VALUES (?, ?, ?, ?, ?)",
                (unique_slug(db, slug or business_name), email, generate_password_hash(password),
                 int(is_demo), to_iso(now)),
            )
        except sqlite3.IntegrityError:
            raise AccountError("An account with this email already exists. Try logging in instead.") from None
        seed_account_defaults(db, cur.lastrowid, business_name)
    return db.execute("SELECT * FROM accounts WHERE id = ?", (cur.lastrowid,)).fetchone()


def set_password(db, account_id, password):
    if len(password) < MIN_PASSWORD:
        raise AccountError(f"Password must be at least {MIN_PASSWORD} characters.")
    with transaction(db):
        db.execute("UPDATE accounts SET password_hash = ? WHERE id = ?",
                   (generate_password_hash(password), account_id))


def change_slug(db, account_id, slug):
    slug = (slug or "").strip().lower()
    if not SLUG_RE.match(slug) or not re.search(r"[a-z]", slug):
        raise AccountError("Use 3-40 lowercase letters, numbers and dashes (it must include a letter).")
    with transaction(db):
        if db.execute("SELECT 1 FROM accounts WHERE slug = ? AND id != ?", (slug, account_id)).fetchone():
            raise AccountError("That booking page address is taken. Try another.")
        db.execute("UPDATE accounts SET slug = ? WHERE id = ?", (slug, account_id))
    return slug
