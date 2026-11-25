"""Minimal session-based CSRF protection (synchronizer token pattern).

Every state-changing request must include a `csrf_token` form field matching
the token stored in the signed session cookie.
"""
import hmac
import secrets

from flask import abort, request, session

SESSION_KEY = "_csrf_token"


def generate_csrf_token() -> str:
    token = session.get(SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[SESSION_KEY] = token
    return token


def init_app(app):
    app.jinja_env.globals["csrf_token"] = generate_csrf_token

    @app.before_request
    def csrf_protect():
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            expected = session.get(SESSION_KEY)
            sent = request.form.get("csrf_token", "")
            if not expected or not hmac.compare_digest(sent, expected):
                abort(400, description="Your form session expired or the security token was "
                                       "missing. Go back, reload the page and try again.")
