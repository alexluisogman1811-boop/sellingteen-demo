"""PayRecover AI: payment follow-ups for small service businesses."""
import os
import secrets
from datetime import timedelta

from flask import Flask, g

from . import db, mailer
from .formatting import register_filters


def _load_or_create_secret(instance_path):
    path = os.path.join(instance_path, "secret_key")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    key = secrets.token_hex(32)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(key)
    return key


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)

    app.config.update(
        DATABASE=os.path.join(app.instance_path, "payrecover.sqlite3"),
        SECRET_KEY=os.environ.get("SECRET_KEY") or _load_or_create_secret(app.instance_path),
        SESSION_COOKIE_NAME="payrecover_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
        MAX_CONTENT_LENGTH=64 * 1024,
        # Safe offline drafts unless AI_MOCK is explicitly "0" (only then can paid AI calls happen).
        AI_MOCK=os.environ.get("AI_MOCK", "1").strip() != "0",
        ANTHROPIC_API_KEY=os.environ.get("ANTHROPIC_API_KEY", ""),
        AI_MODEL=os.environ.get("AI_MODEL", "claude-haiku-5-5"),
        # Lets a demo or test pretend "today" is a fixed date (YYYY-MM-DD).
        FIXED_TODAY=os.environ.get("PAYRECOVER_TODAY") or None,
        # Free trial and subscription (see billing.py). BILLING_ENABLED=0 turns it off.
        BILLING_ENABLED=os.environ.get("BILLING_ENABLED", "1") == "1",
        TRIAL_DAYS=int(os.environ.get("TRIAL_DAYS", "30") or 30),
        PAYMENT_LINK=os.environ.get("PAYMENT_LINK", "").strip(),
        PRICE_TEXT=os.environ.get("PRICE_TEXT", "$12 per month").strip(),
        SUPPORT_EMAIL=os.environ.get("SUPPORT_EMAIL", "").strip(),
        LEGAL_NAME=os.environ.get("LEGAL_NAME", "sellingteen").strip(),
    )
    # Optional email (forgot-password links only). Off unless SMTP_HOST and SMTP_FROM are set.
    app.config["MAIL"] = mailer.mail_settings()
    if test_config:
        app.config.update(test_config)

    db.init_app(app)
    register_filters(app)

    from . import account, auth, billing, legal, routes

    app.register_blueprint(auth.bp)
    app.register_blueprint(routes.bp)
    app.register_blueprint(billing.bp)
    app.register_blueprint(legal.bp)
    app.register_blueprint(account.bp)
    billing.register_cli(app)

    app.before_request(auth.load_user)
    app.before_request(auth.check_csrf)
    app.before_request(billing.check_access)
    app.jinja_env.globals["csrf_token"] = auth.csrf_token
    app.jinja_env.globals["email_enabled"] = auth.email_enabled

    @app.after_request
    def security_headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        if g.get("user") is not None:
            resp.headers["Cache-Control"] = "no-store"
        return resp

    from flask import render_template

    @app.errorhandler(400)
    def bad_request(err):
        return render_template("error.html", code=400, message=err.description), 400

    @app.errorhandler(404)
    def not_found(err):
        return render_template("error.html", code=404, message="We couldn't find that page."), 404

    @app.errorhandler(413)
    def too_large(err):
        return render_template("error.html", code=413, message="That form was too large."), 413

    return app
