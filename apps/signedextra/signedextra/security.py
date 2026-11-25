"""Authentication, authorization, CSRF protection, throttling and audit logging."""
import hmac
import json
import secrets
from datetime import timedelta
from functools import wraps

from flask import abort, g, has_request_context, redirect, request, session, url_for

from .db import get_db
from .util import now_iso, sha256_hex, utcnow

ROLES = ("owner", "office", "foreman")
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_MINUTES = 15


# --- tokens -----------------------------------------------------------------

def new_token():
    """Return (raw_token, token_hash). Only the hash is stored."""
    raw = secrets.token_urlsafe(32)
    return raw, sha256_hex(raw)


def hash_token(raw):
    return sha256_hex(raw or "")


# --- CSRF -------------------------------------------------------------------

def csrf_token():
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


def check_csrf():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token") or ""
    expected = session.get("_csrf") or ""
    if not expected or not hmac.compare_digest(sent, expected):
        abort(400, description="Your form expired. Go back, refresh the page and try again.")


# --- session / current user -------------------------------------------------

def load_current_user():
    g.user = None
    g.company = None
    user_id = session.get("user_id")
    if not user_id:
        return
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user or not user["active"] or user["session_epoch"] != session.get("epoch"):
        session.clear()
        return
    g.user = user
    g.company = db.execute("SELECT * FROM companies WHERE id = ?", (user["company_id"],)).fetchone()


def login_user(user):
    session.clear()
    session.permanent = True
    session["user_id"] = user["id"]
    session["epoch"] = user["session_epoch"]
    csrf_token()


def logout_user():
    session.clear()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            # script_root keeps the URL prefix (e.g. /signedextra) so the redirect after login works.
            return redirect(url_for("auth.login", next=request.script_root + request.full_path.rstrip("?")))
        return view(*args, **kwargs)
    return wrapped


def roles_required(*roles):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if g.user["role"] not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def is_manager():
    return g.user is not None and g.user["role"] in ("owner", "office")


def can_see_prices():
    if g.user is None:
        return False
    return is_manager() or bool(g.company["foreman_sees_prices"])


# --- login throttling -------------------------------------------------------

def _attempt_key(email):
    return f"{(email or '').lower()}|{request.remote_addr or ''}"


def login_blocked(email):
    db = get_db()
    since = (utcnow() - timedelta(minutes=LOGIN_WINDOW_MINUTES)).isoformat()
    count = db.execute(
        "SELECT COUNT(*) FROM login_attempts WHERE key = ? AND created_at >= ?",
        (_attempt_key(email), since),
    ).fetchone()[0]
    return count >= LOGIN_MAX_FAILURES


def record_login_failure(email):
    db = get_db()
    # Old attempts no longer count toward a lockout; drop them so the table stays small.
    since = (utcnow() - timedelta(minutes=LOGIN_WINDOW_MINUTES)).isoformat()
    db.execute("DELETE FROM login_attempts WHERE created_at < ?", (since,))
    db.execute("INSERT INTO login_attempts (key, created_at) VALUES (?, ?)",
               (_attempt_key(email), now_iso()))
    db.commit()


def clear_login_failures(email):
    db = get_db()
    db.execute("DELETE FROM login_attempts WHERE key = ?", (_attempt_key(email),))
    db.commit()


# --- audit ------------------------------------------------------------------

def audit(company_id, action, ticket_id=None, details=None, actor=None, user_id=None):
    """Append an audit event. Caller is responsible for committing."""
    if user_id is None and g.get("user") is not None:
        user_id = g.user["id"]
    if actor is None:
        actor = g.user["name"] if g.get("user") is not None else "system"
    get_db().execute(
        "INSERT INTO audit_events (company_id, ticket_id, user_id, actor, action, details,"
        " ip_address, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (company_id, ticket_id, user_id, actor, action,
         json.dumps(details or {}, sort_keys=True),
         (request.remote_addr or "") if has_request_context() else "", now_iso()),
    )
