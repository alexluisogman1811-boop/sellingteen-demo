"""Pure availability engine (no Flask, no database).

A slot is bookable when, in the business timezone, it:
  * exists on the wall clock (not skipped by a DST change),
  * is in the future and respects minimum notice / maximum advance,
  * lies fully inside a weekly availability window that is itself clipped to
    that day's opening hours,
  * does not overlap a blocked period,
  * keeps ``buffer_minutes`` clear of every other active booking.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .timeutil import local_to_utc


@dataclass
class Schedule:
    tz: ZoneInfo
    opening: dict = field(default_factory=dict)   # weekday -> (open, close) or None
    windows: dict = field(default_factory=dict)   # weekday -> [(start, end), ...]
    blocks: list = field(default_factory=list)    # [(start_utc, end_utc)]
    busy: list = field(default_factory=list)      # [(start_utc, end_utc)]
    slot_interval: int = 15
    buffer_minutes: int = 0
    min_notice_minutes: int = 0
    max_advance_days: int = 60


def bookable_windows(schedule, day):
    """Availability windows for ``day`` clipped to its opening hours.

    Overlapping or touching windows are merged, so 09:00-12:00 + 12:00-17:00
    behaves like 09:00-17:00 and a slot may span the join."""
    hours = schedule.opening.get(day.weekday())
    if not hours:
        return []
    open_t, close_t = hours
    result = []
    for start, end in sorted(schedule.windows.get(day.weekday(), [])):
        s, e = max(start, open_t), min(end, close_t)
        if s >= e:
            continue
        if result and s <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], e))
        else:
            result.append((s, e))
    return result


def last_bookable_day(schedule, now):
    return now.astimezone(schedule.tz).date() + timedelta(days=schedule.max_advance_days)


def check_slot(schedule, duration_minutes, start_local, now, enforce_notice=True):
    """Return None if the slot is bookable, otherwise a human-readable reason."""
    start_utc = local_to_utc(start_local, schedule.tz)
    if start_utc is None:
        return "That time does not exist in the business timezone (daylight saving change)."
    if start_utc <= now:
        return "That time is in the past."
    if enforce_notice:
        if start_utc < now + timedelta(minutes=schedule.min_notice_minutes):
            return "That time is too soon; please allow more advance notice."
        if start_local.date() > last_bookable_day(schedule, now):
            return "That date is too far in advance."

    end_utc = start_utc + timedelta(minutes=duration_minutes)
    end_local = end_utc.astimezone(schedule.tz).replace(tzinfo=None)
    if end_local.date() != start_local.date():
        return "That time is outside our available hours."
    fits = any(
        ws <= start_local.time() and end_local.time() <= we
        for ws, we in bookable_windows(schedule, start_local.date())
    )
    if not fits:
        return "That time is outside our available hours."

    for b_start, b_end in schedule.blocks:
        if start_utc < b_end and end_utc > b_start:
            return "That time is unavailable."

    buffer = timedelta(minutes=schedule.buffer_minutes)
    for b_start, b_end in schedule.busy:
        if start_utc < b_end + buffer and end_utc + buffer > b_start:
            return "That time has already been booked."
    return None


def candidate_starts(schedule, duration_minutes, day):
    step = timedelta(minutes=max(1, schedule.slot_interval))
    length = timedelta(minutes=duration_minutes)
    seen = set()
    for ws, we in bookable_windows(schedule, day):
        t = datetime.combine(day, ws)
        end = datetime.combine(day, we)
        while t + length <= end:
            if t not in seen:
                seen.add(t)
                yield t
            t += step


def slots_for_date(schedule, duration_minutes, day, now, enforce_notice=True):
    """Bookable local (naive) start times for ``day``, sorted."""
    return sorted(
        t for t in candidate_starts(schedule, duration_minutes, day)
        if check_slot(schedule, duration_minutes, t, now, enforce_notice) is None
    )


def available_dates(schedule, duration_minutes, first_day, last_day, now, enforce_notice=True):
    today = now.astimezone(schedule.tz).date()
    first_day = max(first_day, today)
    if enforce_notice:
        last_day = min(last_day, last_bookable_day(schedule, now))
    result = []
    day = first_day
    while day <= last_day:
        if slots_for_date(schedule, duration_minutes, day, now, enforce_notice):
            result.append(day)
        day += timedelta(days=1)
    return result


def validate_booking_time(schedule, duration_minutes, start_local, now, enforce_notice=True):
    """Server-side gate used when saving: the requested start must be one of
    the generated slots (so it is aligned to the slot grid) and still free."""
    reason = check_slot(schedule, duration_minutes, start_local, now, enforce_notice)
    if reason:
        return reason
    if start_local not in set(candidate_starts(schedule, duration_minutes, start_local.date())):
        return "Please choose one of the listed times."
    return None
