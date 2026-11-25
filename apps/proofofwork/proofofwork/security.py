"""CSRF protection and HTTP security headers."""
import hmac
import logging
import re
import secrets

from flask import abort, current_app, g, request, session
from markupsafe import Markup

CSP = (
    # blob: is needed only to preview photos on the device before upload.
    "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)


def csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def csrf_field():
    return Markup('<input type="hidden" name="csrf_token" value="%s">') % csrf_token()


def check_csrf():
    if request.method in ("GET", "HEAD", "OPTIONS") or not current_app.config.get("CSRF_ENABLED", True):
        return
    expected = session.get("_csrf_token")
    supplied = request.form.get("csrf_token", "")
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        abort(400, description="This form has expired or is invalid. Go back, reload the page and try again.")


def set_security_headers(response):
    headers = response.headers
    headers.setdefault("Content-Security-Policy", CSP)
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("X-Frame-Options", "DENY")
    # no-referrer keeps share tokens out of Referer headers sent to other sites.
    headers.setdefault("Referrer-Policy", "no-referrer")
    headers.setdefault("Permissions-Policy", "geolocation=(), microphone=()")
    if request.endpoint != "static" and (g.get("user") is not None or request.path.startswith("/r/")):
        headers["Cache-Control"] = "no-store"
    if request.path.startswith("/r/"):
        headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


SHARE_PATH_RE = re.compile(r"(/r/)[A-Za-z0-9_-]{16,}")


def redact_share_tokens(text):
    return SHARE_PATH_RE.sub(r"\1[redacted]", text)


class ShareTokenRedactingFilter(logging.Filter):
    """Share tokens are secrets: strip them from every log line (access logs, error logs)."""

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:  # never let logging break a request
            return True
        redacted = redact_share_tokens(message)
        if redacted != message:
            record.msg, record.args = redacted, ()
        if record.exc_text:
            record.exc_text = redact_share_tokens(record.exc_text)
        return True


# werkzeug = `flask run` access log; waitress/gunicorn = production servers.
REDACTED_LOGGERS = ("werkzeug", "waitress", "waitress.queue", "gunicorn.access", "gunicorn.error")


def install_log_redaction(app):
    for logger in [app.logger] + [logging.getLogger(name) for name in REDACTED_LOGGERS]:
        if not any(isinstance(f, ShareTokenRedactingFilter) for f in logger.filters):
            logger.addFilter(ShareTokenRedactingFilter())


def init_app(app):
    install_log_redaction(app)
    app.before_request(check_csrf)
    app.after_request(set_security_headers)
    app.jinja_env.globals["csrf_field"] = csrf_field
