"""Sessions, CSRF, authorization helpers, security headers and log redaction."""
import functools
import hashlib
import hmac
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from flask import abort, current_app, g, redirect, request, session, url_for

from .db import get_db, iso, now_iso

# ---------------------------------------------------------------- log redaction

_REDACTIONS = [
    # Share tokens are 43 chars; anything token-length after /report/ is hidden (but not /report/generate).
    (re.compile(r"(/report/)[A-Za-z0-9_\-]{20,}"), r"\1[REDACTED]"),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]+"), "[REDACTED_KEY]"),
    (re.compile(r"(?i)((?:token|api_key|apikey|password|key)=)[^&\s\"']+"), r"\1[REDACTED]"),
]


def redact(text):
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed log call
            return True
        record.msg = redact(message)
        record.args = ()
        return True


def install_log_redaction(app):
    redactor = RedactingFilter()
    for name in ("werkzeug", "waitress", "jobproof", app.logger.name):
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactingFilter) for f in logger.filters):
            logger.addFilter(redactor)
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(redactor)


# ---------------------------------------------------------------- sessions

def _hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def start_session(user_id):
    """Create a fresh server-side session (prevents fixation; revocable on logout)."""
    session.clear()
    token = secrets.token_urlsafe(32)
    lifetime = current_app.config["PERMANENT_SESSION_LIFETIME"]
    expires = iso(datetime.now(timezone.utc) + lifetime)
    db = get_db()
    db.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso(),))  # housekeeping
    db.execute(
        "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (_hash_token(token), user_id, now_iso(), expires),
    )
    db.commit()
    session.permanent = True
    session["sid"] = token
    session["csrf"] = secrets.token_urlsafe(32)


def end_session():
    token = session.get("sid")
    if token:
        db = get_db()
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))
        db.commit()
    session.clear()


def load_current_user():
    g.user = None
    g.business = None
    token = session.get("sid")
    if not token:
        return
    db = get_db()
    row = db.execute(
        "SELECT u.id, u.email, s.expires_at FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ?",
        (_hash_token(token),),
    ).fetchone()
    if row is None or row["expires_at"] < now_iso():
        session.pop("sid", None)
        return
    g.user = row
    g.business = db.execute("SELECT * FROM businesses WHERE user_id = ?", (row["id"],)).fetchone()


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.get("user") is None:
            # script_root keeps the address prefix when the app runs under one (e.g. /jobproof)
            target = request.script_root + (request.full_path if request.query_string else request.path)
            return redirect(url_for("auth.login", next=target))
        return view(*args, **kwargs)
    return wrapped


# ---------------------------------------------------------------- ownership

def get_owned_job(job_id):
    """Return a job only if it belongs to the signed-in user's business; 404 otherwise."""
    job = get_db().execute(
        "SELECT * FROM jobs WHERE id = ? AND business_id = ?", (job_id, g.business["id"])
    ).fetchone()
    if job is None:
        abort(404)
    return job


def get_owned_photo(file_key):
    if not re.fullmatch(r"[a-f0-9]{32}", file_key or ""):
        abort(404)
    photo = get_db().execute(
        "SELECT p.* FROM photos p JOIN jobs j ON j.id = p.job_id "
        "WHERE p.file_key = ? AND j.business_id = ?",
        (file_key, g.business["id"]),
    ).fetchone()
    if photo is None:
        abort(404)
    return photo


# ---------------------------------------------------------------- CSRF

def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def _check_csrf():
    if not current_app.config.get("CSRF_ENABLED", True):
        return
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
    expected = session.get("csrf") or ""
    if not expected or not hmac.compare_digest(sent, expected):
        abort(400)


# ---------------------------------------------------------------- wiring

def register_security(app):
    @app.before_request
    def _before():
        load_current_user()
        _check_csrf()

    @app.after_request
    def _headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if current_app.config.get("SESSION_COOKIE_SECURE"):
            # Only sent when serving over HTTPS (JOBPROOF_SECURE_COOKIES=1); tells browsers to stay on HTTPS.
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        # no-referrer keeps share tokens from leaking via the Referer header
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; "
            "object-src 'none'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
        )
        if g.get("user") is not None:
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    app.jinja_env.globals["csrf_token"] = csrf_token
