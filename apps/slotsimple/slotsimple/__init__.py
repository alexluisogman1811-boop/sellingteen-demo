"""SlotSimple - online appointment booking for small service businesses (many businesses per install)."""
import logging
import os
import secrets
from datetime import timedelta
from pathlib import Path

import click
from flask import Flask, abort, g, render_template

from . import db as dbmod
from . import mailer
from .security import check_csrf, csrf_token, load_account, set_security_headers
from .timeutil import from_iso, get_tz, now_utc

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _env(*names, default=None):
    """First non-empty environment variable among ``names`` (hosting name first, legacy name second)."""
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


def _env_flag(*names, default=False):
    value = _env(*names)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on")


def _secret_key(instance_path):
    key = _env("SECRET_KEY", "SLOTSIMPLE_SECRET_KEY")
    if key:
        return key
    path = Path(instance_path) / "secret_key"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(32), encoding="utf-8")
    return path.read_text(encoding="utf-8").strip()


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    production = (_env("APP_ENV", default="development") or "").lower() == "production"

    app.config.update(
        PRODUCTION=production,
        DATABASE=_env("DATABASE_PATH", "SLOTSIMPLE_DATABASE"),
        DEMO_MODE=_env_flag("DEMO_MODE", "SLOTSIMPLE_DEMO"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=_env_flag("SESSION_COOKIE_SECURE", "SLOTSIMPLE_SECURE_COOKIES", default=production),
        TRUST_PROXY=_env_flag("TRUST_PROXY"),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        MAX_CONTENT_LENGTH=64 * 1024,
        RATE_LIMITS=True,
        NOW=None,
        # Free trial and subscription (see billing.py). BILLING_ENABLED=0 turns it off.
        BILLING_ENABLED=_env_flag("BILLING_ENABLED", default=True),
        TRIAL_DAYS=int(_env("TRIAL_DAYS", default="30")),
        PAYMENT_LINK=_env("PAYMENT_LINK", default=""),
        PRICE_TEXT=_env("PRICE_TEXT", default="$12 per month"),
        SUPPORT_EMAIL=_env("SUPPORT_EMAIL", default=""),
        LEGAL_NAME=_env("LEGAL_NAME", default="sellingteen"),
        # Optional email for "Forgot password?" links only. Off unless SMTP_HOST and SMTP_FROM are set.
        MAIL=mailer.mail_settings(),
    )
    if test_config:
        app.config.update(test_config)
    if not app.config.get("DATABASE"):
        Path(app.instance_path).mkdir(parents=True, exist_ok=True)
        app.config["DATABASE"] = str(Path(app.instance_path) / "slotsimple.sqlite3")
    if not app.config.get("SECRET_KEY"):
        if app.config["PRODUCTION"] and not _env("SECRET_KEY", "SLOTSIMPLE_SECRET_KEY"):
            raise RuntimeError("SECRET_KEY is not set. Set a long random SECRET_KEY for production.")
        app.config["SECRET_KEY"] = _secret_key(app.instance_path)
    if app.config["PRODUCTION"] and len(app.config["SECRET_KEY"]) < 32:
        raise RuntimeError("SECRET_KEY is too short for production. Use at least 32 random characters.")

    if app.config["TRUST_PROXY"]:
        # Behind one HTTPS reverse proxy: real client IPs (rate limits) and https:// URLs.
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    app.teardown_appcontext(dbmod.close_db)

    with app.app_context():
        dbmod.init_db(dbmod.get_db(), app.config["DATABASE"])
        if app.config["DEMO_MODE"]:
            from .demo import ensure_demo_account
            ensure_demo_account(dbmod.get_db())

    from . import billing

    @app.before_request
    def _before():
        check_csrf()
        load_account()
        # Which business's data this request shows: the one named in /book/<slug>/...
        # for public pages, otherwise the logged-in owner's own account.
        slug = g.get("slug")
        if slug is not None:
            from .accounts import get_account_by_slug
            g.biz = get_account_by_slug(dbmod.get_db(), slug)
            if g.biz is None:
                abort(404, description="We couldn't find that booking page. Please check the link.")
        else:
            g.biz = g.get("account")
        g.settings = dbmod.get_settings(dbmod.get_db(), g.biz["id"]) if g.biz else dict(dbmod.DEFAULT_SETTINGS)
        g.tz = get_tz(g.settings["timezone"])
        return billing.check_access()

    app.after_request(set_security_headers)
    # A global (not just context) so imported macros can render CSRF fields.
    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.context_processor
    def _inject():
        return {
            "csrf_token": csrf_token,
            "settings": g.get("settings", {}),
            "demo_mode": app.config["DEMO_MODE"],
            "current_admin": g.get("account"),
            "biz": g.get("biz"),
            "WEEKDAYS": WEEKDAYS,
            "mail_enabled": mailer.mail_enabled(app.config.get("MAIL")),
        }

    @app.template_filter("local")
    def _local(value, fmt="%a %d %b %Y, %H:%M"):
        if not value:
            return ""
        dt = from_iso(value) if isinstance(value, str) else value
        return dt.astimezone(g.tz).strftime(fmt)

    @app.template_filter("money")
    def _money(cents):
        symbol = g.settings.get("currency_symbol", "$") if g.get("settings") else "$"
        return f"{symbol}{(cents or 0) / 100:,.2f}"

    @app.template_filter("tzabbr")
    def _tzabbr(_unused=None):
        return now_utc().astimezone(g.tz).strftime("%Z")

    @app.template_filter("nice_date")
    def _nice_date(value):
        return f"{value.strftime('%b')} {value.day}, {value.year}" if hasattr(value, "strftime") else value

    for code in (400, 403, 404, 405, 413, 429, 500):
        app.register_error_handler(code, _error_handler)

    from . import legal, site
    from .admin import bp as admin_bp
    from .public import bp as public_bp
    app.register_blueprint(site.bp)
    app.register_blueprint(legal.bp)
    app.register_blueprint(billing.bp)
    app.register_blueprint(public_bp)
    app.register_blueprint(admin_bp)

    billing.register_cli(app)
    _register_cli(app)
    return app


def _error_handler(err):
    code = getattr(err, "code", 500) or 500
    titles = {400: "Bad request", 403: "Forbidden", 404: "Page not found", 405: "Not allowed",
              413: "Request too large", 429: "Too many requests", 500: "Something went wrong"}
    description = getattr(err, "description", "") if code != 500 else "An unexpected error occurred."
    if code == 500:
        logging.getLogger(__name__).error("Unhandled error: %s", type(err).__name__)
    if "settings" not in g:
        g.settings = dict(dbmod.DEFAULT_SETTINGS)
        g.tz = get_tz("UTC")
    g.setdefault("biz", None)
    return render_template("error.html", code=code, title=titles.get(code, "Error"),
                           description=description), code


def _register_cli(app):
    from .accounts import AccountError, create_account, get_account_by_email, set_password

    @app.cli.command("init-db")
    def init_db_command():
        """Create or upgrade tables (safe to re-run; backs up before upgrading)."""
        dbmod.init_db(dbmod.get_db(), app.config["DATABASE"])
        click.echo(f"Database ready at {app.config['DATABASE']}")

    @app.cli.command("create-account")
    @click.option("--name", prompt="Business name")
    @click.option("--email", prompt=True)
    @click.password_option()
    def create_account_command(name, email, password):
        """Create a business account (same as signing up on the website)."""
        try:
            row = create_account(dbmod.get_db(), name.strip(), email, password, now_utc())
        except AccountError as e:
            raise click.ClickException(str(e)) from None
        click.echo(f"Account {row['email']} created. Booking page: /book/{row['slug']}")

    @app.cli.command("set-password")
    @click.argument("email")
    @click.password_option()
    def set_password_command(email, password):
        """Reset an account's password."""
        row = get_account_by_email(dbmod.get_db(), email)
        if row is None:
            raise click.ClickException(f"No account with the email {email}.")
        try:
            set_password(dbmod.get_db(), row["id"], password)
        except AccountError as e:
            raise click.ClickException(str(e)) from None
        click.echo(f"Password for {row['email']} updated.")

    @app.cli.command("seed-demo")
    def seed_demo_command():
        """Create or reset the demo account (demo@example.com / demo1234). Other accounts are untouched."""
        from .demo import DEMO_EMAIL, DEMO_PASSWORD, ensure_demo_account, reset_demo
        account = ensure_demo_account(dbmod.get_db())
        reset_demo(dbmod.get_db(), account["id"])
        click.echo(f"Demo data loaded. Log in as {DEMO_EMAIL} / {DEMO_PASSWORD}.")
