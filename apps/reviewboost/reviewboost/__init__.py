import logging
import os
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from flask import Flask, render_template
from werkzeug.exceptions import HTTPException

from . import db, mailer, security

STATUS_LABELS = {
    "not_contacted": "Not contacted",
    "contacted": "Contacted",
    "review_received": "Review received",
}

# Plain-language titles/messages for error pages, instead of HTTP jargon.
FRIENDLY_ERRORS = {
    400: ("Something wasn't right", "Please go back and try again."),
    403: ("You don't have access to this", "Please go back and try again."),
    404: ("Page not found", "We couldn't find that page. It may have been deleted."),
    405: ("That action isn't available", "Please use the buttons in the app."),
    413: ("That's too much text", "Please shorten what you typed and try again."),
}


def load_env_file(path):
    """Load KEY=VALUE lines from a .env file without overriding real env vars."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class StripQueryStrings(logging.Filter):
    """Request logs must not contain search terms (customer names, phone numbers)."""

    def filter(self, record):
        if record.args and isinstance(record.args, tuple) and isinstance(record.args[0], str):
            record.args = (re.sub(r"\?\S*", "?[hidden]", record.args[0]),) + record.args[1:]
        return True


def nice_date(iso):
    try:
        d = date.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso
    return f"{d.strftime('%b')} {d.day}, {d.year}"


def create_app(test_config=None):
    if test_config is None:
        load_env_file(Path(__file__).resolve().parent.parent / ".env")

    production = os.environ.get("APP_ENV", "development").strip().lower() == "production"
    secure_cookie = os.environ.get("SESSION_COOKIE_SECURE", "1" if production else "0") == "1"

    app = Flask(__name__, instance_relative_config=True)
    app.config.update(
        PRODUCTION=production,
        SECRET_KEY=os.environ.get("SECRET_KEY"),
        DATABASE=os.environ.get("DATABASE_PATH")
        or os.path.join(app.instance_path, "reviewboost.sqlite"),
        # AI is OFF (offline messages) unless AI_MOCK=0 AND an ANTHROPIC_API_KEY are both set.
        AI_MOCK=os.environ.get("AI_MOCK", "1"),
        AI_MODEL=os.environ.get("AI_MODEL", "").strip() or "claude-haiku-5-5",
        ANTHROPIC_API_KEY=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=secure_cookie,  # on by default in production (HTTPS only)
        TRUST_PROXY=os.environ.get("TRUST_PROXY") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        # One-click private demo accounts with sample data. Set DEMO_MODE=0 to turn off.
        DEMO_MODE=os.environ.get("DEMO_MODE", "1") == "1",
        MAX_CONTENT_LENGTH=64 * 1024,
        # Free trial and subscription (see billing.py). BILLING_ENABLED=0 turns it off.
        BILLING_ENABLED=os.environ.get("BILLING_ENABLED", "1") == "1",
        TRIAL_DAYS=int(os.environ.get("TRIAL_DAYS", "30") or 30),
        PAYMENT_LINK=os.environ.get("PAYMENT_LINK", "").strip(),
        PRICE_TEXT=os.environ.get("PRICE_TEXT", "$19 per month").strip(),
        SUPPORT_EMAIL=os.environ.get("SUPPORT_EMAIL", "").strip(),
        LEGAL_NAME=os.environ.get("LEGAL_NAME", "sellingteen").strip(),
        # Optional email for "Forgot password?" links. OFF unless SMTP_HOST and SMTP_FROM are set.
        MAIL=mailer.mail_settings(),
    )
    if test_config:
        app.config.update(test_config)

    if not app.config["SECRET_KEY"] or app.config["SECRET_KEY"] == "change-me":
        raise RuntimeError(
            "SECRET_KEY is not set. Copy .env.example to .env and set a long random SECRET_KEY."
        )
    if app.config["PRODUCTION"] and len(app.config["SECRET_KEY"]) < 32:
        raise RuntimeError("SECRET_KEY is too short for production. Use at least 32 random characters.")

    if app.config["TRUST_PROXY"]:
        # Behind one HTTPS reverse proxy: use its X-Forwarded-For/Proto so client IPs
        # (login throttling) and https:// URLs are correct.
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    werkzeug_log = logging.getLogger("werkzeug")
    if not any(isinstance(f, StripQueryStrings) for f in werkzeug_log.filters):
        werkzeug_log.addFilter(StripQueryStrings())

    db.init_db(app)
    app.before_request(security.load_current_business)
    app.before_request(security.check_csrf)
    from . import billing
    app.before_request(billing.check_access)
    app.after_request(security.add_security_headers)
    app.jinja_env.globals.update(csrf_token=security.csrf_token, STATUS_LABELS=STATUS_LABELS,
                                 mail_on=lambda: mailer.mail_enabled(app.config.get("MAIL")))
    app.jinja_env.filters["nice_date"] = nice_date

    from . import auth, customers, demo, legal, message_templates

    app.register_blueprint(auth.bp)
    app.register_blueprint(demo.bp)
    app.register_blueprint(customers.bp)
    app.register_blueprint(message_templates.bp)
    app.register_blueprint(billing.bp)
    app.register_blueprint(legal.bp)
    billing.register_cli(app)

    @app.get("/healthz")
    def healthz():
        # For the hosting platform's health check: confirms the app and database respond.
        db.get_db().execute("SELECT 1").fetchone()
        return {"status": "ok"}

    @app.errorhandler(HTTPException)
    def handle_http_error(err):
        title, message = FRIENDLY_ERRORS.get(err.code, (err.name, err.description))
        # Keep our own specific messages (e.g. "Customer not found."), replace Werkzeug's generic ones.
        if err.description and err.description != type(err).description:
            message = err.description
        return render_template("error.html", code=err.code, title=title, message=message), err.code

    @app.errorhandler(sqlite3.Error)
    def handle_db_error(err):
        # Log the error type only: messages can echo query details.
        app.logger.error("Database error: %s", type(err).__name__)
        return render_template(
            "error.html", code=503, title="We couldn't reach your data",
            message="Something went wrong loading or saving your information. "
                    "Please wait a moment and try again."), 503

    @app.errorhandler(Exception)
    def handle_unexpected_error(err):
        app.logger.error("Unhandled error: %s", type(err).__name__, exc_info=err)
        return render_template("error.html", code=500, title="Something went wrong",
                               message="An unexpected error occurred. Please try again."), 500

    return app
