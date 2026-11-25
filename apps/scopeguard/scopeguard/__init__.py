"""ScopeGuard: document agreed scope, revisions, change requests and client approvals."""
import hmac
import os
import secrets
from datetime import datetime

from flask import Flask, abort, g, render_template, request, session

from . import db as dbmod
from . import mailer
from .services import CR_LABELS, DECISION_LABELS, METHOD_LABELS, METHOD_SHORT


def _env(*names, default=None):
    """First setting found among names. The generic names (SECRET_KEY, DATABASE_PATH, ...) are what the
    shared hosting package sets; the SCOPEGUARD_* names still work for existing installs."""
    for name in names:
        value = os.environ.get(name)
        if value not in (None, ""):
            return value
    return default


def _flag(*names, default="0"):
    return str(_env(*names, default=default)).strip().lower() in ("1", "true", "yes", "on")


def _load_secret_key(instance_path):
    env = _env("SECRET_KEY", "SCOPEGUARD_SECRET_KEY")
    if env:
        return env
    path = os.path.join(instance_path, "secret_key")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(secrets.token_hex(32))
    with open(path, encoding="utf-8") as fh:
        return fh.read().strip()


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)
    database = _env("DATABASE_PATH", "SCOPEGUARD_DB", default=os.path.join(app.instance_path, "scopeguard.db"))
    app.config.update(
        DATABASE=database,
        # Backups (before upgrades and resets) sit next to the database, outside the code folder.
        BACKUP_DIR=os.path.join(os.path.dirname(os.path.abspath(database)), "backups"),
        DEMO_MODE=_flag("SCOPEGUARD_DEMO", "DEMO_MODE"),
        # Public address clients use to reach this app, e.g. https://scope.example.com (with or without
        # the /scopeguard prefix). Empty = use the address of the current request.
        BASE_URL=_env("SCOPEGUARD_BASE_URL", "APP_BASE_URL", default="").rstrip("/"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=_flag("SESSION_COOKIE_SECURE", "SCOPEGUARD_SECURE_COOKIES"),
        MAX_CONTENT_LENGTH=1024 * 1024,
        # Free trial and subscription (see billing.py)
        BILLING_ENABLED=_flag("BILLING_ENABLED", default="1"),
        TRIAL_DAYS=int(_env("TRIAL_DAYS", default="30")),
        PAYMENT_LINK=_env("PAYMENT_LINK", default=""),
        PRICE_TEXT=_env("PRICE_TEXT", default="$12 per month"),
        SUPPORT_EMAIL=_env("SUPPORT_EMAIL", default=""),
        LEGAL_NAME=_env("LEGAL_NAME", default="ScopeGuard"),
        # Optional email, used only for "forgot password" links. OFF unless SMTP_HOST and SMTP_FROM are set.
        MAIL=mailer.mail_settings(),
    )
    if test_config:
        app.config.update(test_config)
    # Behind a reverse proxy, trust its X-Forwarded-For/-Proto so approval records keep the client's real IP.
    # Only enable when a proxy you control sits in front; otherwise clients could spoof their recorded IP.
    proxies = int(_env("SCOPEGUARD_TRUSTED_PROXIES", "TRUST_PROXY", default=app.config.get("TRUSTED_PROXIES", 0)) or 0)
    if proxies:
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=proxies, x_proto=proxies, x_host=proxies)
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = _load_secret_key(app.instance_path)

    app.teardown_appcontext(dbmod.close_db)
    with app.app_context():
        dbmod.init_db(backup_dir=app.config["BACKUP_DIR"])

    # ------------------------------------------------------------ CSRF

    def csrf_token():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return session["csrf_token"]

    @app.before_request
    def csrf_protect():
        if request.blueprint == "portal":
            return  # client pages use a cookie-free, link-bound form token (see portal.py)
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            sent = request.form.get("csrf_token", "")
            expected = session.get("csrf_token", "")
            if not expected or not hmac.compare_digest(sent, expected):
                abort(400, "The form expired or was not submitted from ScopeGuard. Reload the page and try again.")

    @app.after_request
    def security_headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"  # review tokens live in URLs
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "form-action 'self' mailto:; frame-ancestors 'none'; base-uri 'none'"
        )
        if request.endpoint and (request.endpoint.startswith("portal.") or g.get("user")):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ------------------------------------------------------------ template helpers

    def fmt_ts(value):
        if not value:
            return ""
        try:
            return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M UTC")
        except ValueError:
            return value

    app.jinja_env.globals.update(
        csrf_token=csrf_token,
        METHOD_LABELS=METHOD_LABELS,
        METHOD_SHORT=METHOD_SHORT,
        DECISION_LABELS=DECISION_LABELS,
        CR_LABELS=CR_LABELS,
    )
    app.jinja_env.filters["ts"] = fmt_ts

    @app.context_processor
    def inject_demo():
        return {"demo_mode": app.config["DEMO_MODE"]}

    # ------------------------------------------------------------ blueprints & errors

    from . import billing
    from .auth import bp as auth_bp
    from .auth import register_cli as auth_cli
    from .legal import bp as legal_bp
    from .portal import bp as portal_bp
    from .public import bp as public_bp
    from .views import bp as main_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(public_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(portal_bp)
    app.register_blueprint(billing.bp)
    app.register_blueprint(legal_bp)
    # Runs after auth's load_account (registered first): accounts whose trial ended go to Subscribe.
    app.before_request(billing.check_access)
    billing.register_cli(app)
    auth_cli(app)

    @app.get("/healthz")
    def healthz():
        # For the hosting platform's health check: confirms the app and its database respond.
        dbmod.get_db().execute("SELECT 1").fetchone()
        return {"status": "ok"}

    for code in (400, 403, 404, 405, 413, 429):
        app.register_error_handler(
            code,
            lambda e, code=code: (render_template("error.html", code=code, error=e), code),
        )

    from .demo import register_cli

    register_cli(app)
    return app
