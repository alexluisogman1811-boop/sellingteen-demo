"""ClientStart — client onboarding portal for small service businesses."""
import os
import secrets
from datetime import timedelta

from flask import Flask, render_template

from . import db
from .security import csrf_protect, get_csrf_token


def _env_flag(*names: str, default: bool = False) -> bool:
    """True if any of the given environment variables is set to 1/true/yes/on."""
    for name in names:
        if name in os.environ:
            return os.environ[name].strip().lower() in ("1", "true", "yes", "on")
    return default


def _load_secret_key(data_dir: str) -> str:
    """Use CLIENTSTART_SECRET_KEY, else a random key persisted in the data directory.

    Persisting it keeps owner sessions and CSRF tokens valid across restarts.
    """
    # SECRET_KEY is the name the shared sellingteen hosting package uses.
    env = os.environ.get("CLIENTSTART_SECRET_KEY") or os.environ.get("SECRET_KEY")
    if env:
        return env
    path = os.path.join(data_dir, "secret_key")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    key = secrets.token_hex(32)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    # All persistent state (database, uploads, secret key) lives in one directory.
    data_dir = os.path.abspath(os.environ.get("CLIENTSTART_DATA_DIR") or app.instance_path)
    os.makedirs(data_dir, exist_ok=True)

    app.config.from_mapping(
        DATA_DIR=data_dir,
        DATABASE=os.path.join(data_dir, "clientstart.sqlite3"),
        UPLOAD_FOLDER=os.path.join(data_dir, "uploads"),
        MAX_UPLOAD_BYTES=int(os.environ.get("CLIENTSTART_MAX_UPLOAD_MB", "10")) * 1024 * 1024,
        # Each account's own business name is shown to its clients. CLIENTSTART_AGENCY_NAME is only
        # used to name the account that receives data from an old single-owner install.
        LEGACY_BUSINESS_NAME=os.environ.get("CLIENTSTART_AGENCY_NAME", "My business"),
        LEGACY_EMAIL=os.environ.get("CLIENTSTART_LEGACY_EMAIL", ""),
        PORTAL_LINK_DAYS=int(os.environ.get("CLIENTSTART_PORTAL_LINK_DAYS", "60")),
        # Full public base URL used in portal links, e.g. https://onboard.example.com
        PUBLIC_URL=(os.environ.get("CLIENTSTART_PUBLIC_URL") or "").rstrip("/"),
        BEHIND_PROXY=_env_flag("CLIENTSTART_BEHIND_PROXY", "BEHIND_PROXY", "TRUST_PROXY"),
        # The destructive "reset all data" button is off unless explicitly enabled (demo use).
        ALLOW_RESET=_env_flag("CLIENTSTART_ALLOW_RESET"),
        CSRF_ENABLED=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=_env_flag("CLIENTSTART_SECURE_COOKIES", "SESSION_COOKIE_SECURE"),
        SESSION_COOKIE_NAME="clientstart_session",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        # Free trial and subscription (see billing.py). BILLING_ENABLED=0 turns it off.
        BILLING_ENABLED=_env_flag("BILLING_ENABLED", default=True),
        TRIAL_DAYS=int(os.environ.get("TRIAL_DAYS", "30") or 30),
        PAYMENT_LINK=os.environ.get("PAYMENT_LINK", "").strip(),
        PRICE_TEXT=os.environ.get("PRICE_TEXT", "$15 per month").strip() or "$15 per month",
        SUPPORT_EMAIL=os.environ.get("SUPPORT_EMAIL", "").strip(),
        LEGAL_NAME=os.environ.get("LEGAL_NAME", "sellingteen").strip(),
        TODAY=None,
    )
    # Optional email, only for "forgot password" links (off unless SMTP_HOST and SMTP_FROM are set).
    from . import mailer
    app.config["MAIL"] = mailer.mail_settings()
    if test_config:
        app.config.update(test_config)
    # Whole-request hard limit, a little above the per-file limit for form overhead.
    if not app.config.get("MAX_CONTENT_LENGTH"):
        app.config["MAX_CONTENT_LENGTH"] = app.config["MAX_UPLOAD_BYTES"] + 1024 * 1024
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = _load_secret_key(data_dir)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    if app.config["BEHIND_PROXY"]:
        # Trust one reverse proxy (nginx, Caddy, ...) for client IP and https scheme.
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)
    with app.app_context():
        db.init_db()

    from . import billing
    from .auth import email_enabled, is_owner, load_current_account
    app.before_request(load_current_account)
    app.before_request(csrf_protect)
    app.before_request(billing.check_access)

    @app.context_processor
    def inject_globals():
        from flask import g
        from .services import KIND_LABELS, STATUS_LABELS
        account = g.get("account")
        return dict(csrf_token=get_csrf_token, STATUS_LABELS=STATUS_LABELS, KIND_LABELS=KIND_LABELS,
                    agency_name=account["name"] if account else "", is_owner=is_owner,
                    email_enabled=email_enabled)

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        # Portal URLs contain the access token: never leak them via Referer.
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        if app.config["SESSION_COOKIE_SECURE"]:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return resp

    @app.template_filter("filesize")
    def filesize(n):
        n = int(n or 0)
        for unit in ("B", "KB", "MB"):
            if n < 1024 or unit == "MB":
                return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
            n /= 1024

    for code in (400, 401, 403, 404, 410, 429):
        app.register_error_handler(
            code, lambda e, code=code: (render_template("error.html", error=e, code=code), code))

    @app.errorhandler(413)
    def too_large(e):
        """Send oversized uploads back to the page they came from with a clear message."""
        from flask import flash, redirect, request, url_for
        limit = app.config["MAX_UPLOAD_BYTES"] // (1024 * 1024)
        args = request.view_args or {}
        if request.blueprint == "portal" and "token" in args:
            flash(f"That file is too large. The maximum size is {limit} MB.", "portal-error")
            return redirect(url_for("portal.home", token=args["token"])
                            + (f"#task-{args['task_id']}" if "task_id" in args else ""), 303)
        return render_template("error.html", error=e, code=413), 413

    from . import legal, public
    from .auth import bp as auth_bp, set_password_command
    from .owner import bp as owner_bp
    from .portal import bp as portal_bp
    app.register_blueprint(public.bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(owner_bp)
    app.register_blueprint(portal_bp)
    app.register_blueprint(billing.bp)
    app.register_blueprint(legal.bp)
    app.cli.add_command(set_password_command)
    billing.register_cli(app)
    return app
