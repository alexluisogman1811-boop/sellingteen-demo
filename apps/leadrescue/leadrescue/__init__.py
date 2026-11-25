import logging
import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from flask import Flask, render_template
from flask_wtf.csrf import CSRFError
from werkzeug.exceptions import SecurityError
from werkzeug.routing import IntegerConverter

from .config import CONFIGS, WEAK_SECRETS
from .extensions import csrf, db, login_manager, migrate
from .logging_filters import RedactSensitiveFilter

MAX_DB_INT = 2**31 - 1  # PostgreSQL INTEGER
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


class BoundedIntConverter(IntegerConverter):
    """<int:...> that rejects ids too big for the database (404 instead of a 500)."""

    def __init__(self, map, *args, **kwargs):
        kwargs.setdefault("max", MAX_DB_INT)
        super().__init__(map, *args, **kwargs)


def normalize_database_url(url: str) -> str:
    """Hosts like Render and Heroku hand out postgres:// URLs; SQLAlchemy needs
    an explicit driver, so use psycopg 3."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def _configure_logging(app: Flask) -> None:
    """Plain, timestamped logs on stdout (what Render and most hosts collect).

    Only touches the root logger when nothing has configured it yet, so it never
    changes logging for other apps sharing the same process (sellingteen hosting)."""
    if app.config.get("TESTING"):
        return
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
        root.setLevel(logging.INFO)


def _auto_migrate(app: Flask) -> None:
    """AUTO_MIGRATE=1: create or upgrade the tables on start-up (e.g. a fresh SQLite file).

    A failure is logged instead of raised, so one app's database problem can't take down
    other apps served from the same process; /healthz then reports the database as unavailable."""
    from flask_migrate import upgrade
    with app.app_context():
        try:
            upgrade(directory=str(MIGRATIONS_DIR))
        except Exception:
            app.logger.exception("Automatic database upgrade failed; run `flask db upgrade` by hand.")


def create_app(config_name: str | None = None) -> Flask:
    load_dotenv()
    config_name = config_name or os.environ.get("APP_ENV", "development")
    if config_name not in CONFIGS:
        raise RuntimeError(f"APP_ENV must be one of: {', '.join(CONFIGS)}")
    app = Flask(__name__)
    app.url_map.converters["int"] = BoundedIntConverter
    app.config.from_object(CONFIGS[config_name])
    if config_name != "testing":
        # Read secrets at app-creation time (not import time) so .env and the environment are honoured.
        app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "")
        app.config["SQLALCHEMY_DATABASE_URI"] = normalize_database_url(os.environ.get("DATABASE_URL", ""))
        for key in ("SIGNUP_ACCESS_CODE", "LEGAL_ENTITY_NAME", "SUPPORT_EMAIL", "PAYMENT_LINK", "APP_BASE_URL"):
            app.config[key] = os.environ.get(key, "").strip()
        # The sellingteen hosting package sets LEGAL_NAME for every app.
        app.config["LEGAL_ENTITY_NAME"] = app.config["LEGAL_ENTITY_NAME"] or os.environ.get("LEGAL_NAME", "").strip()
        app.config["BILLING_ENABLED"] = os.environ.get("BILLING_ENABLED", "1").strip().lower() in ("1", "true", "yes")
        app.config["TRIAL_DAYS"] = int(os.environ.get("TRIAL_DAYS", "") or app.config["TRIAL_DAYS"])
        app.config["PRICE_TEXT"] = os.environ.get("PRICE_TEXT", "").strip() or app.config["PRICE_TEXT"]
        app.config["AUTO_MIGRATE"] = os.environ.get("AUTO_MIGRATE", "").strip().lower() in ("1", "true", "yes")
        if os.environ.get("FORCE_HTTPS"):
            app.config["FORCE_HTTPS"] = os.environ["FORCE_HTTPS"].lower() in ("1", "true", "yes")
        app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_BYTES", 1024 * 1024))
        app.config["DRAFT_PROVIDER"] = os.environ.get("DRAFT_PROVIDER", "template")
        app.config["APP_TIMEZONE"] = os.environ.get("APP_TIMEZONE", app.config["APP_TIMEZONE"])
        hosts = [h.strip() for h in os.environ.get("TRUSTED_HOSTS", "").split(",") if h.strip()]
        if hosts:
            app.config["TRUSTED_HOSTS"] = hosts

    app.config["SQLALCHEMY_DATABASE_URI"] = normalize_database_url(app.config["SQLALCHEMY_DATABASE_URI"])
    if not app.config["SQLALCHEMY_DATABASE_URI"]:
        raise RuntimeError("DATABASE_URL is not set. Copy .env.example to .env and configure it.")
    secret = app.config["SECRET_KEY"]
    if secret in WEAK_SECRETS or len(secret) < 32:
        if config_name == "production":
            raise RuntimeError("SECRET_KEY must be a random string of at least 32 characters in production.")
        if not app.config.get("TESTING"):
            app.logger.warning("SECRET_KEY is weak or missing; set a strong one in .env.")
            if not secret:
                raise RuntimeError("SECRET_KEY is not set. Copy .env.example to .env and configure it.")
    try:
        ZoneInfo(app.config["APP_TIMEZONE"])
    except (ZoneInfoNotFoundError, ValueError):
        raise RuntimeError(f"APP_TIMEZONE '{app.config['APP_TIMEZONE']}' is not a valid IANA time zone, e.g. America/Chicago.")

    if os.environ.get("TRUST_PROXY", "").lower() in ("1", "true", "yes"):
        # Behind exactly one reverse proxy (nginx, a PaaS router): trust its X-Forwarded-* headers
        # so redirects and invite links use https and the real host.
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    _configure_logging(app)
    if config_name == "production":
        missing = [k for k in ("LEGAL_ENTITY_NAME", "SUPPORT_EMAIL") if not app.config.get(k)]
        if missing:
            app.logger.warning("Not set: %s. The privacy notice and terms pages will show placeholders.", ", ".join(missing))

    # Keep invite tokens and customer search terms out of request logs.
    redact = RedactSensitiveFilter()
    for name in ("werkzeug", "waitress", "gunicorn.access"):
        logging.getLogger(name).addFilter(redact)

    db.init_app(app)
    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
        _enable_sqlite_foreign_keys(app)
    # Migrations live inside the package so they ship with it (the hosting build copies only the package).
    migrate.init_app(app, db, directory=str(MIGRATIONS_DIR))
    # Register `db` on app.cli too, so `app.test_cli_runner()` (used by the hosting manage.py) can run
    # `db upgrade`; the plain `flask db` command finds it through Flask-Migrate's plugin as well.
    from flask_migrate.cli import db as db_cli
    app.cli.add_command(db_cli)
    csrf.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message_category = "info"
    login_manager.session_protection = "strong"

    from . import models  # noqa: F401  (register models with SQLAlchemy/Alembic)
    from . import billing
    from .auth import bp as auth_bp
    from .data_rights import bp as data_bp
    from .drafts import bp as drafts_bp
    from .leads import bp as leads_bp
    from .main import bp as main_bp
    from .passwords import bp as passwords_bp
    from .workspace import bp as workspace_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(leads_bp)
    app.register_blueprint(drafts_bp)
    app.register_blueprint(workspace_bp)
    app.register_blueprint(data_bp)
    app.register_blueprint(billing.bp)
    app.register_blueprint(passwords_bp)

    from .cli import register_cli
    from .security import register_security
    from .template_helpers import register_template_helpers

    register_security(app)
    # Runs after the tenant is loaded (register_security), so it knows the active workspace.
    app.before_request(billing.check_access)
    register_template_helpers(app)
    register_cli(app)
    billing.register_cli(app)
    _register_error_handlers(app)
    if app.config.get("AUTO_MIGRATE"):
        _auto_migrate(app)
    return app


def _enable_sqlite_foreign_keys(app: Flask) -> None:
    """SQLite (used for tests) ignores foreign keys unless asked; PostgreSQL always enforces them."""
    from sqlalchemy import event

    with app.app_context():
        @event.listens_for(db.engine, "connect")
        def _fk_pragma(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()


@login_manager.user_loader
def load_user(session_id: str):
    """Session ids are "<user id>.<session token>". Logging out rotates the token,
    which invalidates every copy of the old session cookie."""
    from .models import User

    user_id, _, token = session_id.partition(".")
    if not user_id.isdigit() or not token or int(user_id) > MAX_DB_INT:
        return None
    user = db.session.get(User, int(user_id))
    if user is None or not user.session_token or not _consteq(user.session_token, token):
        return None
    return user


def _consteq(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(a.encode(), b.encode())


def _register_error_handlers(app: Flask) -> None:
    @app.errorhandler(SecurityError)
    def untrusted_host(e):
        # Raised for a Host header not in TRUSTED_HOSTS. Templates can't build URLs
        # for an untrusted host, so answer with plain text.
        return "Bad request.", 400, {"Content-Type": "text/plain; charset=utf-8"}

    @app.errorhandler(CSRFError)
    def csrf_error(e):
        return render_template("errors/error.html", code=400, message="Your form expired or was invalid. Please go back, refresh and try again."), 400

    @app.errorhandler(400)
    def bad_request(e):
        return render_template("errors/error.html", code=400, message="Bad request."), 400

    @app.errorhandler(403)
    def forbidden(e):
        return render_template("errors/error.html", code=403, message="You don't have permission to do that."), 403

    @app.errorhandler(404)
    def not_found(e):
        return render_template("errors/error.html", code=404, message="Page not found."), 404

    @app.errorhandler(405)
    def method_not_allowed(e):
        return render_template("errors/error.html", code=405, message="That action isn't allowed here."), 405

    @app.errorhandler(413)
    def too_large(e):
        return render_template("errors/error.html", code=413, message="That file is too large."), 413

    @app.errorhandler(500)
    def server_error(e):
        # Never leave a half-finished transaction behind; never show internals.
        db.session.rollback()
        return render_template("errors/error.html", code=500, message="Something went wrong. Nothing was saved. Please try again."), 500
