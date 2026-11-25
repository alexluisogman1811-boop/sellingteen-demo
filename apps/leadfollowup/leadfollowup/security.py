"""Session auth, CSRF protection and response hardening."""
import functools
import secrets

from flask import abort, g, redirect, request, session, url_for

from .db import get_db

CSRF_SESSION_KEY = "_csrf"


def safe_next(target, default):
    """Only allow same-site relative paths, to prevent open redirects."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return default


def wants_json() -> bool:
    return (
        request.is_json
        or request.accept_mimetypes.best == "application/json"
        or request.path.endswith("/generate")
    )


def csrf_token() -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def _check_csrf():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    expected = session.get(CSRF_SESSION_KEY)
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    if not expected or not sent or not secrets.compare_digest(expected, sent):
        abort(400, description="Your session expired. Please reload the page and try again.")


def _load_user():
    g.user = None
    user_id = session.get("user_id")
    if user_id is not None:
        g.user = get_db().execute(
            "SELECT id, email, business_name, is_demo FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if g.user is None:
            session.clear()


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            if wants_json():
                abort(401)
            # script_root keeps the address prefix when the app runs under one (e.g. /leadfollowup)
            return redirect(url_for("auth.login", next=(request.script_root + request.full_path).rstrip("?")))
        return view(*args, **kwargs)

    return wrapped


def _security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "frame-ancestors 'none'; form-action 'self'; base-uri 'self'",
    )
    if g.get("user") is not None:
        # Private pages must not be cached (e.g. after logout on a shared device).
        response.headers["Cache-Control"] = "no-store"
    return response


def init_app(app):
    app.before_request(_load_user)
    if app.config.get("CSRF_ENABLED", True):
        app.before_request(_check_csrf)
    app.after_request(_security_headers)
    app.jinja_env.globals["csrf_token"] = csrf_token
