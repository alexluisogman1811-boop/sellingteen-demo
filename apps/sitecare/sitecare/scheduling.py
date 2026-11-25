"""Recurring schedule maths and occurrence generation.

Occurrences are generated lazily, up to a horizon, and tracked per task with
`generated_through` so repeated calls are cheap and idempotent.
"""
import calendar
from datetime import date, timedelta


def add_months(start, months, anchor_day):
    """Return the date `months` months after `start`, on `anchor_day` clamped to month end."""
    index = start.month - 1 + months
    year = start.year + index // 12
    month = index % 12 + 1
    day = min(anchor_day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def nth_due_date(start, frequency, interval_days, n):
    if frequency == "daily":
        return start + timedelta(days=n)
    if frequency == "weekly":
        return start + timedelta(weeks=n)
    if frequency == "monthly":
        # Computed from the start date each time so Jan 31 -> Feb 28 -> Mar 31 (no drift).
        return add_months(start, n, start.day)
    if frequency == "custom":
        if not interval_days or interval_days < 1:
            raise ValueError("custom frequency needs interval_days >= 1")
        return start + timedelta(days=n * interval_days)
    raise ValueError(f"unknown frequency: {frequency}")


def due_dates(start, frequency, interval_days, until, after=None):
    """Yield due dates from `start` through `until` inclusive, skipping dates <= `after`."""
    n = 0
    while True:
        due = nth_due_date(start, frequency, interval_days, n)
        if due > until:
            return
        if after is None or due > after:
            yield due
        n += 1


def period_days(frequency, interval_days):
    """Length of one schedule period, used to sanity-check completion dates."""
    return {"daily": 1, "weekly": 7, "monthly": 31}.get(frequency) or interval_days or 1


def describe_schedule(frequency, interval_days):
    if frequency == "custom":
        return f"Every {interval_days} day{'s' if interval_days != 1 else ''}"
    return {"daily": "Daily", "weekly": "Weekly", "monthly": "Monthly"}.get(frequency, frequency)


def ensure_occurrences(db, until, task_id=None, account_id=None):
    """Create pending occurrences for active tasks on active websites through `until`.

    Pass `account_id` to limit the work to one account (every web request does).
    """
    sql = (
        "SELECT t.id, t.account_id, t.start_date, t.frequency, t.interval_days, t.generated_through "
        "FROM tasks t JOIN websites w ON w.id = t.website_id "
        "WHERE t.active = 1 AND w.status = 'active'"
    )
    params = []
    if account_id is not None:
        sql += " AND t.account_id = ?"
        params.append(account_id)
    if task_id is not None:
        sql += " AND t.id = ?"
        params.append(task_id)
    changed = False
    for task in db.execute(sql, params).fetchall():
        after = date.fromisoformat(task["generated_through"]) if task["generated_through"] else None
        if after is not None and after >= until:
            continue
        start = date.fromisoformat(task["start_date"])
        rows = [
            (task["account_id"], task["id"], d.isoformat())
            for d in due_dates(start, task["frequency"], task["interval_days"], until, after)
        ]
        if rows:
            db.executemany(
                "INSERT OR IGNORE INTO occurrences (account_id, task_id, due_date) VALUES (?, ?, ?)", rows
            )
        db.execute("UPDATE tasks SET generated_through = ? WHERE id = ?", (until.isoformat(), task["id"]))
        changed = True
    if changed:
        db.commit()


def clear_pending(db, task_ids, from_date):
    """Remove pending (unrecorded) occurrences due on/after `from_date`.

    Past-due unrecorded items stay so reports keep showing them as missed; completed
    and skipped occurrences are history and are never removed here.
    """
    if not task_ids:
        return
    marks = ",".join("?" * len(task_ids))
    sql = f"DELETE FROM occurrences WHERE status = 'pending' AND task_id IN ({marks})"
    db.execute(sql + " AND due_date >= ?", [*task_ids, from_date.isoformat()])


def restart_generation(db, task_ids, today):
    """Make future generation resume from `today` (dates before today are not back-filled)."""
    if not task_ids:
        return
    marks = ",".join("?" * len(task_ids))
    db.execute(
        f"UPDATE tasks SET generated_through = ? WHERE id IN ({marks})",
        [(today - timedelta(days=1)).isoformat(), *task_ids],
    )
