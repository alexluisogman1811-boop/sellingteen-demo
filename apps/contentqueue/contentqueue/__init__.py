"""ContentQueue: a social media content planning workspace for small businesses.

Many businesses can sign up; each one only ever sees its own content.

ContentQueue NEVER posts to Instagram, TikTok, Facebook, LinkedIn, X, YouTube or any
other platform and never asks for social media passwords. "Scheduled" means planned
inside ContentQueue only. Publishing happens outside the app; users record it here.
"""
import os
import secrets
from datetime import date, timedelta
from pathlib import Path

from flask import Flask, Response, abort, render_template, request, session
from werkzeug.exceptions import SecurityError

from . import auth, db, mailer, timeutil
from .constants import COLORS, PLATFORMS, STATUS_LABELS, STATUSES
from .queries import is_overdue

CSRF_SESSION_KEY = "_csrf_token"
# Logging out is harmless to forge, and must still work from a stale tab.
CSRF_EXEMPT = {"auth.logout"}


def load_env_file(path):
    """Load KEY=VALUE lines from a .env file without overriding real env vars."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _load_secret_key(instance_path):
    """Development convenience: a random key kept in instance/secret_key."""
    path = os.path.join(instance_path, "secret_key")
    try:
        with open(path, encoding="utf-8") as fh:
            key = fh.read().strip()
            if key:
                return key
    except FileNotFoundError:
        pass
    key = secrets.token_hex(32)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(key)
    return key


def csrf_token():
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def create_app(test_config=None):
    if test_config is None:
        load_env_file(Path(__file__).resolve().parent.parent / ".env")
    env = os.environ.get
    production = env("APP_ENV", "development").strip().lower() == "production"

    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)
    database = env("DATABASE_PATH") or os.path.join(app.instance_path, "contentqueue.sqlite3")
    data_dir = os.path.dirname(os.path.abspath(database))
    app.config.from_mapping(
        PRODUCTION=production,
        SECRET_KEY=env("SECRET_KEY") or env("CONTENTQUEUE_SECRET_KEY"),
        DATABASE=database,
        UPLOAD_FOLDER=env("UPLOAD_FOLDER") or os.path.join(data_dir, "uploads"),
        BACKUP_FOLDER=env("BACKUP_FOLDER") or os.path.join(data_dir, "backups"),
        MAX_CONTENT_LENGTH=6 * 1024 * 1024,  # whole request
        MAX_IMAGE_BYTES=5 * 1024 * 1024,  # single image
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=env("SESSION_COOKIE_SECURE", "1" if production else "0") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        TRUST_PROXY=env("TRUST_PROXY") == "1",
        CSRF_ENABLED=True,
        TRUSTED_HOSTS=auth.trusted_hosts_from_env(production),
        TIMEZONE=env("CONTENTQUEUE_TIMEZONE", ""),
        # Free trial and subscription (see billing.py). BILLING_ENABLED=0 turns it off.
        BILLING_ENABLED=env("BILLING_ENABLED", "1") == "1",
        TRIAL_DAYS=int(env("TRIAL_DAYS", "30") or 30),
        PAYMENT_LINK=env("PAYMENT_LINK", "").strip(),
        PRICE_TEXT=env("PRICE_TEXT", "$9 per month").strip(),
        SUPPORT_EMAIL=env("SUPPORT_EMAIL", "").strip(),
        LEGAL_NAME=env("LEGAL_NAME", "sellingteen").strip(),
        # Optional email, only for "forgot password" links. OFF unless SMTP_HOST and SMTP_FROM are set.
        MAIL=mailer.mail_settings(),
    )
    if test_config:
        app.config.update(test_config)

    if not app.config.get("SECRET_KEY"):
        if app.config["PRODUCTION"]:
            raise RuntimeError("SECRET_KEY is not set. Set a long random SECRET_KEY for production.")
        app.config["SECRET_KEY"] = _load_secret_key(app.instance_path)
    if app.config["PRODUCTION"] and len(app.config["SECRET_KEY"]) < 32:
        raise RuntimeError("SECRET_KEY is too short for production. Use at least 32 random characters.")

    if app.config["TRUST_PROXY"]:
        # Behind one HTTPS reverse proxy: use its X-Forwarded-* headers so client IPs
        # (login throttling) and https:// URLs are correct.
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    app.config["_TZINFO"], tz_error = timeutil.resolve_timezone(app.config["TIMEZONE"])
    if tz_error:
        app.logger.warning(tz_error)
    app.config["TIMEZONE_ERROR"] = tz_error
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    db.init_app(app)

    from . import billing

    # Order matters: who is signed in → must they sign in → CSRF → trial/subscription gate.
    app.before_request(auth.load_current_account)
    app.before_request(auth.require_login)

    @app.before_request
    def csrf_protect():
        if (request.method not in ("POST", "PUT", "PATCH", "DELETE") or not app.config["CSRF_ENABLED"]
                or request.endpoint in CSRF_EXEMPT):
            return
        expected = session.get(CSRF_SESSION_KEY)
        sent = request.form.get("csrf_token") or request.headers.get("X-CSRFToken", "")
        if not expected or not sent or not secrets.compare_digest(expected, sent):
            abort(400, description="Your form session expired or the security token was missing. Please reload the page and try again.")

    app.before_request(billing.check_access)

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'",
        )
        if request.endpoint != "static":
            # Content must not be shown from cache after logging out (e.g. via the Back button).
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # Jinja globals (not a context processor) so imported macros can use them too.
    app.jinja_env.globals.update(
        csrf_token=csrf_token,
        mail_on=auth.mail_on,
        PLATFORMS=PLATFORMS,
        STATUSES=STATUSES,
        STATUS_LABELS=STATUS_LABELS,
        COLORS=COLORS,
        is_overdue=is_overdue,
    )

    @app.context_processor
    def inject_today():
        return {
            "today": timeutil.today(),
            "timezone_label": timeutil.timezone_label(),
            # One-shot signal to the browser to delete its autosaved copy after a confirmed save.
            "clear_draft": session.pop("_clear_draft", None) if "_clear_draft" in session else None,
        }

    @app.template_filter("status_label")
    def status_label(value):
        return STATUS_LABELS.get(value, value)

    @app.template_filter("platform_label")
    def platform_label(value):
        return PLATFORMS.get(value, {}).get("label", value or "")

    @app.template_filter("nice_date")
    def nice_date(value, fmt="%a %d %b %Y"):
        if not value:
            return ""
        try:
            d = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
        except ValueError:
            return value
        return d.strftime(fmt)

    def render_error(code, title, message):
        return render_template("error.html", code=code, title=title, message=message), code

    @app.errorhandler(400)
    def bad_request(err):
        return render_error(400, "Bad request", err.description)

    @app.errorhandler(SecurityError)
    def untrusted_host(_err):
        # No URL adapter exists for an untrusted Host, so don't render the templated page.
        return Response("Bad request: untrusted host header. Open ContentQueue via its normal address "
                        "or add the host to CONTENTQUEUE_ALLOWED_HOSTS.", 400, mimetype="text/plain")

    @app.errorhandler(401)
    def unauthorized(_err):
        return render_error(401, "Please log in",
                            "You're not logged in (or your session ended), so this change was not saved. "
                            "Log in again — text you were typing in the post editor is kept in this browser "
                            "and will be offered when you reopen it.")

    @app.errorhandler(404)
    def not_found(_err):
        return render_error(404, "Not found", "That page or item doesn’t exist (it may have been deleted).")

    @app.errorhandler(413)
    def too_large(_err):
        mb = app.config["MAX_IMAGE_BYTES"] // (1024 * 1024)
        return render_error(413, "Upload too large", f"Images must be {mb} MB or smaller.")

    @app.errorhandler(500)
    def server_error(_err):
        return render_error(500, "Something went wrong", "An unexpected error occurred. Your data was not changed.")

    from . import legal
    from .views import data, library, main, posts

    app.register_blueprint(main.bp)
    app.register_blueprint(posts.bp)
    app.register_blueprint(library.bp)
    app.register_blueprint(data.bp)
    app.register_blueprint(auth.bp)
    app.register_blueprint(billing.bp)
    app.register_blueprint(legal.bp)
    billing.register_cli(app)

    @app.get("/healthz")
    def healthz():
        # For the hosting platform's health check: confirms the app and database respond.
        db.get_db().execute("SELECT 1").fetchone()
        return {"status": "ok"}

    from .uploads import upload_exists

    app.jinja_env.globals.update(
        new_form_nonce=posts.new_form_nonce,
        upload_exists=lambda name: upload_exists(app.config["UPLOAD_FOLDER"], name),
    )
    return app
