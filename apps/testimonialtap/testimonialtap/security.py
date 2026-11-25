"""CSRF protection, account authentication, rate limiting and public tokens."""
import hmac
import secrets
import string
import threading
import time
from collections import defaultdict, deque
from functools import wraps

from flask import abort, current_app, g, redirect, request, session, url_for

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# The public submission form is unauthenticated, so there is no session to forge
# requests for. It is protected by a signed, single-use, project-bound form stamp
# instead, which keeps it working when a visitor's browser drops cookies.
CSRF_EXEMPT_ENDPOINTS = {"public.submit"}
_TOKEN_ALPHABET = string.ascii_letters + string.digits


def new_public_token():
    """32 alphanumeric characters (~190 bits).

    Letters and digits only, so links survive email clients, chat apps and
    markdown autolinkers that strip trailing '-' or '_' or treat '_' as italics.
    """
    return "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(32))


def csrf_token():
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


def check_csrf():
    if request.method not in UNSAFE_METHODS or not current_app.config.get("CSRF_ENABLED", True):
        return
    if request.endpoint in CSRF_EXEMPT_ENDPOINTS:
        return
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
    expected = session.get("_csrf") or ""
    if not expected or not sent or not hmac.compare_digest(expected, sent):
        abort(400, description="Your form's security token was missing or expired. "
                               "Please reload the page and try again.")


def load_current_account():
    """before_request: g.account is the logged-in account row, or None."""
    from .db import get_db
    g.account = None
    account_id = session.get("account_id")
    if account_id is not None:
        g.account = get_db().execute(
            "SELECT id, business_name, email, website, is_demo, created_at, paid_until "
            "FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if g.account is None:
            session.clear()


def is_owner():
    """True when someone is logged in to an account (they own that account's data)."""
    return g.get("account") is not None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_owner():
            # script_root keeps the mount prefix (e.g. /testimonialtap) so login returns to the right page.
            return redirect(url_for("admin.login",
                                    next=request.script_root + request.full_path.rstrip("?")))
        return view(*args, **kwargs)
    return wrapped


def safe_next(target, fallback):
    """Only allow same-site relative redirects (prevents open redirects)."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return fallback


def client_ip():
    return request.remote_addr or "unknown"


class RateLimiter:
    """Small in-memory sliding-window limiter.

    Suitable for a single-process deployment. For multiple workers or hosts,
    put a shared limiter (reverse proxy, Redis, WAF) in front of the app.
    """

    def __init__(self):
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key, limit, window_seconds):
        now = time.monotonic()
        with self._lock:
            if len(self._hits) > 10_000:
                self._prune(now, window_seconds)
            q = self._hits[key]
            while q and q[0] <= now - window_seconds:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True

    def _prune(self, now, window_seconds):
        for key in [k for k, q in self._hits.items() if not q or q[-1] <= now - window_seconds]:
            del self._hits[key]

    def reset(self):
        with self._lock:
            self._hits.clear()
