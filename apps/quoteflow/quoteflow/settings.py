"""Settings that are read while a request is being handled.

The sellingteen host (combined_wsgi.py) gives each app its environment variables only while the
app is being created, then restores the original environment. Anything read from os.environ
later - during a request - would be missing there (for example AI_MOCK, so every quote would try
the paid AI). create_app() therefore stores a snapshot of these values, and get() falls back to
it. A variable that is set in the live environment still wins, so a stand-alone deployment and
the tests behave exactly as before.
"""
import os

from flask import current_app, has_app_context

RUNTIME_KEYS = ("AI_MOCK", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "ADMIN_EMAILS", "SUPPORT_EMAIL",
                "STRIPE_SECRET_KEY", "STRIPE_PRICE_ID", "STRIPE_WEBHOOK_SECRET", "APP_BASE_URL")


def snapshot():
    return {k: os.environ[k] for k in RUNTIME_KEYS if k in os.environ}


def get(name, default=None):
    if name in os.environ:
        return os.environ[name]
    if has_app_context():
        return current_app.config.get("RUNTIME_ENV", {}).get(name, default)
    return default
