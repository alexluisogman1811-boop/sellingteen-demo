"""SignedExtra: capture extra work on site, get it signed or formally noticed, invoice it."""
import os
import secrets
from datetime import date, timedelta
from pathlib import Path

import click
from flask import Flask, g, render_template

from . import db as db_module
from . import mailer
from .security import check_csrf, csrf_token, load_current_user
from .tickets_service import STATUS_LABELS
from .util import cents_to_str, fmt_datetime, x100_to_str

ROOT = Path(__file__).resolve().parent.parent


def nice_date(value):
    """date or ISO string -> 'Nov 24, 2026'."""
    try:
        d = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    except ValueError:
        return value
    return f"{d.strftime('%b')} {d.day}, {d.year}"


DEFAULT_PRICE = "$29 per month (founding price, locked for 12 months)"


def _secret_key(instance_dir, production):
    key = os.environ.get("SIGNEDEXTRA_SECRET_KEY")
    if key:
        if production and len(key) < 32:
            raise RuntimeError("SIGNEDEXTRA_SECRET_KEY is too short for production (use 32+ random characters).")
        return key
    if production:
        raise RuntimeError("SIGNEDEXTRA_SECRET_KEY must be set when SIGNEDEXTRA_ENV=production.")
    key_file = instance_dir / ".secret_key"
    if not key_file.exists():
        key_file.write_text(secrets.token_hex(32), encoding="utf-8")
    return key_file.read_text(encoding="utf-8").strip()


def create_app(test_config=None):
    instance_dir = Path(os.environ.get("SIGNEDEXTRA_INSTANCE", ROOT / "instance"))
    instance_dir.mkdir(parents=True, exist_ok=True)
    production = os.environ.get("SIGNEDEXTRA_ENV") == "production"

    app = Flask(__name__, instance_path=str(instance_dir))
    app.config.update(
        DATABASE=str(instance_dir / "signedextra.sqlite3"),
        UPLOAD_DIR=str(instance_dir / "uploads"),
        MAX_CONTENT_LENGTH=12 * 1024 * 1024,
        PERMANENT_SESSION_LIFETIME=timedelta(days=14),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=production,
        DEMO_MODE=os.environ.get("SIGNEDEXTRA_DEMO") == "1",
        PRODUCTION=production,
        SIGN_LINK_DAYS=7,
        INVITE_DAYS=7,
        RESET_LINK_HOURS=24,
        # Public address used in every link people copy (invites, signing, password resets).
        # In production the request's Host header is never used for these links.
        PUBLIC_BASE_URL=os.environ.get("PUBLIC_BASE_URL", "").strip(),
        # Behind one HTTPS proxy (e.g. PythonAnywhere): trust its X-Forwarded-For/Proto only.
        TRUST_PROXY=os.environ.get("TRUST_PROXY") == "1",
        # Free trial and subscription (billing.py). BILLING_ENABLED=0 turns it off.
        BILLING_ENABLED=os.environ.get("BILLING_ENABLED", "1") == "1",
        TRIAL_DAYS=int(os.environ.get("TRIAL_DAYS", "30") or 30),
        PAYMENT_LINK=os.environ.get("PAYMENT_LINK", "").strip(),
        PRICE_TEXT=os.environ.get("PRICE_TEXT", "").strip() or DEFAULT_PRICE,
        SUPPORT_EMAIL=os.environ.get("SUPPORT_EMAIL", "").strip(),
        LEGAL_NAME=os.environ.get("LEGAL_NAME", "sellingteen").strip(),
        # Per-IP limits: (max events, window in seconds). See ratelimit.py.
        RATE_LIMIT_SIGN=(60, 15 * 60),
        RATE_LIMIT_SIGN_BAD=(10, 15 * 60),
        RATE_LIMIT_SIGNUP=(5, 60 * 60),
        RATE_LIMIT_RESET=(10, 60 * 60),       # "forgot password" emails per IP
        RATE_LIMIT_RESET_EMAIL=(3, 60 * 60),  # ... and per email address
        # Optional email (OFF unless SMTP_HOST and SMTP_FROM are set). Only for password-reset links.
        MAIL=mailer.mail_settings(),
    )
    if test_config:
        app.config.update(test_config)
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = _secret_key(instance_dir, production)
    Path(app.config["UPLOAD_DIR"]).mkdir(parents=True, exist_ok=True)

    if app.config["TRUST_PROXY"]:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
    if production and not (app.config["PUBLIC_BASE_URL"] or "").startswith("https://"):
        app.logger.warning("PUBLIC_BASE_URL is not an https:// address; shared links will be incomplete.")

    db_module.init_app(app)

    from . import billing

    @app.before_request
    def _before():
        load_current_user()
        check_csrf()
        return billing.check_access()

    @app.after_request
    def _headers(resp):
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self';"
            " frame-ancestors 'none'; form-action 'self'; base-uri 'none'; object-src 'none'")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Permissions-Policy"] = "geolocation=(), microphone=()"
        if production:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if g.get("user") is not None:
            resp.headers["Cache-Control"] = "no-store"
        return resp

    app.jinja_env.globals["csrf_token"] = csrf_token
    app.jinja_env.filters["money"] = cents_to_str
    app.jinja_env.filters["qty"] = x100_to_str
    app.jinja_env.filters["dt"] = fmt_datetime
    app.jinja_env.filters["status_label"] = lambda s: STATUS_LABELS.get(s, s)

    app.jinja_env.filters["nice_date"] = nice_date

    @app.context_processor
    def _ctx():
        from .links import public_base_configured
        from .security import can_see_prices, is_manager
        return {"is_manager": is_manager, "can_see_prices": can_see_prices,
                "public_base_configured": public_base_configured,
                "mail_enabled": lambda: mailer.mail_enabled(app.config.get("MAIL"))}

    from . import legal
    from .views import account, auth, core, invoices, public, tickets
    app.register_blueprint(auth.bp)
    app.register_blueprint(account.bp)
    app.register_blueprint(core.bp)
    app.register_blueprint(tickets.bp)
    app.register_blueprint(invoices.bp)
    app.register_blueprint(public.bp)
    app.register_blueprint(billing.bp)
    app.register_blueprint(legal.bp)
    billing.register_cli(app)

    @app.get("/healthz")
    def healthz():
        # For the hosting platform's health check: confirms the app and database respond.
        db_module.get_db().execute("SELECT 1").fetchone()
        return {"status": "ok"}

    def _error(code, title):
        def handler(err):
            message = getattr(err, "description", "") or ""
            return render_template("error.html", code=code, title=title, message=message), code
        return handler

    app.register_error_handler(400, _error(400, "Bad request"))
    app.register_error_handler(403, _error(403, "You don't have access to this page"))
    app.register_error_handler(404, _error(404, "Page not found"))
    app.register_error_handler(405, _error(405, "That page can't be opened this way"))
    app.register_error_handler(409, _error(409, "That action isn't allowed right now"))
    app.register_error_handler(413, _error(413, "File too large"))
    app.register_error_handler(429, _error(429, "Please slow down"))
    app.register_error_handler(500, _error(500, "Something went wrong"))

    @app.cli.command("reset-link")
    @click.argument("email")
    def reset_link_command(email):
        """Support: print a one-time, 24-hour password-reset link for a user (e.g. an Owner who forgot).
        Verify the request came from that user's own email first, then reply to that address with it."""
        from .passwords import create_reset_link
        db = db_module.get_db()
        user = db.execute("SELECT * FROM users WHERE email = ?", ((email or "").strip().lower(),)).fetchone()
        if user is None or not user["active"]:
            raise click.ClickException(f"No active user with the email {email}.")
        with app.test_request_context("/"):
            link = create_reset_link(user, created_by="support (command line)")
        db.commit()
        click.echo(f"Reset link for {user['email']} ({user['role']}), valid 24 hours, works once:\n{link}")

    @app.cli.command("seed-demo")
    def seed_demo_command():
        """Create a demo company with clearly labeled fictional sample data."""
        from .demo import seed_demo
        if production:
            raise click.ClickException("Refusing to add demo data while SIGNEDEXTRA_ENV=production.")
        accounts = seed_demo()
        click.echo("Demo company created. All names and amounts are fictional sample data.")
        for role, email, password in accounts:
            click.echo(f"  {role:8} {email}  password: {password}")

    return app
