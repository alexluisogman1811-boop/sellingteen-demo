"""ShiftSnap - a weekly shift scheduler for small businesses (Flask + SQLite).

Any business can sign up online; each business only ever sees its own employees and shifts.
Managers manage employees and shifts; employees sign in to see the schedule.
"""
import logging
import os
import secrets
from datetime import timedelta

from flask import Flask, render_template
from werkzeug.exceptions import HTTPException

from . import csrf, db, mailer

FRIENDLY_ERRORS = {
    401: "Please sign in to continue.",
    403: "You don't have permission to do that. Only managers can change employees and shifts.",
}


def _env(*names: str, default: str = "") -> str:
    """First non-empty environment variable among `names` (ShiftSnap's own name first, then the
    generic name the shared hosting package uses)."""
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


def _load_secret_key(app: Flask) -> str:
    env = _env("SHIFTSNAP_SECRET_KEY", "SECRET_KEY")
    if env:
        return env
    path = os.path.join(app.instance_path, "secret_key")
    if not os.path.exists(path):
        # 0o600: readable by the account running ShiftSnap only (ignored on Windows).
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(secrets.token_hex(32))
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


def _env_flag(*names: str, default: bool = False) -> bool:
    value = _env(*names)
    if not value:
        return default
    return value.lower() in ("1", "true", "yes", "on")


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)
    app.config.from_mapping(
        DATABASE=_env("SHIFTSNAP_DATABASE", "DATABASE_PATH",
                      default=os.path.join(app.instance_path, "shiftsnap.sqlite3")),
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_HTTPONLY=True,
        # Set SHIFTSNAP_SECURE_COOKIES=1 (or SESSION_COOKIE_SECURE=1) when serving over HTTPS.
        SESSION_COOKIE_SECURE=_env_flag("SHIFTSNAP_SECURE_COOKIES", "SESSION_COOKIE_SECURE"),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        MAX_CONTENT_LENGTH=256 * 1024,
        FIXED_TODAY=None,
        PASSWORD_HASH_METHOD=None,  # Werkzeug default (scrypt); tests use a faster method
        # Free trial and subscription (see billing.py). BILLING_ENABLED=0 turns it off.
        BILLING_ENABLED=_env_flag("BILLING_ENABLED", default=True),
        TRIAL_DAYS=int(_env("TRIAL_DAYS", default="30") or 30),
        PAYMENT_LINK=_env("PAYMENT_LINK"),
        PRICE_TEXT=_env("PRICE_TEXT", default="$19 per month per location"),
        SUPPORT_EMAIL=_env("SUPPORT_EMAIL"),
        LEGAL_NAME=_env("LEGAL_NAME", default="sellingteen"),
        # Optional email, OFF unless SMTP_HOST and SMTP_FROM are set (see mailer.py). Only used
        # for "forgot password" links, never for marketing.
        MAIL=mailer.mail_settings(),
    )
    if test_config:
        app.config.update(test_config)
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = _load_secret_key(app)
    if app.config.get("TRUST_PROXY", _env_flag("SHIFTSNAP_TRUST_PROXY", "TRUST_PROXY")):
        # Behind exactly one reverse proxy (e.g. Caddy/nginx doing HTTPS): use the client IP and
        # scheme it forwards, so login throttling is per real client. Never enable without a proxy,
        # or clients could spoof X-Forwarded-For.
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)
    csrf.init_app(app)

    from . import auth, billing, legal, views
    auth.init_app(app)
    app.before_request(billing.check_access)  # after sign-in is known (auth hooks run first)
    app.register_blueprint(auth.bp)
    app.register_blueprint(views.bp)
    app.register_blueprint(billing.bp)
    app.register_blueprint(legal.bp)
    billing.register_cli(app)
    views.register_template_helpers(app)

    @app.get("/healthz")
    def healthz():
        # For the hosting platform's health check: confirms the app and database respond.
        db.get_db().execute("SELECT 1").fetchone()
        return {"status": "ok"}

    @app.after_request
    def security_headers(resp):
        # All scripts/styles are same-origin files; nothing inline, nothing third-party.
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        # Pages can contain private employee data: don't let shared browsers/proxies cache them.
        if resp.mimetype in ("text/html", "text/csv"):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    @app.errorhandler(HTTPException)
    def handle_http_error(err):
        return render_template("error.html", code=err.code, name=err.name,
                               description=FRIENDLY_ERRORS.get(err.code, err.description)), err.code

    @app.errorhandler(Exception)
    def handle_unexpected(err):
        logging.getLogger(__name__).exception("Unhandled error")
        return render_template("error.html", code=500, name="Something went wrong",
                               description="An unexpected error occurred. If you were saving "
                                           "something, check whether the change was applied."), 500

    return app
