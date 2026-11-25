"""TestimonialTap: collect, approve and display genuine client testimonials."""
import logging
import os
import re
import secrets
from datetime import datetime, timedelta

import click
from flask import Flask, render_template, request
from werkzeug.security import generate_password_hash

from . import db as dbmod
from . import mailer
from .security import RateLimiter, check_csrf, csrf_token, is_owner, load_current_account


def _env(*names, default=None):
    """First set environment variable among names (hosting names first, then TESTIMONIALTAP_*)."""
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip() != "":
            return value.strip()
    return default


def _env_bool(*names, default=False):
    value = _env(*names)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on")


def _load_secret_key(instance_path):
    key = _env("SECRET_KEY", "TESTIMONIALTAP_SECRET_KEY")
    if key:
        return key
    path = os.path.join(instance_path, "secret_key")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    # Create the file readable by the owner only (no effect on Windows ACLs).
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key)
    return key


_SHARE_TOKEN_IN_PATH = re.compile(r"(/[sw]/)([A-Za-z0-9_-]{4})[A-Za-z0-9_-]+")


class RedactShareTokens(logging.Filter):
    """Shorten /s/<token> and /w/<token> in request logs so full share links aren't logged."""

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = _SHARE_TOKEN_IN_PATH.sub(r"\1\2...", message)
        if redacted != message:
            record.msg, record.args = redacted, ()
        return True


def _install_log_redaction(app):
    # Request lines (werkzeug) and unhandled-exception reports (app logger) both include the path.
    for logger in (logging.getLogger("werkzeug"), app.logger):
        if not any(isinstance(f, RedactShareTokens) for f in logger.filters):
            logger.addFilter(RedactShareTokens())


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)

    production = (_env("APP_ENV", default="development") or "").lower() == "production"
    app.config.from_mapping(
        PRODUCTION=production,
        DATABASE=_env("DATABASE_PATH", "TESTIMONIALTAP_DB",
                      default=os.path.join(app.instance_path, "testimonialtap.sqlite")),
        # The app's public address including any prefix, e.g. https://name.pythonanywhere.com/testimonialtap
        BASE_URL=(_env("APP_BASE_URL", "TESTIMONIALTAP_BASE_URL", default="") or "").rstrip("/"),
        MAX_CONTENT_LENGTH=64 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=_env_bool("SESSION_COOKIE_SECURE", "TESTIMONIALTAP_SECURE_COOKIES",
                                        default=production),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        CSRF_ENABLED=True,
        # (max requests, window seconds) per client IP.
        # Stored testimonials, and all form POSTs (incl. ones with validation errors).
        SUBMIT_RATE_LIMIT=(5, 600),
        SUBMIT_ATTEMPT_LIMIT=(30, 600),
        LOGIN_RATE_LIMIT=(8, 300),
        SIGNUP_RATE_LIMIT=(5, 3600),
        # Bots tend to submit forms instantly; humans take a few seconds.
        MIN_SUBMIT_SECONDS=3,
        FORM_MAX_AGE_SECONDS=24 * 3600,
        PAGE_SIZE=50,
        # Free trial and subscription (see billing.py). BILLING_ENABLED=0 turns it off.
        BILLING_ENABLED=_env_bool("BILLING_ENABLED", default=True),
        TRIAL_DAYS=int(_env("TRIAL_DAYS", default="30")),
        PAYMENT_LINK=_env("PAYMENT_LINK", default=""),
        PRICE_TEXT=_env("PRICE_TEXT", default="$9 per month"),
        SUPPORT_EMAIL=_env("SUPPORT_EMAIL", default=""),
        LEGAL_NAME=_env("LEGAL_NAME", default="sellingteen"),
        # Optional email for "Forgot password?" links. OFF unless SMTP_HOST and SMTP_FROM are set.
        MAIL=mailer.mail_settings(),
        RESET_RATE_LIMIT=(5, 3600),
    )
    if test_config:
        app.config.update(test_config)
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = _load_secret_key(app.instance_path)
    _install_log_redaction(app)

    if _env_bool("TRUST_PROXY", "TESTIMONIALTAP_TRUST_PROXY"):
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    app.limiter = RateLimiter()
    app.teardown_appcontext(dbmod.close_db)
    dbmod.init_db(app)

    from . import billing
    app.before_request(load_current_account)
    app.before_request(check_csrf)
    app.before_request(billing.check_access)
    app.jinja_env.globals.update(csrf_token=csrf_token, is_owner=is_owner,
                                 mail_enabled=lambda: mailer.mail_enabled(app.config.get("MAIL")))
    app.jinja_env.filters["stars"] = lambda r: ("★" * r + "☆" * (5 - r)) if r else ""
    app.jinja_env.filters["date"] = _format_date

    from . import legal
    from .admin import bp as admin_bp
    from .public import bp as public_bp
    app.register_blueprint(admin_bp)
    app.register_blueprint(public_bp)
    app.register_blueprint(billing.bp)
    app.register_blueprint(legal.bp)
    billing.register_cli(app)

    @app.get("/healthz")
    def healthz():
        # For the hosting platform's health check: confirms the app and database respond.
        dbmod.get_db().execute("SELECT 1").fetchone()
        return {"status": "ok"}

    _register_errors(app)
    _register_headers(app)
    _register_cli(app)
    return app


def _format_date(value, with_time=False):
    if not value:
        return ""
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value
    return dt.strftime("%d %b %Y %H:%M UTC" if with_time else "%d %b %Y")


def _register_errors(app):
    messages = {
        400: "Bad request",
        403: "Forbidden",
        404: "Not found",
        405: "Method not allowed",
        413: "Submission too large",
        429: "Too many requests",
        500: "Something went wrong",
    }
    for code, title in messages.items():
        def handler(err, code=code, title=title):
            # Never show exception details; the traceback goes to the server log only.
            description = ("Something went wrong on our side. Please try again in a moment."
                           if code == 500 else getattr(err, "description", ""))
            public = request.path.startswith(("/s/", "/w/"))
            return render_template("error.html", code=code, title=title, description=description,
                                   public=public), code
        app.register_error_handler(code, handler)


def _register_headers(app):
    @app.after_request
    def security_headers(resp):
        endpoint = request.endpoint or ""
        embeddable = endpoint == "public.embed"
        csp = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
               "img-src 'self' data:; form-action 'self'; base-uri 'none'; object-src 'none'; ")
        csp += "frame-ancestors *" if embeddable else "frame-ancestors 'none'"
        resp.headers.setdefault("Content-Security-Policy", csp)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        if not embeddable:
            resp.headers.setdefault("X-Frame-Options", "DENY")
        if endpoint.startswith("public."):
            # Public URLs contain tokens; don't leak them via Referer.
            resp.headers.setdefault("Referrer-Policy", "no-referrer")
        else:
            resp.headers.setdefault("Referrer-Policy", "same-origin")
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp


def _register_cli(app):
    @app.cli.command("set-password")
    @click.argument("email")
    @click.password_option(help="New password (min 10 characters).")
    def set_password(email, password):
        """Reset an account's password (for an owner who is locked out)."""
        if len(password) < 10:
            raise click.ClickException("Password must be at least 10 characters.")
        db = dbmod.get_db()
        cur = db.execute("UPDATE accounts SET password_hash = ? WHERE email = ?",
                         (generate_password_hash(password), email.strip().lower()))
        if cur.rowcount == 0:
            raise click.ClickException(f"No account with the email {email}.")
        db.commit()
        click.echo(f"{email}: password updated.")
