import logging
import os
import secrets
from datetime import timedelta
from pathlib import Path

from flask import Flask, flash, g, jsonify, redirect, render_template, request, session, url_for

from . import db, mailer
from .util import STATUS_LABELS, due_relative, format_date, format_money, plural, safe_next

log = logging.getLogger(__name__)


def _secret_key(instance_path, production):
    key = os.environ.get("SECRET_KEY", "").strip()
    if key:
        if production and len(key) < 32:
            raise RuntimeError("SECRET_KEY must be at least 32 characters in production")
        return key
    if production:
        raise RuntimeError("SECRET_KEY must be set when APP_ENV=production")
    # Dev convenience: persist a random key so sessions survive restarts.
    path = Path(instance_path) / "secret_key"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(secrets.token_hex(32), encoding="utf-8")
    return path.read_text(encoding="utf-8").strip()


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    production = os.environ.get("APP_ENV", "development") == "production"
    # Secure (HTTPS-only) session cookie: on by default in production; SESSION_COOKIE_SECURE=1/0 overrides.
    secure_cookie = os.environ.get("SESSION_COOKIE_SECURE", "1" if production else "0").strip() == "1"
    app.config.update(
        PRODUCTION=production,
        DATABASE=os.environ.get("DATABASE_PATH") or os.path.join(app.instance_path, "invoicechaser.sqlite3"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=secure_cookie,
        PERMANENT_SESSION_LIFETIME=timedelta(days=14),
        MAX_CONTENT_LENGTH=64 * 1024,
        # AI is OFF by default: it only runs with AI_MOCK=0 AND an ANTHROPIC_API_KEY.
        AI_MOCK=os.environ.get("AI_MOCK", "1").strip() != "0",
        ANTHROPIC_API_KEY=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
        ANTHROPIC_MODEL=os.environ.get("ANTHROPIC_MODEL", "").strip() or "claude-haiku-5-5",
        AI_GENERATIONS_PER_HOUR=int(os.environ.get("AI_GENERATIONS_PER_HOUR", "30")),
        TODAY=None,
        # Free trial and subscription (see billing.py). BILLING_ENABLED=0 turns it off.
        BILLING_ENABLED=os.environ.get("BILLING_ENABLED", "1").strip() != "0",
        TRIAL_DAYS=int(os.environ.get("TRIAL_DAYS", "30") or 30),
        # Free plan after the trial: everything keeps working, up to this many customers.
        FREE_CUSTOMER_LIMIT=int(os.environ.get("FREE_CUSTOMER_LIMIT", "5") or 5),
        PAYMENT_LINK=os.environ.get("PAYMENT_LINK", "").strip(),
        PRICE_TEXT=os.environ.get("PRICE_TEXT", "$9 per month").strip(),
        SUPPORT_EMAIL=os.environ.get("SUPPORT_EMAIL", "").strip(),
        LEGAL_NAME=os.environ.get("LEGAL_NAME", "sellingteen").strip(),
        # Optional email (off unless SMTP_HOST and SMTP_FROM are set): only for "Forgot password?" links.
        MAIL=mailer.mail_settings(),
    )
    if test_config:
        app.config.update(test_config)
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = _secret_key(app.instance_path, production)

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if os.environ.get("TRUST_PROXY") == "1":
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_db(app)

    from . import auth, billing, customers, dashboard, invoices, legal, settings
    for module in (auth, customers, invoices, dashboard, settings, billing, legal):
        app.register_blueprint(module.bp)
    billing.register_cli(app)

    app.jinja_env.filters.update(money=format_money, date=format_date, due_relative=due_relative)
    app.jinja_env.globals.update(STATUS_LABELS=STATUS_LABELS, plural=plural)
    # A global (not a context variable) so imported macros can see it too.
    app.jinja_env.globals["csrf_token"] = lambda: session.get("csrf_token", "")
    app.jinja_env.globals["mail_on"] = lambda: mailer.mail_enabled(app.config.get("MAIL"))

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok"})

    @app.before_request
    def load_user_and_check_csrf():
        g.user = None
        uid = session.get("user_id")
        if uid is not None:
            user = db.get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
            # A password change bumps session_version, signing out every other session.
            if user is None or user["session_version"] != session.get("session_version"):
                session.clear()
            else:
                g.user = user
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        if request.method == "POST":
            sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
            if not secrets.compare_digest(sent, session["csrf_token"]):
                log.info("CSRF check failed for %s %s", request.method, request.path)
                flash("That page had expired, so nothing was changed. Please try again.", "error")
                back = safe_next(_same_origin_path(request.referrer)) or url_for("dashboard.home")
                return redirect(back)

    # Runs after the user is loaded: works out the account's plan (trial / free / paid). Never blocks.
    app.before_request(billing.check_access)
    app.jinja_env.globals["pro_price"] = billing.pro_price

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "frame-ancestors 'none'; form-action 'self'; base-uri 'none'; object-src 'none'",
        )
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if production:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if request.endpoint != "static":
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    for code, title, message in (
        (400, "Something was wrong with that request", "Please go back and try again."),
        (404, "Page not found", "That page doesn't exist, or it isn't part of your account."),
        (405, "That action isn't available here", "Please use the buttons in the app."),
        (413, "That was too much data", "Please shorten what you entered and try again."),
        (500, "Something went wrong on our side", "The problem has been logged. Please try again in a moment."),
    ):
        app.register_error_handler(code, _error_handler(code, title, message))

    return app


def _same_origin_path(url):
    if not url:
        return None
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    if parts.netloc and parts.netloc != request.host:
        return None
    return parts.path + (f"?{parts.query}" if parts.query else "")


def _error_handler(code, title, message):
    def handler(err):
        if code == 500:
            log.exception("Unhandled error on %s %s", request.method, request.path)
        return render_template("error.html", code=code, title=title, message=message), code
    return handler
