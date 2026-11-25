"""StockPing: an online inventory tracker for small shops, built with Flask and SQLite.

Each shop signs up for its own account and only ever sees its own data.
"""
import os
import secrets
from datetime import timedelta

from flask import Flask, render_template
from werkzeug.exceptions import HTTPException

from . import db, mailer, security, services


def _env(*names, default=None):
    for name in names:
        value = os.environ.get(name)
        if value not in (None, ""):
            return value.strip()
    return default


def _secret_key(instance_path):
    """Local installs: a random key kept in instance/secret_key (created on first run)."""
    path = os.path.join(instance_path, "secret_key")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(secrets.token_hex(32))
    with open(path, encoding="utf-8") as fh:
        return fh.read().strip()


def create_app(test_config=None):
    production = _env("APP_ENV", default="development").lower() == "production"
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)
    app.config.from_mapping(
        PRODUCTION=production,
        SECRET_KEY=_env("SECRET_KEY", "STOCKPING_SECRET_KEY"),
        DATABASE=_env("DATABASE_PATH", "STOCKPING_DATABASE",
                      default=os.path.join(app.instance_path, "stockping.sqlite3")),
        CURRENCY_SYMBOL=_env("STOCKPING_CURRENCY", default="$"),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,   # CSV uploads
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # HTTPS-only login cookie: on by default in production.
        SESSION_COOKIE_SECURE=_env("SESSION_COOKIE_SECURE", default="1" if production else "0") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        TRUST_PROXY=_env("TRUST_PROXY", default="0") == "1",
        CSRF_ENABLED=True,
        # Free trial and subscription (see billing.py). BILLING_ENABLED=0 turns it off.
        BILLING_ENABLED=_env("BILLING_ENABLED", default="1") == "1",
        TRIAL_DAYS=int(_env("TRIAL_DAYS", default="30")),
        PAYMENT_LINK=_env("PAYMENT_LINK", default=""),
        PRICE_TEXT=_env("PRICE_TEXT", default="$12 per month"),
        SUPPORT_EMAIL=_env("SUPPORT_EMAIL", default=""),
        LEGAL_NAME=_env("LEGAL_NAME", default="sellingteen"),
        # Optional email (off unless SMTP_HOST and SMTP_FROM are set): only for "Forgot password?".
        MAIL=mailer.mail_settings(),
    )
    if test_config:
        app.config.update(test_config)
    if not app.config.get("SECRET_KEY"):
        if app.config["PRODUCTION"]:
            raise RuntimeError("SECRET_KEY is not set. Set a long random SECRET_KEY for production.")
        app.config["SECRET_KEY"] = _secret_key(app.instance_path)
    if app.config["PRODUCTION"] and len(app.config["SECRET_KEY"]) < 32:
        raise RuntimeError("SECRET_KEY is too short for production. Use at least 32 random characters.")

    if app.config["TRUST_PROXY"]:
        # Behind one HTTPS reverse proxy: use its X-Forwarded-For/Proto so client IPs
        # (login throttling) and https:// URLs are correct.
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)
    security.init_app(app)
    from . import auth, billing, legal
    app.before_request(billing.check_access)   # after security's load_current_account

    @app.template_filter("money")
    def money(cents):
        sign = "-" if cents < 0 else ""
        cents = abs(int(cents or 0))
        return f"{sign}{app.config['CURRENCY_SYMBOL']}{cents // 100:,}.{cents % 100:02d}"

    app.jinja_env.globals.update(
        STATUS_LABELS=services.STATUS_LABELS,
        MOVEMENT_LABELS=services.MOVEMENT_LABELS,
    )

    from .views import bp
    app.register_blueprint(bp)
    app.register_blueprint(auth.bp)
    app.register_blueprint(billing.bp)
    app.register_blueprint(legal.bp)
    billing.register_cli(app)
    auth.register_cli(app)

    @app.get("/healthz")
    def healthz():
        # For the hosting platform's health check: confirms the app and database respond.
        db.get_db().execute("SELECT 1").fetchone()
        return {"status": "ok"}

    @app.cli.command("check")
    def check_command():
        """Verify every product's quantity equals the sum of its stock movements (all accounts)."""
        import click
        conn = db.connect(app.config["DATABASE"])
        bad, total = [], 0
        try:
            for account in conn.execute("SELECT id, email FROM accounts ORDER BY id").fetchall():
                scoped = db.AccountDB(conn, account["id"])
                bad += [(account["email"], row) for row in services.find_inconsistencies(scoped)]
            total = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        finally:
            conn.close()
        for email, row in bad:
            click.echo(f"MISMATCH {row['sku']} ({email}): on hand {row['quantity']}, "
                       f"history total {row['ledger_quantity']}")
        click.echo(f"Checked {total} product(s): {len(bad)} mismatch(es).")
        if bad:
            raise SystemExit(1)

    @app.errorhandler(HTTPException)
    def http_error(exc):
        messages = {
            404: "That page or record does not exist.",
            405: "That action is not allowed here.",
            413: "The uploaded file is too large (limit 2 MB).",
        }
        message = messages.get(exc.code) or exc.description or "The request could not be processed."
        return render_template("error.html", code=exc.code, title=exc.name, message=message), exc.code

    @app.errorhandler(Exception)
    def unexpected_error(exc):
        app.logger.exception("Unhandled error")
        return render_template("error.html", code=500, title="Something went wrong",
                               message="An unexpected error occurred. No partial stock change "
                                       "was saved. Please try again."), 500

    return app
