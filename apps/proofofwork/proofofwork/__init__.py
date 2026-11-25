"""ProofOfWork: job completion records with checklists, photos and shareable reports."""
import getpass
import os
import secrets
from datetime import date, timedelta

import click
from flask import Flask, render_template

from . import db as dbmod
from . import mailer
from . import security
from .services import ITEM_STATES, STATUS_LABELS


def _env_bool(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _load_secret_key(instance_path):
    key = os.environ.get("PROOFOFWORK_SECRET_KEY")
    if key:
        return key
    path = os.path.join(instance_path, "secret_key")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    key = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(key)
    return key


def _nice_date(value):
    try:
        return date.fromisoformat(value).strftime("%a %d %b %Y")
    except (TypeError, ValueError):
        return value or ""


def create_app(test_config=None):
    # Keep data (database, photos, secret key) outside the code folder in production so upgrades can't touch it.
    instance_dir = os.environ.get("PROOFOFWORK_INSTANCE_DIR")
    app = Flask(__name__, instance_relative_config=True,
                instance_path=os.path.abspath(instance_dir) if instance_dir else None)
    os.makedirs(app.instance_path, exist_ok=True)
    max_mb = int(os.environ.get("PROOFOFWORK_MAX_PHOTO_MB", "10"))
    max_photos = int(os.environ.get("PROOFOFWORK_MAX_PHOTOS_PER_UPLOAD", "8"))
    app.config.update(
        DATABASE=os.environ.get("PROOFOFWORK_DATABASE", os.path.join(app.instance_path, "proofofwork.sqlite3")),
        UPLOAD_DIR=os.environ.get("PROOFOFWORK_UPLOAD_DIR", os.path.join(app.instance_path, "uploads")),
        # Only used to name the business when an old single-business database is upgraded to accounts.
        COMPANY_NAME=os.environ.get("PROOFOFWORK_COMPANY_NAME", "My business"),
        BASE_URL=os.environ.get("PROOFOFWORK_BASE_URL", "").rstrip("/"),
        MAX_PHOTO_BYTES=max_mb * 1024 * 1024,
        MAX_PHOTOS_PER_UPLOAD=max_photos,
        # Hard cap for one request: a full batch of photos plus headroom for the other form fields.
        MAX_CONTENT_LENGTH=max_photos * max_mb * 1024 * 1024 + 512 * 1024,
        ALLOW_DEMO_RESET=_env_bool("PROOFOFWORK_ALLOW_RESET", True),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=_env_bool("PROOFOFWORK_SECURE_COOKIES", False),
        SESSION_COOKIE_NAME="pow_session",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        CSRF_ENABLED=True,
        # Free trial and subscription (see billing.py). BILLING_ENABLED=0 turns it off.
        BILLING_ENABLED=_env_bool("BILLING_ENABLED", True),
        TRIAL_DAYS=int(os.environ.get("TRIAL_DAYS", "30") or 30),
        PAYMENT_LINK=os.environ.get("PAYMENT_LINK", "").strip(),
        PRICE_TEXT=os.environ.get("PRICE_TEXT", "$19 per month").strip(),
        SUPPORT_EMAIL=os.environ.get("SUPPORT_EMAIL", "").strip(),
        LEGAL_NAME=os.environ.get("LEGAL_NAME", "sellingteen").strip(),
        # Behind one HTTPS reverse proxy (e.g. PythonAnywhere): trust its X-Forwarded-* headers.
        TRUST_PROXY=_env_bool("TRUST_PROXY", False),
        # Optional email (password reset links only). Off unless SMTP_HOST and SMTP_FROM are set.
        MAIL=mailer.mail_settings(),
    )
    if test_config:
        app.config.update(test_config)
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = _load_secret_key(app.instance_path)
    os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)

    if app.config["TRUST_PROXY"]:
        # Correct client IPs (login throttling) and https:// share links behind the hosting proxy.
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    dbmod.init_app(app)
    from . import account, auth, billing, legal, views  # noqa: F401 (account adds routes to auth.bp)
    app.register_blueprint(auth.bp)
    app.register_blueprint(views.bp)
    app.register_blueprint(billing.bp)
    app.register_blueprint(legal.bp)
    # After the blueprints, so the signed-in user is loaded before the CSRF check can fail.
    security.init_app(app)
    # Then the trial/subscription check, which needs the signed-in user's account.
    app.before_request(billing.check_access)
    billing.register_cli(app)

    @app.get("/healthz")
    def healthz():
        # For the hosting platform's health check: confirms the app and database respond.
        dbmod.get_db().execute("SELECT 1").fetchone()
        return {"status": "ok"}

    app.jinja_env.filters["nice_date"] = _nice_date
    app.jinja_env.globals["mail_enabled"] = lambda: mailer.mail_enabled(app.config.get("MAIL"))
    app.jinja_env.globals.update(STATUS_LABELS=STATUS_LABELS, ITEM_STATES=ITEM_STATES)

    def error_page(code, title, message):
        def handler(exc):
            detail = getattr(exc, "description", None) if code == 400 else None
            return render_template("error.html", code=code, title=title, message=detail or message), code
        return handler

    app.register_error_handler(400, error_page(400, "Bad request", "The request could not be processed."))
    app.register_error_handler(403, error_page(403, "Not allowed", "You do not have permission to do that."))
    app.register_error_handler(404, error_page(404, "Not found", "This page does not exist or is no longer available."))
    app.register_error_handler(405, error_page(405, "Page expired", "Go back to the previous page and try again."))
    app.register_error_handler(413, error_page(
        413, "Upload too large",
        f"Uploads are limited to {max_mb} MB per photo and {max_photos} photos at a time. Go back and try fewer or smaller photos."))
    app.register_error_handler(500, error_page(500, "Something went wrong", "An unexpected error occurred."))

    with app.app_context():
        dbmod.init_db()

    @app.cli.command("init-db")
    def init_db_command():
        """Create database tables (safe to run repeatedly)."""
        dbmod.init_db()
        click.echo("Database ready.")

    def account_for(email):
        """The business account for --account EMAIL (its sign-up or a manager's email), or the only account."""
        db = dbmod.get_db()
        if email:
            row = billing._find_account(db, email)
            if row is None:
                raise click.ClickException(f"No account with the email {email}.")
            return row["id"]
        rows = db.execute("SELECT id FROM accounts ORDER BY id LIMIT 2").fetchall()
        if len(rows) != 1:
            raise click.ClickException("Say which business with --account their@email.com.")
        return rows[0]["id"]

    @app.cli.command("create-user")
    @click.option("--email", prompt=True)
    @click.option("--name", prompt=True)
    @click.option("--role", type=click.Choice(["manager", "worker"]), default="manager", show_default=True)
    @click.option("--account", "account_email", default=None,
                  help="Email of the business account to add the user to (needed when there are several).")
    def create_user_command(email, name, role, account_email):
        """Create a user in a business account (prompts for a password)."""
        password = os.environ.get("PROOFOFWORK_NEW_USER_PASSWORD") or getpass.getpass("Password (min 10 chars): ")
        account_id = account_for(account_email) if account_email else None
        try:
            auth.create_user(email, name, password, role, account_id=account_id)
        except ValueError as exc:
            raise click.ClickException(str(exc))
        click.echo(f"Created {role} {email}.")

    @app.cli.command("seed-demo")
    @click.option("--account", "account_email", default=None, help="Email of the business account.")
    def seed_demo_command(account_email):
        """Add clearly labelled demo customers, templates, jobs and photos to one business account."""
        from .demo import seed_demo
        seed_demo(account_for(account_email))
        click.echo("Demo data added.")

    @app.cli.command("reset-data")
    @click.option("--demo/--empty", default=True, help="Reload demo data after clearing (default) or leave empty.")
    @click.option("--account", "account_email", default=None, help="Email of the business account.")
    @click.confirmation_option(prompt="This deletes this business's customers, jobs and photos (users are kept). Continue?")
    def reset_data_command(demo, account_email):
        """Delete one business's job data and photos, keeping its users and every other business."""
        from .demo import reset_data
        reset_data(account_for(account_email), with_demo=demo)
        click.echo("Data reset" + (" with demo data." if demo else "."))

    return app
