"""LeadFollowUp AI - Flask application factory."""
import logging
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Flask, jsonify, render_template

from . import ai, db, demo, mailer, security
from .followups import days_overdue, due_label, followup_state, today
from .security import wants_json

log = logging.getLogger(__name__)


class RedactQueryStrings(logging.Filter):
    """Strip query strings from access-log lines: searches can contain customer names."""

    _QUERY = re.compile(r"\?[^\s\"]*")

    def filter(self, record):
        if record.args:
            record.args = tuple(self._QUERY.sub("?[redacted]", a) if isinstance(a, str) else a for a in record.args)
        return True


def _install_log_redaction():
    access_log = logging.getLogger("werkzeug")
    if not any(isinstance(f, RedactQueryStrings) for f in access_log.filters):
        access_log.addFilter(RedactQueryStrings())


def _env_flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def load_dotenv(path: str):
    """Minimal .env loader (KEY=VALUE lines). Real environment variables win."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)

    if test_config is None:
        load_dotenv(os.path.join(os.path.dirname(app.root_path), ".env"))

    secret = os.environ.get("SECRET_KEY")
    if not secret:
        secret = secrets.token_hex(32)
        log.warning("SECRET_KEY is not set; using a temporary key (sessions reset on restart).")

    app.config.from_mapping(
        SECRET_KEY=secret,
        DATABASE=os.environ.get("DATABASE_PATH") or os.path.join(app.instance_path, "leadfollowup.db"),
        APP_TIMEZONE=os.environ.get("APP_TIMEZONE", "UTC"),
        AI_MOCK=_env_flag("AI_MOCK", "1"),
        ANTHROPIC_API_KEY=os.environ.get("ANTHROPIC_API_KEY", ""),
        AI_MODEL=os.environ.get("AI_MODEL", "claude-haiku-5-5"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=_env_flag("SESSION_COOKIE_SECURE", "0"),
        PERMANENT_SESSION_LIFETIME=timedelta(days=14),
        MAX_CONTENT_LENGTH=64 * 1024,
        # Free trial and subscription (see billing.py). BILLING_ENABLED=0 turns it off.
        BILLING_ENABLED=_env_flag("BILLING_ENABLED", "1"),
        TRIAL_DAYS=int(os.environ.get("TRIAL_DAYS", "30") or 30),
        PAYMENT_LINK=os.environ.get("PAYMENT_LINK", "").strip(),
        PRICE_TEXT=os.environ.get("PRICE_TEXT", "$12 per month").strip(),
        SUPPORT_EMAIL=os.environ.get("SUPPORT_EMAIL", "").strip(),
        LEGAL_NAME=os.environ.get("LEGAL_NAME", "sellingteen").strip(),
        # Optional email, used only for "Forgot password?" links. OFF unless SMTP_HOST and SMTP_FROM are set.
        MAIL=mailer.mail_settings(),
    )
    if test_config:
        app.config.update(test_config)

    try:
        ZoneInfo(app.config["APP_TIMEZONE"])
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("Unknown APP_TIMEZONE %r; falling back to UTC", app.config["APP_TIMEZONE"])
        app.config["APP_TIMEZONE"] = "UTC"

    _install_log_redaction()
    db.init_app(app)
    demo.init_app(app)
    security.init_app(app)

    from . import account, billing, legal
    from .auth import bp as auth_bp
    from .auth import mail_on, register_password_reset
    from .leads import bp as leads_bp

    app.before_request(billing.check_access)  # runs after security's login loader
    app.register_blueprint(auth_bp)
    app.register_blueprint(leads_bp)
    app.register_blueprint(billing.bp)
    app.register_blueprint(legal.bp)
    app.register_blueprint(account.bp)
    billing.register_cli(app)
    register_password_reset(app)

    @app.route("/healthz")
    def healthz():
        """Uptime check: 200 when the app and its database answer, 503 otherwise. No details."""
        try:
            db.get_db().execute("SELECT 1").fetchone()
            resp, code = jsonify(status="ok"), 200
        except sqlite3.Error as exc:
            log.error("Health check failed (%s)", type(exc).__name__)
            resp, code = jsonify(status="error"), 503
        resp.headers["Cache-Control"] = "no-store"
        return resp, code

    @app.template_filter("localtime")
    def localtime(iso: str, fmt: str = "%d %b %Y, %H:%M"):
        if not iso:
            return ""
        return datetime.fromisoformat(iso).astimezone(ZoneInfo(app.config["APP_TIMEZONE"])).strftime(fmt)

    @app.template_filter("nicedate")
    def nicedate(iso: str):
        if not iso:
            return "—"
        return datetime.fromisoformat(iso).strftime("%a %d %b %Y")

    # Globals (not a context processor) so imported macros can use them too.
    app.jinja_env.globals.update(followup_state=followup_state, due_label=due_label,
                                days_overdue=days_overdue, today=today, mail_on=mail_on)

    @app.context_processor
    def inject_helpers():
        return {"ai_live": ai.ai_enabled()}

    def error_page(code, title, message):
        # Never include exception details: they can contain internals or secrets.
        if wants_json():
            return jsonify(error=message), code
        return render_template("error.html", code=code, title=title, message=message), code

    @app.errorhandler(400)
    def bad_request(e):
        return error_page(400, "Something went wrong", getattr(e, "description", None) or "Bad request.")

    @app.errorhandler(401)
    def unauthorized(_e):
        return error_page(401, "Please log in", "Your session has expired. Please log in again.")

    @app.errorhandler(404)
    def not_found(_e):
        return error_page(404, "Page not found", "This page doesn't exist, or you don't have access to it.")

    @app.errorhandler(405)
    def method_not_allowed(_e):
        return error_page(405, "Not allowed", "That action isn't available here.")

    @app.errorhandler(413)
    def too_large(_e):
        return error_page(413, "Too much text", "What you submitted is too long. Please shorten it and try again.")

    @app.errorhandler(sqlite3.Error)
    def database_error(e):
        log.error("Database error (%s)", type(e).__name__)
        return error_page(503, "We couldn't reach your data",
                          "The database is busy or unavailable. Please wait a moment and try again.")

    @app.errorhandler(500)
    def server_error(_e):
        return error_page(500, "Something went wrong", "An unexpected error occurred. Please try again.")

    return app
