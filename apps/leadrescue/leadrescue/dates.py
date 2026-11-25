"""Business-local dates.

Servers normally run in UTC, but roofers work in US local time. Without this,
at 7 pm in Texas the server is already on "tomorrow": follow-ups would show as
overdue a day early and "today" defaults would be wrong.
"""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import current_app, g, has_app_context, has_request_context

DEFAULT_TZ = "America/Chicago"

# Offered in Settings. Any valid IANA name also works if set directly.
US_TIMEZONES = [
    ("America/New_York", "Eastern"),
    ("America/Chicago", "Central"),
    ("America/Denver", "Mountain"),
    ("America/Phoenix", "Arizona (no daylight saving)"),
    ("America/Los_Angeles", "Pacific"),
    ("America/Anchorage", "Alaska"),
    ("Pacific/Honolulu", "Hawaii"),
    ("America/Puerto_Rico", "Puerto Rico"),
]


def app_tz() -> ZoneInfo:
    """The active workspace's time zone, else the app default (APP_TIMEZONE)."""
    if has_request_context():
        business = g.get("business")
        name = getattr(business, "timezone", None) if business is not None else None
        if name:
            try:
                return ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError):
                pass
    name = current_app.config.get("APP_TIMEZONE", DEFAULT_TZ) if has_app_context() else DEFAULT_TZ
    return ZoneInfo(name)


def local_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(app_tz())


def local_today() -> date:
    return local_now().date()


def to_local(value: datetime | None) -> datetime | None:
    """Convert a stored timestamp (UTC; SQLite returns it naive) to local time."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(app_tz())
