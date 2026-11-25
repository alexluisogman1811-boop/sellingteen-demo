"""Simple per-IP rate limits stored in SQLite (no extra packages).

Limits are set in config as (max_events, window_seconds):
    RATE_LIMIT_SIGN       requests to public signing pages (sign / decline)
    RATE_LIMIT_SIGN_BAD   invalid or expired signing-link lookups (blocks link guessing)
    RATE_LIMIT_SIGNUP     company sign-ups
"""
from datetime import timedelta

from flask import abort, current_app, request

from .db import get_db
from .util import now_iso, utcnow


def _client_key():
    return request.remote_addr or "unknown"


def _window(bucket):
    limit, seconds = current_app.config[bucket]
    since = (utcnow() - timedelta(seconds=seconds)).isoformat()
    return limit, since


def count(bucket, key=None):
    limit, since = _window(bucket)
    n = get_db().execute("SELECT COUNT(*) FROM rate_events WHERE bucket = ? AND key = ? AND created_at >= ?",
                         (bucket, key or _client_key(), since)).fetchone()[0]
    return n, limit


def record(bucket, key=None):
    db = get_db()
    _limit, since = _window(bucket)
    db.execute("DELETE FROM rate_events WHERE bucket = ? AND created_at < ?", (bucket, since))
    db.execute("INSERT INTO rate_events (bucket, key, created_at) VALUES (?, ?, ?)",
               (bucket, key or _client_key(), now_iso()))
    db.commit()


def blocked(bucket, key=None):
    n, limit = count(bucket, key)
    return n >= limit


def limit_or_abort(bucket, message="Too many requests from your network. Please wait a few minutes and try again."):
    """Counts this request; aborts with 429 once the limit for this IP is reached."""
    if blocked(bucket):
        abort(429, description=message)
    record(bucket)
