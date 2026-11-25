import hmac
import secrets
from functools import wraps

from flask import abort, g, redirect, request, session, url_for

from .db import get_db

# Largest id SQLite can store; bigger ids from the URL can't exist and would crash the query.
MAX_ID = 2**63 - 1

# Logging out is harmless to forge, and must still work from a stale tab.
CSRF_EXEMPT = {"auth.logout"}


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


def check_csrf():
    if request.method != "POST" or request.endpoint in CSRF_EXEMPT:
        return
    sent = request.form.get("_csrf", "")
    expected = session.get("_csrf", "")
    if not sent or not expected or not hmac.compare_digest(sent, expected):
        abort(400, description="This page was open for too long. Please refresh the page and try again.")


def is_safe_local_path(target):
    # Reject "//host" and "/\host": browsers treat both as links to another site.
    return bool(target) and target.startswith("/") and not target.startswith(("//", "/\\")) \
        and "\\" not in target


def load_current_business():
    business_id = session.get("business_id")
    g.business = None
    if business_id is not None:
        g.business = get_db().execute(
            "SELECT id, name, email, review_link, is_demo FROM businesses WHERE id = ?", (business_id,)
        ).fetchone()
        if g.business is None:
            session.clear()


def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if request.endpoint != "static":
        # Customer data must not be shown from cache after logging out (e.g. via the Back button).
        response.headers["Cache-Control"] = "no-store"
    return response


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.business is None:
            # script_root keeps the mount prefix (e.g. /reviewboost) so login returns to the right page.
            # full_path keeps the query string too (Flask adds a lone "?" when there is none).
            return redirect(url_for("auth.login", next=request.script_root + request.full_path.rstrip("?")))
        return view(*args, **kwargs)

    return wrapped
