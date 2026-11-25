import os
from datetime import timedelta

WEAK_SECRETS = {"", "change-me-to-a-long-random-string", "dev", "secret"}


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "")
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL", "")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_UPLOAD_BYTES", 1024 * 1024))
    CSV_MAX_ROWS = 2000

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = False
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = timedelta(hours=12)

    WTF_CSRF_TIME_LIMIT = 60 * 60 * 4

    LOGIN_MAX_FAILURES = 5
    LOGIN_LOCKOUT_MINUTES = 15
    INVITE_VALID_DAYS = 7
    LEADS_PER_PAGE = 25

    # "Today" for follow-ups, overdue checks and date validation is computed in
    # this time zone, not the server's (servers usually run in UTC).
    APP_TIMEZONE = "America/Chicago"

    # Message drafting. "template" is free and works offline. Other names only
    # work once a matching provider is registered in app/drafting/providers.py.
    DRAFT_PROVIDER = os.environ.get("DRAFT_PROVIDER", "template")

    # Pilot settings. If SIGNUP_ACCESS_CODE is set, new workspaces can only be
    # created with that code (invited users joining by link don't need it).
    SIGNUP_ACCESS_CODE = ""
    # Shown on the privacy notice and terms pages.
    LEGAL_ENTITY_NAME = ""
    SUPPORT_EMAIL = ""
    # How long the host keeps database backups; stated on the privacy notice.
    BACKUP_RETENTION_DAYS = int(os.environ.get("BACKUP_RETENTION_DAYS", 7))
    # Redirect plain-HTTP requests to HTTPS (production only; needs TRUST_PROXY behind a proxy).
    FORCE_HTTPS = False

    # Free trial and subscription (see billing.py). Status belongs to the business workspace.
    BILLING_ENABLED = True
    TRIAL_DAYS = 14
    PRICE_TEXT = "$29 per month (founder price, locked for life for the first 10 roofers; regular $49)"
    PAYMENT_LINK = ""
    # Public address of this app, e.g. https://you.pythonanywhere.com/leadrescue (used in CLI-printed links).
    APP_BASE_URL = ""
    # Run `db upgrade` automatically when the app starts (for hosts without a deploy step).
    AUTO_MIGRATE = False
    PASSWORD_RESET_HOURS = 24


class DevelopmentConfig(Config):
    DEBUG = False


class ProductionConfig(Config):
    SESSION_COOKIE_SECURE = True
    REMEMBER_COOKIE_SECURE = True
    PREFERRED_URL_SCHEME = "https"
    FORCE_HTTPS = True


class TestingConfig(Config):
    TESTING = True
    SECRET_KEY = "test-secret-key-not-for-production-0123456789"
    SQLALCHEMY_DATABASE_URI = os.environ.get("TEST_DATABASE_URL", "sqlite:///:memory:")
    SQLALCHEMY_ENGINE_OPTIONS = {}
    # CSRF stays ON in tests; the test client fetches real tokens.
    WTF_CSRF_ENABLED = True


CONFIGS = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
}
