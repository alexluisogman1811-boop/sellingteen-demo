import hmac
import os
import secrets
from datetime import timedelta

from urllib.parse import urlsplit

from flask import Flask, abort, flash, redirect, request, session

from . import db, mailer
from .extraction import ai_mock_enabled, ai_settings


def create_app(test_config=None):
    env_key = os.environ.get("SECRET_KEY")
    if env_key is not None and len(env_key) < 32:
        # Anyone who can guess the key can forge a login cookie for any account.
        raise RuntimeError(
            "SECRET_KEY must be a long random value (at least 32 characters). Generate one with: "
            'python -c "import secrets; print(secrets.token_hex(32))"  - or leave SECRET_KEY unset '
            "to use a random key for this run (everyone is logged out on restart).")
    app = Flask(__name__, instance_relative_config=True)
    app.config.update(
        SECRET_KEY=env_key or secrets.token_hex(32),
        DATABASE=os.path.join(app.instance_path, "contractordocs.sqlite3"),
        UPLOAD_FOLDER=os.path.join(app.instance_path, "uploads"),
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE") == "1",
        # Signed session cookies are rejected by the server after this long.
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        WTF_CSRF_ENABLED=True,
        # Free trial and subscription (see billing.py). BILLING_ENABLED=0 turns it off.
        BILLING_ENABLED=os.environ.get("BILLING_ENABLED", "1") == "1",
        TRIAL_DAYS=int(os.environ.get("TRIAL_DAYS", "30") or 30),
        PAYMENT_LINK=os.environ.get("PAYMENT_LINK", "").strip(),
        PRICE_TEXT=os.environ.get("PRICE_TEXT", "$29 per month").strip(),
        SUPPORT_EMAIL=os.environ.get("SUPPORT_EMAIL", "").strip(),
        LEGAL_NAME=os.environ.get("LEGAL_NAME", "sellingteen").strip(),
        # Optional email (off unless SMTP_HOST and SMTP_FROM are set); only used for password reset links.
        MAIL=mailer.mail_settings(),
    )
    # AI reading is off unless AI_MOCK=0 and ANTHROPIC_API_KEY are set (default model: claude-haiku-5-5).
    app.config.update(ai_settings())
    if test_config:
        app.config.update(test_config)
    if not os.environ.get("SECRET_KEY") and not app.testing:
        app.logger.warning("SECRET_KEY not set; sessions will reset when the server restarts.")

    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    db.init_db(app.config["DATABASE"])
    app.teardown_appcontext(db.close_db)

    @app.before_request
    def csrf_protect():
        if request.method == "POST" and app.config["WTF_CSRF_ENABLED"]:
            token = session.get("csrf_token")
            if not token or not hmac.compare_digest(token, request.form.get("csrf_token", "")):
                abort(400, "Invalid or missing form token. Reload the page and try again.")

    @app.errorhandler(413)
    def too_large(_e):
        # Send the user back to the page they uploaded from instead of a bare error page.
        flash("That file is larger than 10 MB. Please upload a smaller file (for example a lower-resolution scan).",
              "error")
        ref = urlsplit(request.referrer or "")
        back = ref.path if ref.netloc == request.host and ref.path.startswith("/") else (request.script_root + "/")
        return redirect(back)

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        return resp

    def csrf_token():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return session["csrf_token"]

    app.jinja_env.globals["csrf_token"] = csrf_token
    app.jinja_env.globals["demo_mode"] = ai_mock_enabled

    from .routes import bp
    app.register_blueprint(bp)

    from . import billing, legal
    # Registered after the main blueprint so g.user is already loaded when it runs.
    app.before_request(billing.check_access)
    app.register_blueprint(billing.bp)
    app.register_blueprint(legal.bp)
    billing.register_cli(app)
    return app
