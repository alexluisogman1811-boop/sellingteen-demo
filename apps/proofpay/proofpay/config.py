"""Application configuration, read from environment variables.

Nothing secret is hard-coded here. See .env.example for every variable.
"""
import logging
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger(__name__)

# The project's .env (next to the proofpay package). It must be loaded here, before the Config
# class below reads os.environ: that happens on `import proofpay`, so a load inside create_app()
# came too late under waitress/gunicorn. override=False: real environment variables always win.
# Tests set PROOFPAY_IGNORE_DOTENV=1 so a developer's own .env can't change their results.
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
if os.environ.get("PROOFPAY_IGNORE_DOTENV") != "1":
    load_dotenv(ENV_FILE, override=False)


def _bool(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def load_config() -> dict:
    """Read every setting from the environment NOW (called inside create_app()).

    The hosting setup gives each app its own environment variables only while create_app() runs,
    so nothing may be read from os.environ at import time.
    """
    APP_ENV = os.environ.get("APP_ENV", "development")

    SECRET_KEY = os.environ.get("SECRET_KEY") or None

    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL") or "sqlite:///proofpay.db"
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    APP_BASE_URL = (os.environ.get("APP_BASE_URL") or "").rstrip("/")

    # Uploads
    UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER") or None  # default: <instance>/uploads
    MAX_PHOTO_BYTES = int(os.environ.get("MAX_PHOTO_MB", "8")) * 1024 * 1024
    # Whole-request ceiling (several photos per upload). Werkzeug rejects larger bodies with 413.
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_UPLOAD_MB", "40")) * 1024 * 1024
    MAX_PHOTOS_PER_JOB = int(os.environ.get("MAX_PHOTOS_PER_JOB", "30"))

    # AI
    AI_MOCK = _bool("AI_MOCK", "1")
    AI_API_KEY = os.environ.get("AI_API_KEY") or None
    AI_MODEL = os.environ.get("AI_MODEL") or "claude-haiku-5-5"  # low-cost default
    AI_TIMEOUT_SECONDS = float(os.environ.get("AI_TIMEOUT_SECONDS", "30"))

    # Payments: "manual" is the only provider in the MVP.
    PAYMENT_PROVIDER = os.environ.get("PAYMENT_PROVIDER", "manual")

    DEMO_MODE = _bool("DEMO_MODE", "1")

    # Free trial and Pro subscription (see billing.py). BILLING_ENABLED=0 gives everyone Pro.
    BILLING_ENABLED = _bool("BILLING_ENABLED", "1")
    TRIAL_DAYS = int(os.environ.get("TRIAL_DAYS", "30") or 30)
    PAYMENT_LINK = (os.environ.get("PAYMENT_LINK") or "").strip()
    PRICE_TEXT = (os.environ.get("PRICE_TEXT") or "$12 per month").strip()
    SUPPORT_EMAIL = (os.environ.get("SUPPORT_EMAIL") or "").strip()
    LEGAL_NAME = (os.environ.get("LEGAL_NAME") or "sellingteen").strip()
    FREE_ACTIVE_JOBS = int(os.environ.get("FREE_ACTIVE_JOBS", "3") or 3)

    # Sessions / cookies
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", "0")
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 24 * 14
    REMEMBER_COOKIE_DURATION = 60 * 60 * 24 * 30  # "Keep me logged in" lasts 30 days

    # Basic brute-force protection on login (per IP + email, in-process memory).
    LOGIN_MAX_FAILURES = int(os.environ.get("LOGIN_MAX_FAILURES", "10"))
    LOGIN_LOCKOUT_SECONDS = int(os.environ.get("LOGIN_LOCKOUT_SECONDS", "900"))

    WTF_CSRF_TIME_LIMIT = None  # tie CSRF token to the session, not a clock

    return {k: v for k, v in locals().items() if k.isupper()}


class Config:
    @classmethod
    def finalize(cls, app_config: dict) -> None:
        """Fill in SECRET_KEY safely. Production refuses to start without one."""
        if app_config.get("SECRET_KEY"):
            return
        if app_config.get("APP_ENV") == "production":
            raise RuntimeError("SECRET_KEY must be set in production.")
        app_config["SECRET_KEY"] = secrets.token_hex(32)
        log.warning("SECRET_KEY not set; using a random key for this process. "
                    "Sessions will not survive a restart.")
