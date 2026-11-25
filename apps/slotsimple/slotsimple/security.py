"""CSRF protection, admin authentication, simple rate limiting and headers."""
import functools
import hmac
import secrets
import threading
import time

from flask import abort, current_app, g, redirect, request, session, url_for

from .db import get_db

CSRF_FIELD = "csrf_token"


def csrf_token():
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


def check_csrf():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    expected = session.get("_csrf")
    supplied = request.form.get(CSRF_FIELD) or request.headers.get("X-CSRF-Token", "")
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        abort(400, description="Your form session expired or was invalid. Please go back, reload and try again.")


def load_account():
    """g.account = the logged-in business owner's account, or None."""
    g.account = None
    account_id = session.get("account_id")
    if account_id is not None:
        row = get_db().execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        # Invalidate sessions after a password change.
        if row and hmac.compare_digest(session.get("account_pw", ""), row["password_hash"][-16:]):
            g.account = row
        else:
            session.pop("account_id", None)


def login_account(row):
    csrf = secrets.token_urlsafe(32)
    session.clear()  # prevent session fixation
    session.permanent = True
    session["_csrf"] = csrf
    session["account_id"] = row["id"]
    session["account_pw"] = row["password_hash"][-16:]


def current_account_id():
    """The logged-in account's id. Owner pages use ONLY this to scope queries."""
    return g.account["id"]


def admin_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.get("account") is None:
            # script_root keeps the app's URL prefix (e.g. /slotsimple) in the return address.
            target = request.script_root + request.full_path
            return redirect(url_for("admin.login", next=target[:-1] if target.endswith("?") else target))
        return view(*args, **kwargs)
    return wrapped


def is_safe_local_path(target):
    # Reject "//host" and "/\host": browsers treat both as links to another site.
    return bool(target) and target.startswith("/") and not target.startswith(("//", "/\\")) \
        and "\\" not in target


class RateLimiter:
    """In-memory sliding-window limiter (per process). Good enough for a single
    instance MVP; use a shared store (e.g. Redis) when running several workers."""

    def __init__(self):
        self._hits = {}
        self._lock = threading.Lock()

    def hit(self, key, limit, window_seconds):
        """Record a hit and return True if the key is still within its limit."""
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < window_seconds]
            allowed = len(hits) < limit
            if allowed:
                hits.append(now)
            self._hits[key] = hits
            if len(self._hits) > 10000:
                self._hits = {k: v for k, v in self._hits.items() if v}
            return allowed

    def reset(self, key):
        with self._lock:
            self._hits.pop(key, None)


limiter = RateLimiter()


def rate_limit(bucket, limit, window_seconds):
    if not current_app.config.get("RATE_LIMITS", True):
        return True
    return limiter.hit(f"{bucket}:{request.remote_addr}", limit, window_seconds)


def set_security_headers(response):
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'self'",
    )
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if request.endpoint and (
        request.endpoint.startswith(("admin.", "billing.", "public.manage"))
        or request.endpoint == "public.confirmation"
    ):
        response.headers["Cache-Control"] = "no-store"
    return response
