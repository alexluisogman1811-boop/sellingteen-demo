"""SiteCare Desk - manual maintenance tracker for client websites, for many studios."""
import os
import secrets
from datetime import date, timedelta

from flask import Flask, abort, g, render_template, request, session
from werkzeug.exceptions import HTTPException

from . import constants, db, mailer
from .helpers import verification_key

# Logging out is harmless to forge, and must still work from a stale tab.
CSRF_EXEMPT = {"auth.logout"}


def _secret_key(instance_path):
    env = os.environ.get("SECRET_KEY") or os.environ.get("SITECARE_SECRET_KEY")
    if env:
        return env
    path = os.path.join(instance_path, "secret_key")
    if not os.path.exists(path):
        with open(path, "w", encoding="ascii") as fh:
            fh.write(secrets.token_hex(32))
    with open(path, encoding="ascii") as fh:
        return fh.read().strip()


def csrf_token():
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


def _env_flag(name, default):
    return os.environ.get(name, default).strip() == "1"


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    production = os.environ.get("APP_ENV", "development").strip().lower() == "production"
    app.config.from_mapping(
        PRODUCTION=production,
        DATABASE=os.environ.get("DATABASE_PATH") or os.path.join(app.instance_path, "sitecare.sqlite3"),
        UPLOAD_FOLDER=os.environ.get("UPLOAD_FOLDER") or os.path.join(app.instance_path, "uploads"),
        MAX_UPLOAD_BYTES=5 * 1024 * 1024,  # per evidence file
        MAX_FILES_PER_RECORD=5,
        MAX_CONTENT_LENGTH=27 * 1024 * 1024,  # whole request
        SCHEDULE_HORIZON_DAYS=60,
        FIXED_TODAY=None,  # tests pin "today" to a date
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # HTTPS-only cookie: on by default in production. SITECARE_SECURE_COOKIES=1 is the older name.
        SESSION_COOKIE_SECURE=_env_flag("SESSION_COOKIE_SECURE", "1" if production else "0")
        or _env_flag("SITECARE_SECURE_COOKIES", "0"),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        TRUST_PROXY=_env_flag("TRUST_PROXY", "0"),
        # Free trial and subscription (see billing.py). BILLING_ENABLED=0 turns it off.
        BILLING_ENABLED=_env_flag("BILLING_ENABLED", "1"),
        TRIAL_DAYS=int(os.environ.get("TRIAL_DAYS", "30") or 30),
        PAYMENT_LINK=os.environ.get("PAYMENT_LINK", "").strip(),
        PRICE_TEXT=os.environ.get("PRICE_TEXT", "$15 per month").strip(),
        SUPPORT_EMAIL=os.environ.get("SUPPORT_EMAIL", "").strip(),
        LEGAL_NAME=os.environ.get("LEGAL_NAME", "sellingteen").strip(),
        # Sign-in email for the account that receives a single-owner database's records on upgrade.
        OWNER_EMAIL=(os.environ.get("SITECARE_OWNER_EMAIL") or "").strip().lower(),
        # Optional email (password reset links only). Off unless SMTP_HOST and SMTP_FROM are set.
        MAIL=mailer.mail_settings(),
    )
    if test_config:
        app.config.update(test_config)
    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(app.config["DATABASE"])), exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    if not app.config.get("SECRET_KEY"):
        if app.config["PRODUCTION"] and not (os.environ.get("SECRET_KEY") or os.environ.get("SITECARE_SECRET_KEY")):
            raise RuntimeError("SECRET_KEY is not set. In production, set a long random SECRET_KEY.")
        app.config["SECRET_KEY"] = _secret_key(app.instance_path)
    if app.config["PRODUCTION"] and len(app.config["SECRET_KEY"]) < 32:
        raise RuntimeError("SECRET_KEY is too short for production. Use at least 32 random characters.")

    if app.config["TRUST_PROXY"]:
        # Behind one HTTPS reverse proxy: use its X-Forwarded-For/Proto so client IPs
        # (login throttling) and https:// share links are correct.
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)

    from . import auth, billing, legal

    # Order matters: who is signed in -> CSRF -> sign-in required -> trial/subscription.
    app.before_request(auth.load_current_account)

    @app.before_request
    def csrf_protect():
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.endpoint not in CSRF_EXEMPT:
            sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
            expected = session.get("_csrf") or ""
            if not expected or not secrets.compare_digest(sent.encode(), expected.encode()):
                abort(400, "Your session expired or the security token was missing. Reload the page and try again.")

    app.before_request(auth.require_login)
    app.before_request(billing.check_access)

    @app.after_request
    def security_headers(response):
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'",
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if g.get("account") is not None and request.endpoint != "static":
            # A studio's client data must not be shown from cache after logging out.
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.get("/healthz")
    def healthz():
        # For the hosting platform's health check: confirms the app and database respond.
        db.get_db().execute("SELECT 1").fetchone()
        return {"status": "ok"}

    @app.errorhandler(HTTPException)
    def http_error(exc):
        description = exc.description
        if exc.code == 413:
            description = "That upload is too large. Each evidence file must be 5 MB or smaller."
        return render_template("error.html", code=exc.code, name=exc.name, description=description), exc.code

    @app.errorhandler(Exception)
    def unhandled_error(exc):
        if isinstance(exc, HTTPException):
            return http_error(exc)
        app.logger.exception("Unhandled error")
        return (
            render_template(
                "error.html", code=500, name="Something went wrong",
                description="The error was logged. Your data was not changed by the failed request.",
            ),
            500,
        )

    @app.template_filter("nice_date")
    def nice_date(value):
        if not value:
            return ""
        d = date.fromisoformat(value[:10]) if isinstance(value, str) else value
        return d.strftime("%d %b %Y")

    @app.template_filter("filesize")
    def filesize(n):
        return f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"

    app.jinja_env.globals.update(
        csrf_token=csrf_token,
        mail_on=lambda: mailer.mail_enabled(app.config.get("MAIL")),
        PLATFORMS=constants.PLATFORMS,
        PLANS=constants.PLANS,
        WEBSITE_STATUSES=constants.WEBSITE_STATUSES,
        CATEGORIES=constants.CATEGORIES,
        FREQUENCIES=constants.FREQUENCIES,
        RESULTS=constants.RESULTS,
        OCCURRENCE_STATUSES=constants.OCCURRENCE_STATUSES,
        SEVERITIES=constants.SEVERITIES,
        ISSUE_STATUSES=constants.ISSUE_STATUSES,
        VERIFICATION=constants.VERIFICATION,
        VERIFICATION_NOTE=constants.VERIFICATION_NOTE,
        verification_key=verification_key,
    )

    from .views import bp

    app.register_blueprint(auth.bp)
    app.register_blueprint(bp)
    app.register_blueprint(billing.bp)
    app.register_blueprint(legal.bp)
    billing.register_cli(app)
    return app
