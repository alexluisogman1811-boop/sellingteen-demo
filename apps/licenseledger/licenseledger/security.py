"""Authentication, CSRF, license-key hashing and log redaction.

License keys are never stored in plain text. Each key is stored as an
HMAC-SHA256 digest (keyed with a server-side secret kept outside the
database) plus a 4-character hint. The full key is shown exactly once,
in the response that issues it.
"""
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time

from flask import abort, current_app, request, session

KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I: easier to read aloud
KEY_GROUPS = 4
KEY_GROUP_LEN = 5
KEY_PATTERN = re.compile(r"\bLL(?:-[A-Z2-9]{5}){4}\b")
MIN_IMPORTED_KEY_LEN = 8
MAX_KEY_LEN = 128

# Login throttling (as in the other sellingteen apps): within a 15-minute window,
# at most 10 failures per email address and 30 per IP address.
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_FAILURES = 10
LOGIN_MAX_FAILURES_PER_IP = 30
MIN_PASSWORD_LEN = 10


# --------------------------------------------------------------------------- secrets file

def load_or_create_secrets(instance_path):
    """Load the secrets file, creating it on first run. Environment variables
    SECRET_KEY and KEY_HASH_SECRET take precedence (see create_app)."""
    os.makedirs(instance_path, exist_ok=True)
    path = os.path.join(instance_path, "secrets.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    data = {"secret_key": secrets.token_hex(32), "key_hash_secret": secrets.token_hex(32)}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return data


# --------------------------------------------------------------------------- license keys

def generate_license_code():
    """Return a random code like LL-ABCDE-FGH23-JKLMN-PQRST (100 bits of entropy)."""
    groups = (
        "".join(secrets.choice(KEY_ALPHABET) for _ in range(KEY_GROUP_LEN))
        for _ in range(KEY_GROUPS)
    )
    return "LL-" + "-".join(groups)


def normalize_key(key):
    return (key or "").strip()


def hash_key(key, secret=None):
    secret = secret if secret is not None else current_app.config["KEY_HASH_SECRET"]
    return hmac.new(secret.encode(), normalize_key(key).encode(), hashlib.sha256).hexdigest()


def key_hint(key):
    return normalize_key(key)[-4:]


def validate_imported_key(key):
    key = normalize_key(key)
    if len(key) < MIN_IMPORTED_KEY_LEN:
        return f"License key must be at least {MIN_IMPORTED_KEY_LEN} characters"
    if len(key) > MAX_KEY_LEN:
        return f"License key must be at most {MAX_KEY_LEN} characters"
    if any(ch.isspace() for ch in key):
        return "License key must not contain spaces"
    return None


# --------------------------------------------------------------------------- CSRF

def csrf_token():
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


def check_csrf():
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return
    expected = session.get("_csrf")
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
    if not expected or not sent or not hmac.compare_digest(expected, sent):
        abort(400, description="Missing or invalid CSRF token. Reload the page and try again.")


# --------------------------------------------------------------------------- login throttling

def _failures():
    return current_app.extensions.setdefault("login_failures", {})


def _recent(key):
    cutoff = time.monotonic() - LOGIN_WINDOW_SECONDS
    attempts = [t for t in _failures().get(key, []) if t > cutoff]
    _failures()[key] = attempts
    return attempts


def login_locked(ip, email=""):
    return (len(_recent(("email", email))) >= LOGIN_MAX_FAILURES
            or len(_recent(("ip", ip))) >= LOGIN_MAX_FAILURES_PER_IP)


def record_login_failure(ip, email=""):
    now = time.monotonic()
    for key in (("email", email), ("ip", ip)):
        _failures().setdefault(key, []).append(now)


def clear_login_failures(ip, email=""):
    _failures().pop(("email", email), None)


# --------------------------------------------------------------------------- logging

class RedactLicenseKeys(logging.Filter):
    """Defence in depth: scrub anything that looks like a generated key from log records."""

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed record
            return True
        if KEY_PATTERN.search(message):
            record.msg = KEY_PATTERN.sub("LL-[REDACTED]", message)
            record.args = ()
        if record.exc_info and not record.exc_text:
            # Pre-format the traceback so handlers reuse this redacted text.
            text = logging.Formatter().formatException(record.exc_info)
            record.exc_text = KEY_PATTERN.sub("LL-[REDACTED]", text)
        elif record.exc_text:
            record.exc_text = KEY_PATTERN.sub("LL-[REDACTED]", record.exc_text)
        return True


def install_log_redaction(app):
    redactor = RedactLicenseKeys()
    for logger in (app.logger, logging.getLogger("werkzeug"), logging.getLogger()):
        if not any(isinstance(f, RedactLicenseKeys) for f in logger.filters):
            logger.addFilter(redactor)
        for handler in logger.handlers:
            if not any(isinstance(f, RedactLicenseKeys) for f in handler.filters):
                handler.addFilter(redactor)
