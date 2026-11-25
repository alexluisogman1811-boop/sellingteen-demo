"""Follow-up date logic: what counts as today, overdue, upcoming and unscheduled.

All "today" decisions use the business's local date (APP_TIMEZONE), never the
server's clock or UTC, so a lead due today is never shown as overdue just
because UTC has already rolled over (or not yet).

Won and Lost leads are closed, so they never appear as needing a follow-up,
even if an old follow-up date is still stored on them. Changing the status
back to New/Contacted/Interested reopens them.
"""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import current_app

SOURCES = ["Website", "Phone", "Facebook", "LINE", "WhatsApp", "Referral", "Other"]
STATUSES = ["New", "Contacted", "Interested", "Won", "Lost"]
OPEN_STATUSES = ("New", "Contacted", "Interested")
CLOSED_STATUSES = ("Won", "Lost")
CONTACT_METHODS = ["Phone call", "LINE", "WhatsApp", "Facebook", "Email", "SMS", "In person", "Other"]
NOTE_METHOD = "Note"  # an internal note, not a contact with the customer

# Sanity bounds for any date a user can enter.
MIN_DATE = date(2000, 1, 1)
MAX_DATE = date(2100, 12, 31)

_CLOSED_SQL = "('Won', 'Lost')"

# SQL fragments (parameterised with :today) used for counting and filtering.
# Every open lead is in exactly one of overdue/today/upcoming/unscheduled.
STATE_SQL = {
    "overdue": f"next_follow_up IS NOT NULL AND next_follow_up < :today AND status NOT IN {_CLOSED_SQL}",
    "today": f"next_follow_up = :today AND status NOT IN {_CLOSED_SQL}",
    "upcoming": f"next_follow_up > :today AND status NOT IN {_CLOSED_SQL}",
    "unscheduled": f"next_follow_up IS NULL AND status NOT IN {_CLOSED_SQL}",
    "closed": f"status IN {_CLOSED_SQL}",
}
FOLLOWUP_STATES = list(STATE_SQL)


def business_date(now_utc: datetime, tz_name: str) -> date:
    """The calendar date at the business's location for a given UTC instant."""
    return now_utc.astimezone(ZoneInfo(tz_name)).date()


def today() -> date:
    fixed = current_app.config.get("FIXED_TODAY")
    if fixed:
        return date.fromisoformat(fixed)
    return business_date(datetime.now(timezone.utc), current_app.config["APP_TIMEZONE"])


def parse_date(value):
    """Return a date for a YYYY-MM-DD string, None for blank, raise ValueError otherwise."""
    value = (value or "").strip()
    if not value:
        return None
    parsed = date.fromisoformat(value)
    if len(value) != 10 or not (MIN_DATE <= parsed <= MAX_DATE):
        raise ValueError("date out of range")
    return parsed


def followup_state(lead, on: date | None = None) -> str:
    if lead["status"] in CLOSED_STATUSES:
        return "closed"
    if not lead["next_follow_up"]:
        return "unscheduled"
    on = on or today()
    due = date.fromisoformat(lead["next_follow_up"])
    if due < on:
        return "overdue"
    if due == on:
        return "today"
    return "upcoming"


def days_overdue(lead, on: date | None = None) -> int:
    if followup_state(lead, on) != "overdue":
        return 0
    return ((on or today()) - date.fromisoformat(lead["next_follow_up"])).days


def due_label(lead, on: date | None = None) -> str:
    """Human label such as 'Due today', 'Tomorrow', '3 days overdue'."""
    on = on or today()
    state = followup_state(lead, on)
    if state == "closed":
        return f"Closed ({lead['status']})"
    if state == "unscheduled":
        return "No follow-up date"
    delta = (date.fromisoformat(lead["next_follow_up"]) - on).days
    if delta == 0:
        return "Due today"
    if delta == 1:
        return "Tomorrow"
    if delta == -1:
        return "1 day overdue"
    if delta < 0:
        return f"{-delta} days overdue"
    if delta < 7:
        return f"In {delta} days"
    return date.fromisoformat(lead["next_follow_up"]).strftime("%a %d %b")


PRESETS = {"today": 0, "tomorrow": 1, "3days": 3, "week": 7, "2weeks": 14}


def preset_date(preset: str) -> date | None:
    if preset in PRESETS:
        return today() + timedelta(days=PRESETS[preset])
    return None
