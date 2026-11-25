"""Pure scheduling logic: time parsing, durations, overlaps, availability.

Nothing in this module touches the database or Flask, so it is easy to test.

Overnight rule: a shift whose end time is earlier than its start time ends on
the following day (e.g. 22:00-06:00 is an 8 hour shift). The shift form makes
the user confirm this explicitly with an "Ends next day" checkbox.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WEEKDAYS_SHORT = [d[:3] for d in WEEKDAYS]


def parse_time(value: str | None) -> time | None:
    """Parse 'HH:MM' (24h). Returns None if invalid."""
    if not value:
        return None
    value = value.strip()
    # Browsers may submit HH:MM:SS for <input type=time step=...>; seconds are dropped.
    if len(value) == 8 and value[5] == ":":
        secs = value[6:]
        if not (secs.isdigit() and int(secs) < 60):
            return None
        value = value[:5]
    m = TIME_RE.match(value)
    if not m:
        return None
    return time(int(m.group(1)), int(m.group(2)))


def parse_date(value: str | None) -> date | None:
    """Parse 'YYYY-MM-DD'. Returns None if invalid or outside a sane range."""
    if not value:
        return None
    try:
        d = date.fromisoformat(value.strip())
    except ValueError:
        return None
    if not (2000 <= d.year <= 2100):
        return None
    return d


def is_overnight(start: str, end: str) -> bool:
    return parse_time(end) <= parse_time(start)


def shift_interval(shift_date: date, start: str, end: str) -> tuple[datetime, datetime]:
    """Absolute [start, end) datetimes for a shift; end rolls to next day if needed."""
    s = datetime.combine(shift_date, parse_time(start))
    e = datetime.combine(shift_date, parse_time(end))
    if e <= s:
        e += timedelta(days=1)
    return s, e


def shift_hours(shift_date: date, start: str, end: str) -> float:
    s, e = shift_interval(shift_date, start, end)
    return round((e - s).total_seconds() / 3600, 2)


def intervals_overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """Half-open intervals: a shift ending at 17:00 does not overlap one starting at 17:00."""
    return a_start < b_end and b_start < a_end


def find_overlaps(shift_date: date, start: str, end: str, others) -> list:
    """Return the items in `others` (mappings with shift_date/start_time/end_time) that overlap."""
    s, e = shift_interval(shift_date, start, end)
    hits = []
    for other in others:
        od = parse_date(other["shift_date"])
        os_, oe = shift_interval(od, other["start_time"], other["end_time"])
        if intervals_overlap(s, e, os_, oe):
            hits.append(other)
    return hits


def availability_window(day: date, start: str, end: str) -> tuple[datetime, datetime]:
    """Availability window for a given day. Equal start/end means 24 hours from start."""
    s = datetime.combine(day, parse_time(start))
    e = datetime.combine(day, parse_time(end))
    if e <= s:
        e += timedelta(days=1)
    return s, e


def availability_warnings(shift_date: date, start: str, end: str, *, employee_name: str,
                          track_availability: bool, availability: dict) -> list[str]:
    """Warnings (not errors) when a shift falls outside an employee's recorded availability.

    `availability` maps weekday (0=Monday) -> (start, end). When availability is
    tracked, a weekday missing from the map means the employee is unavailable that day.
    """
    if not track_availability:
        return []
    wd = shift_date.weekday()
    if wd not in availability:
        return [f"{employee_name} is not available on {WEEKDAYS[wd]}s."]
    a_start, a_end = availability[wd]
    ws, we = availability_window(shift_date, a_start, a_end)
    s, e = shift_interval(shift_date, start, end)
    if s < ws or e > we:
        return [f"Shift is outside {employee_name}'s availability on {WEEKDAYS[wd]}s "
                f"({a_start}–{a_end})."]
    return []


def week_start(d: date) -> date:
    """Monday of the week containing d."""
    return d - timedelta(days=d.weekday())


def week_days(monday: date) -> list[date]:
    return [monday + timedelta(days=i) for i in range(7)]


def total_hours(shifts) -> float:
    return round(sum(shift_hours(parse_date(s["shift_date"]), s["start_time"], s["end_time"])
                     for s in shifts), 2)


def hours_by_employee(shifts) -> dict:
    """Sum hours per employee_id (None = unassigned). Overnight shifts count toward their start date."""
    totals: dict = {}
    for s in shifts:
        h = shift_hours(parse_date(s["shift_date"]), s["start_time"], s["end_time"])
        totals[s["employee_id"]] = round(totals.get(s["employee_id"], 0) + h, 2)
    return totals
