"""Date/time helpers. Rule of thumb used across the app:

* absolute instants are timezone-aware UTC datetimes (stored as ISO strings);
* wall-clock values are naive datetimes meaning "local time in the business
  timezone" and are only converted through the helpers below.
"""
import re
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from flask import current_app, has_app_context

UTC = timezone.utc
# [0-9] rather than \d: \d also matches non-ASCII digits such as fullwidth ones.
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_TIME_RE = re.compile(r"^[0-9]{2}:[0-9]{2}$")


def now_utc():
    """Current instant; tests can freeze it with app.config['NOW']."""
    if has_app_context():
        fixed = current_app.config.get("NOW")
        if fixed is not None:
            return fixed() if callable(fixed) else fixed
    return datetime.now(UTC)


def get_tz(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return ZoneInfo("UTC")


def is_valid_timezone(name):
    return isinstance(name, str) and name in _timezones()


_TZ_CACHE = None


def _timezones():
    global _TZ_CACHE
    if _TZ_CACHE is None:
        _TZ_CACHE = sorted(
            tz for tz in available_timezones()
            if "/" in tz and not tz.startswith(("Etc/", "SystemV/", "posix/", "right/"))
        ) + ["UTC"]
    return _TZ_CACHE


def timezone_choices():
    return list(_timezones())


def to_iso(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(value):
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def local_to_utc(naive, tz):
    """Convert a local wall-clock time to UTC, or None if it does not exist
    (skipped by a daylight-saving jump). Ambiguous times use the first
    occurrence (fold=0)."""
    aware = naive.replace(tzinfo=tz, fold=0)
    as_utc = aware.astimezone(UTC)
    if as_utc.astimezone(tz).replace(tzinfo=None) != naive:
        return None
    return as_utc


def local_to_utc_lenient(naive, tz):
    """Like local_to_utc but never fails; used for block boundaries."""
    return naive.replace(tzinfo=tz, fold=0).astimezone(UTC)


def utc_to_local(dt, tz):
    return dt.astimezone(tz)


def today_local(tz, now=None):
    return (now or now_utc()).astimezone(tz).date()


def parse_date(value):
    if not isinstance(value, str) or not _DATE_RE.match(value.strip()):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def parse_time(value):
    if not isinstance(value, str) or not _TIME_RE.match(value.strip()):
        return None
    try:
        return time.fromisoformat(value.strip())
    except ValueError:
        return None


def fmt_time(t):
    return t.strftime("%H:%M")
