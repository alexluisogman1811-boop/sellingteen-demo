"""Pure date helpers for the month and week calendar views (weeks start Monday)."""
import calendar
from datetime import date, timedelta

MIN_YEAR, MAX_YEAR = 1970, 2200


def parse_anchor(value, today):
    """Parse the ?date= query parameter, falling back to today for bad input."""
    try:
        parsed = date.fromisoformat((value or "").strip())
    except ValueError:
        return today
    if not MIN_YEAR <= parsed.year <= MAX_YEAR:
        return today
    return parsed


def shift_month(d, months):
    """Move by whole months, clamping the day (31 Jan + 1 month -> 28/29 Feb)."""
    years, month_index = divmod(d.month - 1 + months, 12)
    year = d.year + years
    month = month_index + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def month_grid(year, month):
    """Full weeks (Mon-Sun) covering the month, including spill-over days."""
    return calendar.Calendar(firstweekday=0).monthdatescalendar(year, month)


def week_start(d):
    return d - timedelta(days=d.weekday())


def week_days(d):
    start = week_start(d)
    return [start + timedelta(days=i) for i in range(7)]


def view_range(view, anchor):
    """Return (weeks, start, end, prev_anchor, next_anchor, title) for a view."""
    if view == "week":
        days = week_days(anchor)
        weeks = [days]
        prev_anchor = anchor - timedelta(days=7)
        next_anchor = anchor + timedelta(days=7)
        start, end = days[0], days[-1]
        if start.year == end.year:
            title = f"{start.strftime('%d %b')} – {end.strftime('%d %b %Y')}"
        else:
            title = f"{start.strftime('%d %b %Y')} – {end.strftime('%d %b %Y')}"
    else:
        weeks = month_grid(anchor.year, anchor.month)
        prev_anchor = shift_month(anchor, -1)
        next_anchor = shift_month(anchor, 1)
        start, end = weeks[0][0], weeks[-1][-1]
        title = anchor.strftime("%B %Y")
    # Keep navigation inside the supported range.
    if prev_anchor.year < MIN_YEAR:
        prev_anchor = anchor
    if next_anchor.year > MAX_YEAR:
        next_anchor = anchor
    return weeks, start, end, prev_anchor, next_anchor, title
