"""Login state, CSRF protection, safe redirects and response security headers."""
import secrets
from functools import wraps

from flask import abort, current_app, g, redirect, request, session, url_for

from .db import get_db

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Logging out is harmless to forge, and must still work from a tab left open for hours.
CSRF_EXEMPT = {"auth.logout"}

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'"
)


def get_csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def check_csrf():
    if request.method not in UNSAFE_METHODS or not current_app.config.get("CSRF_ENABLED", True):
        return
    if request.endpoint in CSRF_EXEMPT:
        return
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
    expected = session.get("_csrf_token") or ""
    if not expected or not sent or not secrets.compare_digest(sent.encode(), expected.encode()):
        abort(400, description=(
            "This form could not be verified (CSRF check failed). "
            "Your session may have expired: go back, reload the page and try again."
        ))


def load_current_account():
    """Sets g.account to the logged-in shop (or None) for every request."""
    g.account = None
    account_id = session.get("account_id")
    if account_id is None:
        return
    g.account = get_db().execute(
        "SELECT id, shop_name, email, is_demo, created_at, paid_until FROM accounts WHERE id = ?",
        (account_id,)).fetchone()
    if g.account is None:
        session.clear()


def is_safe_next(target):
    """Only paths inside this app (including its URL prefix, e.g. /stockping/...),
    never another site: rejects '//host', '/\\host', backslashes and absolute URLs."""
    if not target or not target.startswith("/") or target.startswith(("//", "/\\")) or "\\" in target:
        return False
    root = request.script_root
    return not root or target == root or target.startswith(root + "/")


def safe_next(target):
    return target if is_safe_next(target) else url_for("main.dashboard")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.get("account") is None:
            # script_root keeps the address prefix when the app runs under one (e.g. /stockping)
            return redirect(url_for("auth.login",
                                    next=(request.script_root + request.full_path).rstrip("?")))
        return view(*args, **kwargs)
    return wrapped


def add_security_headers(response):
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if request.endpoint != "static":
        # Stock data must not be shown from the browser cache after logging out.
        response.headers["Cache-Control"] = "no-store"
    return response


def init_app(app):
    app.before_request(load_current_account)
    app.before_request(check_csrf)
    app.after_request(add_security_headers)
    app.jinja_env.globals["csrf_token"] = get_csrf_token
