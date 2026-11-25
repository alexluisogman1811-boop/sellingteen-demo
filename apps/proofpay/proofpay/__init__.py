"""ProofPay application factory."""
import logging
import os
import re

from flask import Flask, render_template, request
from flask_login import LoginManager
from flask_wtf.csrf import CSRFError, CSRFProtect
from sqlalchemy import event, inspect, text
from werkzeug.exceptions import HTTPException

from . import mailer
from .config import Config, load_config  # importing config loads .env first (see config.py)
from .models import User, db

csrf = CSRFProtect()
login_manager = LoginManager()
log = logging.getLogger("proofpay")


def create_app(overrides: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    # Every setting is read from the environment here, at create_app() time (see config.py).
    app.config.update(load_config())
    app.config["MAIL"] = mailer.mail_settings()  # optional email; off unless SMTP is set up
    if overrides:
        app.config.update(overrides)
    Config.finalize(app.config)

    os.makedirs(app.instance_path, exist_ok=True)
    if not app.config.get("UPLOAD_FOLDER"):
        app.config["UPLOAD_FOLDER"] = os.path.join(app.instance_path, "uploads")
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Customer share links are bearer secrets; keep them out of request logs.
    for name in ("werkzeug", "proofpay", "gunicorn.access"):
        logging.getLogger(name).addFilter(_redact_share_tokens)

    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Please log in to continue."
    # "basic": a changed IP/user-agent marks the session non-fresh instead of logging out
    # (strong mode logs users out on every browser auto-update). Logout still revokes all
    # sessions via User.session_version.
    login_manager.session_protection = "basic"

    from . import auth, billing, jobs, customers, photos, public, demo, legal
    app.register_blueprint(public.bp)
    app.register_blueprint(auth.bp)
    app.register_blueprint(jobs.bp)
    app.register_blueprint(customers.bp)
    app.register_blueprint(photos.bp)
    app.register_blueprint(demo.bp)
    app.register_blueprint(billing.bp)
    app.register_blueprint(legal.bp)
    demo.register_cli(app)
    billing.register_cli(app)
    # Works out the plan (trial / Pro / Free) for the trial bar and plan limits. Never blocks a page.
    app.before_request(billing.load_access)

    from .helpers import register_template_helpers
    register_template_helpers(app)

    _register_error_handlers(app)
    _register_security_headers(app)

    with app.app_context():
        if db.engine.dialect.name == "sqlite":
            event.listen(db.engine, "connect", _sqlite_pragmas)
        db.create_all()
        _add_missing_columns()

    return app


def _add_missing_columns() -> None:
    """Safe, idempotent migration for columns added after a database was created.

    create_all() never alters existing tables. Adding a nullable column keeps every existing row.
    """
    columns = {c["name"] for c in inspect(db.engine).get_columns("businesses")}
    if "paid_until" not in columns:
        with db.engine.begin() as conn:
            conn.execute(text("ALTER TABLE businesses ADD COLUMN paid_until DATE"))


_SHARE_TOKEN_RE = re.compile(r"/p/[A-Za-z0-9_-]{20,64}")


def _redact_share_tokens(record: logging.LogRecord) -> bool:
    message = record.getMessage()
    if "/p/" in message:
        record.msg = _SHARE_TOKEN_RE.sub("/p/[redacted]", message)
        record.args = ()
    return True


def _sqlite_pragmas(dbapi_conn, _record):
    # SQLite ignores foreign keys unless asked.
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


@login_manager.user_loader
def load_user(user_key: str):
    # Session key is "<id>:<session_version>"; a bumped version logs out old sessions.
    try:
        user_id, version = (int(x) for x in user_key.split(":", 1))
    except (ValueError, AttributeError):
        return None
    user = db.session.get(User, user_id)
    if user is None or user.session_version != version:
        return None
    return user


def _register_error_handlers(app: Flask) -> None:
    messages = {
        400: "That request didn't look right. Please try again.",
        403: "You don't have access to that page.",
        404: "We couldn't find that page.",
        405: "That action isn't allowed here.",
        413: "That upload is too large. Please choose smaller photos.",
    }

    @app.errorhandler(CSRFError)
    def csrf_error(e):
        log.warning("CSRF failure on %s: %s", request.path, e.description)
        return render_template("error.html", code=400,
                               message="Your form expired. Please go back, refresh the page and try again."), 400

    @app.errorhandler(HTTPException)
    def http_error(e):
        code = e.code or 500
        return render_template("error.html", code=code,
                               message=messages.get(code, "Something went wrong. Please try again.")), code

    @app.errorhandler(Exception)
    def unhandled(e):
        # Details go to the server log only; the user never sees a stack trace.
        log.exception("Unhandled error on %s %s", request.method, request.path)
        db.session.rollback()
        return render_template("error.html", code=500,
                               message="Something went wrong. Please try again."), 500


def _register_security_headers(app: Flask) -> None:
    @app.after_request
    def headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        return resp
