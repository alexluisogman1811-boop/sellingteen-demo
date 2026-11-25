"""Small shared helpers: time, form validation, redirects, CSV safety."""
import re
from datetime import date, datetime
from urllib.parse import urlparse

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def now_iso():
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def today_iso():
    return date.today().isoformat()


def is_valid_date(value):
    if not DATE_RE.match(value or ""):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


class Form:
    """Collects cleaned values and human-readable errors from submitted data."""

    def __init__(self, data):
        self.data = data
        self.errors = []

    def text(self, name, label, max_len=200, required=False):
        value = (self.data.get(name) or "").replace("\x00", "").strip()
        if required and not value:
            self.errors.append(f"{label} is required.")
        elif len(value) > max_len:
            self.errors.append(f"{label} must be {max_len} characters or fewer.")
            value = value[:max_len]
        return value

    def date(self, name, label, required=False):
        value = self.text(name, label, 10, required)
        if value and not is_valid_date(value):
            self.errors.append(f"{label} must be a valid date (YYYY-MM-DD).")
            return ""
        return value

    def time(self, name, label):
        value = self.text(name, label, 8)
        if value and not TIME_RE.match(value):
            self.errors.append(f"{label} must be a valid time (HH:MM).")
            return ""
        return value[:5]

    def integer(self, name, label, required=False):
        raw = self.text(name, label, 12, required)
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            self.errors.append(f"{label} is invalid.")
            return None

    def checkbox(self, name):
        return self.data.get(name) in ("1", "on", "true", "yes")


def safe_next(target):
    """Only allow same-site relative redirects (prevents open redirects)."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return None
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return None
    return target


def csv_safe(value):
    """Neutralise spreadsheet formula injection in exported cells."""
    text = "" if value is None else str(value)
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def escape_like(value):
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
