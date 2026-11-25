"""LicenseLedger — license and renewal tracker for small digital-product sellers.

Each seller has a separate business account; every record belongs to one account.
"""
import os
from datetime import timedelta

from flask import Flask, g, redirect, request, session, url_for

from . import db as dbmod
from . import mailer, security, services

__version__ = "1.1.0"

# Pages anyone can open without logging in. The dashboard endpoint ("/") shows
# the public landing page to visitors who are not logged in.
PUBLIC_ENDPOINTS = {
    "static", "healthz", "main.dashboard", "auth.login", "auth.signup", "auth.forgot_password",
    "auth.reset_password", "legal.terms", "legal.privacy", "legal.refunds",
}


def _env_flag(name, default):
    return (os.environ.get(name, default) or default).strip().lower() in ("1", "true", "yes", "on")


def login_redirect():
    """Redirect to login, remembering the page (with the mount prefix, e.g. /licenseledger/...)."""
    target = None
    if request.method == "GET":
        target = request.script_root + request.full_path.rstrip("?")
    return redirect(url_for("auth.login", next=target))


def create_app(config=None, instance_path=None):
    app = Flask(__name__, instance_path=instance_path, instance_relative_config=True)
    config = config or {}
    secrets_ = security.load_or_create_secrets(app.instance_path)
    env = os.environ
    # Every setting is read here, once: the hosting package removes the app's
    # environment variables right after create_app() returns.
    app.config.update(
        SECRET_KEY=env.get("SECRET_KEY") or secrets_["secret_key"],
        KEY_HASH_SECRET=env.get("KEY_HASH_SECRET") or secrets_["key_hash_secret"],
        DATABASE=env.get("DATABASE_PATH") or os.path.join(app.instance_path, "licenseledger.db"),
        BACKUP_DIR=env.get("BACKUP_DIR") or os.path.join(app.instance_path, "backups"),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=_env_flag("SESSION_COOKIE_SECURE", "0"),
        SESSION_COOKIE_NAME="licenseledger_session",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        TODAY=None,
        # Free trial and subscription (see billing.py). BILLING_ENABLED=0 turns it off.
        BILLING_ENABLED=_env_flag("BILLING_ENABLED", "1"),
        TRIAL_DAYS=int((env.get("TRIAL_DAYS") or "30").strip() or 30),
        PAYMENT_LINK=(env.get("PAYMENT_LINK") or "").strip(),
        PRICE_TEXT=(env.get("PRICE_TEXT") or "$9 per month").strip(),
        SUPPORT_EMAIL=(env.get("SUPPORT_EMAIL") or "").strip(),
        LEGAL_NAME=(env.get("LEGAL_NAME") or "sellingteen").strip(),
        # Email used for the owner account when an old single-owner database is upgraded.
        LEGACY_OWNER_EMAIL=(env.get("LEGACY_OWNER_EMAIL") or dbmod.LEGACY_EMAIL).strip().lower(),
        # Optional email (password reset links only). Off unless SMTP_HOST and SMTP_FROM are set.
        MAIL=mailer.mail_settings(),
    )
    app.config.from_mapping(config)
    app.config.setdefault("BACKUP_DIR", os.path.join(app.instance_path, "backups"))

    dbmod.init_db(app.config["DATABASE"], app.config["BACKUP_DIR"], legacy_email=app.config["LEGACY_OWNER_EMAIL"])
    app.teardown_appcontext(dbmod.close_db)
    security.install_log_redaction(app)

    from . import auth, billing, legal
    from .views import bp as main_bp

    app.register_blueprint(auth.bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(billing.bp)
    app.register_blueprint(legal.bp)
    billing.register_cli(app)

    @app.get("/healthz")
    def healthz():
        dbmod.get_db().execute("SELECT 1").fetchone()
        return {"status": "ok"}

    @app.before_request
    def guard():
        security.check_csrf()
        auth.load_account()
        if request.endpoint in PUBLIC_ENDPOINTS and not (g.account and request.endpoint == "main.dashboard"):
            return None
        if g.account is None:
            return login_redirect()
        return billing.check_access()

    @app.after_request
    def headers(response):
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if session.get("account_id"):
            # Account pages hold customer data; keep them out of shared caches.
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.context_processor
    def account_context():
        return {
            "account": g.get("account"),
            "access": g.get("access"),
            "mail_on": mailer.mail_enabled(app.config["MAIL"]),
        }

    app.jinja_env.globals.update(
        csrf_token=security.csrf_token,
        LICENSE_TYPES=services.LICENSE_TYPES,
        STATUS_LABELS=services.STATUS_LABELS,
        ORDER_STATUSES=services.ORDER_STATUSES,
        PAYMENT_STATUSES=services.PAYMENT_STATUSES,
        DELIVERY_STATUSES=services.DELIVERY_STATUSES,
        DELIVERY_METHODS=services.DELIVERY_METHODS,
        ORDER_DELIVERY_STATES=services.ORDER_DELIVERY_STATES,
        app_version=__version__,
    )
    app.jinja_env.filters["days_until"] = services.days_until
    return app
