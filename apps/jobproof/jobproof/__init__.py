"""JobProof AI - turn job notes and photos into professional customer reports."""
import logging
import os
import secrets
import sqlite3
from datetime import timedelta

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

from . import db
from .security import install_log_redaction, register_security


class ConfigError(RuntimeError):
    """Production configuration is missing or unsafe. Messages name settings, never their values."""


MIN_SECRET_LENGTH = 32
_PLACEHOLDER_SECRETS = {"change-me", "changeme", "secret", "your-secret-key", "replace-me"}
# Windows paths are limited to 260 characters; uploads are stored as <data>\uploads\photos\<32 hex>.jpg
_WINDOWS_MAX_PATH = 259
_UPLOAD_SUFFIX_LEN = len(os.path.join("uploads", "photos", "0" * 32 + ".jpg")) + 1


def is_production():
    return os.environ.get("JOBPROOF_ENV", "").strip().lower() == "production"


def _check_production_config(app):
    key = os.environ.get("JOBPROOF_SECRET_KEY", "")
    if len(key) < MIN_SECRET_LENGTH or key.strip().lower() in _PLACEHOLDER_SECRETS:
        raise ConfigError(
            f"JOBPROOF_SECRET_KEY must be set to a random value of at least {MIN_SECRET_LENGTH} characters. "
            "Generate one with: py -c \"import secrets; print(secrets.token_hex(32))\"")
    if not app.config["AI_MOCK"] and not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        raise ConfigError("AI_MOCK=0 requires ANTHROPIC_API_KEY. Set the key, or set AI_MOCK=1 to use the "
                          "built-in report writer.")
    if os.environ.get("JOBPROOF_SECURE_COOKIES", "0") not in ("0", "1"):
        raise ConfigError("JOBPROOF_SECURE_COOKIES must be 0 or 1.")


def _check_data_dir(app):
    """Fail at startup (not on the first upload) if the data folder is unusable."""
    upload_root = app.config["UPLOAD_FOLDER"]
    if os.name == "nt" and len(os.path.abspath(upload_root)) + _UPLOAD_SUFFIX_LEN > _WINDOWS_MAX_PATH:
        raise ConfigError("The data folder path is too long for Windows (260-character limit). "
                          "Set JOBPROOF_DATA_DIR to a short folder such as C:\\apps\\jobproof\\data.")
    probe = os.path.join(upload_root, "photos", ".write-test")
    try:
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
    except OSError:
        raise ConfigError("The data folder is not writable. Check JOBPROOF_DATA_DIR and folder permissions.")


def _load_secret_key(instance_path):
    """Secret key comes from the environment, or (development only) a per-install random file."""
    env_key = os.environ.get("JOBPROOF_SECRET_KEY")
    if env_key:
        return env_key
    key_file = os.path.join(instance_path, "secret_key")
    if os.path.exists(key_file):
        with open(key_file, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    key = secrets.token_hex(32)
    with open(key_file, "w", encoding="utf-8") as fh:
        fh.write(key)
    return key


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)
    data_dir = os.environ.get("JOBPROOF_DATA_DIR") or app.instance_path
    os.makedirs(data_dir, exist_ok=True)

    app.config.update(
        DATABASE=os.path.join(data_dir, "jobproof.db"),
        UPLOAD_FOLDER=os.path.join(data_dir, "uploads"),
        MAX_PHOTO_BYTES=10 * 1024 * 1024,
        MAX_LOGO_BYTES=2 * 1024 * 1024,
        MAX_PHOTOS_PER_JOB=30,
        MAX_CONTENT_LENGTH=80 * 1024 * 1024,
        AI_MOCK=os.environ.get("AI_MOCK", "1").strip() != "0",
        AI_MODEL=os.environ.get("AI_MODEL", "claude-haiku-5-5"),
        AI_TIMEOUT_SECONDS=float(os.environ.get("AI_TIMEOUT_SECONDS", "60")),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("JOBPROOF_SECURE_COOKIES", "0") == "1",
        SESSION_COOKIE_NAME="jobproof_session",
        PERMANENT_SESSION_LIFETIME=timedelta(days=14),
        CSRF_ENABLED=True,
        LOGIN_MAX_FAILURES=5,
        LOGIN_WINDOW_SECONDS=15 * 60,
        # Free trial and subscription (see billing.py). BILLING_ENABLED=0 turns it off.
        BILLING_ENABLED=os.environ.get("BILLING_ENABLED", "1") == "1",
        TRIAL_DAYS=int(os.environ.get("TRIAL_DAYS", "30") or 30),
        PAYMENT_LINK=os.environ.get("PAYMENT_LINK", "").strip(),
        PRICE_TEXT=os.environ.get("PRICE_TEXT", "$15 per month").strip(),
        SUPPORT_EMAIL=os.environ.get("SUPPORT_EMAIL", "").strip(),
        LEGAL_NAME=os.environ.get("LEGAL_NAME", "sellingteen").strip(),
    )
    # Optional email (forgot-password links only). OFF unless SMTP_HOST and SMTP_FROM are set - see mailer.py.
    from . import mailer
    app.config["MAIL"] = mailer.mail_settings()
    if test_config:
        app.config.update(test_config)
    app.config["PRODUCTION"] = is_production()
    if app.config["PRODUCTION"]:
        _check_production_config(app)
        # Production never falls back to a generated key file: the key must be fixed and managed by the operator.
        app.config["SECRET_KEY"] = os.environ["JOBPROOF_SECRET_KEY"]
        app.config["DEBUG"] = app.config["TESTING"] = False
    elif not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = _load_secret_key(app.instance_path)

    for sub in ("photos", "logos"):
        os.makedirs(os.path.join(app.config["UPLOAD_FOLDER"], sub), exist_ok=True)
    _check_data_dir(app)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    install_log_redaction(app)

    db.init_app(app)
    register_security(app)
    from . import billing
    app.before_request(billing.check_access)  # after register_security: needs g.user

    from . import account, auth, dashboard, jobs, legal, photos, profile, reports, share
    for module in (auth, account, dashboard, jobs, photos, profile, reports, share, billing, legal):
        app.register_blueprint(module.bp)

    from .demo import register_cli
    register_cli(app)
    billing.register_cli(app)

    from .models import JOB_STATUSES, PHOTO_STAGES, REPORT_STATUSES, format_date, format_datetime
    app.jinja_env.filters["date"] = format_date
    app.jinja_env.filters["datetime"] = format_datetime
    app.jinja_env.globals.update(JOB_STATUSES=JOB_STATUSES, REPORT_STATUSES=REPORT_STATUSES,
                                 PHOTO_STAGES=PHOTO_STAGES)

    @app.route("/favicon.ico")
    def favicon():
        return app.send_static_file("favicon.svg")

    @app.route("/healthz")
    def healthz():
        """Uptime check for monitoring: no login, no database, no account details."""
        return jsonify(status="ok")

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_e):
        return render_template("error.html", title="Upload too large",
                               message="That upload is too large. Each photo must be 10 MB or smaller, and one "
                                       "upload can be about 80 MB in total - try fewer photos at a time."), 413

    @app.errorhandler(sqlite3.Error)
    def database_error(e):
        app.logger.error("Database error: %s", type(e).__name__)
        return render_template("error.html", title="Something went wrong",
                               message="We couldn't reach the database. Your previous work is safe - please try again."), 500

    @app.errorhandler(HTTPException)
    def http_error(e):
        messages = {
            400: ("Please try again", "This page expired or something about the request wasn't right. "
                                      "Go back, refresh the page and try again."),
            403: ("Not allowed", "You don't have permission to do that."),
            404: ("Not found", "We couldn't find that page. It may have been deleted, or you may not have access to it."),
            405: ("Not allowed", "That action isn't available here."),
        }
        title, message = messages.get(e.code, ("Something went wrong", "Please try again."))
        return render_template("error.html", title=title, message=message), e.code

    @app.errorhandler(Exception)
    def unhandled(e):
        app.logger.error("Unhandled error on %s %s: %s", request.method, request.path, type(e).__name__)
        return render_template("error.html", title="Something went wrong",
                               message="An unexpected error occurred. Your saved work is safe - please try again."), 500

    return app
