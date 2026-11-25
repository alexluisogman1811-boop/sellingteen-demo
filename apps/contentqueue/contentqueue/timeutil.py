"""Single source of "now" so overdue checks, calendar "today", defaults and
timestamps always agree.

Planned dates/times are stored as plain wall-clock values (YYYY-MM-DD, HH:MM)
in the business's own timezone — no UTC conversion, so daylight-saving changes
never shift a planned post. Set CONTENTQUEUE_TIMEZONE (e.g. "Europe/London")
when the computer running the app is in a different timezone from the business;
otherwise the computer's local time is used.
"""
from datetime import datetime

from flask import current_app, g, has_app_context

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo, ZoneInfoNotFoundError = None, Exception


def resolve_timezone(name):
    """Return (ZoneInfo or None, error message or None)."""
    if not name:
        return None, None
    if ZoneInfo is None:
        return None, "zoneinfo is not available on this Python."
    try:
        return ZoneInfo(name), None
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None, f"Unknown timezone “{name}” (on Windows run: pip install tzdata). Using this computer’s local time."


def _account_timezone():
    """The signed-in business's own timezone (set at sign-up / on the Account page)."""
    account = g.get("account")
    if not account or not account["timezone"]:
        return None
    cache = g.setdefault("_tz_cache", {})
    if account["timezone"] not in cache:
        cache[account["timezone"]] = resolve_timezone(account["timezone"])[0]
    return cache[account["timezone"]]


def now():
    if has_app_context():
        fixed = current_app.config.get("FIXED_NOW")  # tests only
        if fixed:
            return fixed
        tz = _account_timezone() or current_app.config.get("_TZINFO")
        if tz is not None:
            return datetime.now(tz).replace(tzinfo=None, microsecond=0)
    return datetime.now().replace(microsecond=0)


def today():
    return now().date()


def timezone_label():
    if has_app_context():
        if _account_timezone() is not None:
            return g.account["timezone"]
        if current_app.config.get("_TZINFO") is not None:
            return current_app.config["TIMEZONE"]
    return "the server’s local time"
