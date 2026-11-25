"""Follow-up engine: decides invoice state and which reminder is due, from dates alone.

Pure functions with no database or Flask access, so the rules are easy to test.
"""
from dataclasses import dataclass
from datetime import date, timedelta

DUE_SOON_DAYS = 7
# Never recommend two reminders closer together than this, even if the
# owner is catching up on an invoice that is already weeks overdue.
MIN_GAP_DAYS = 3


@dataclass(frozen=True)
class Stage:
    number: int
    day: int  # days overdue when this reminder becomes due
    label: str
    key: str


STAGES = (
    Stage(1, 1, "Friendly reminder", "friendly"),
    Stage(2, 4, "Second reminder", "second"),
    Stage(3, 8, "Stronger reminder", "firm"),
    Stage(4, 14, "Final reminder", "final"),
)


@dataclass(frozen=True)
class Recommendation:
    stage: Stage
    due_on: date
    is_due: bool


def display_state(status, due_date, today):
    """One of draft, sent, due_soon, overdue, paid."""
    if status in ("draft", "paid"):
        return status
    days_left = (due_date - today).days
    if days_left < 0:
        return "overdue"
    if days_left <= DUE_SOON_DAYS:
        return "due_soon"
    return "sent"


def days_overdue(due_date, today):
    return max(0, (today - due_date).days)


def _stage_date(stage, due_date, previous_action_on):
    planned = due_date + timedelta(days=stage.day)
    if previous_action_on is not None:
        planned = max(planned, previous_action_on + timedelta(days=MIN_GAP_DAYS))
    return planned


def next_followup(status, due_date, history, today):
    """The next reminder for an invoice, or None if no more follow-ups apply.

    history: iterable of (stage_number, on_date) for reminders already sent or skipped.
    Only invoices that have been sent and are unpaid get follow-ups.
    """
    if status != "sent":
        return None
    history = list(history)
    done = max((s for s, _ in history), default=0)
    if done >= len(STAGES):
        return None
    last_on = max((d for _, d in history), default=None)
    stage = STAGES[done]
    due_on = _stage_date(stage, due_date, last_on)
    return Recommendation(stage, due_on, due_on <= today)


def planned_schedule(status, due_date, history, today):
    """Projected date for every remaining stage (used by the invoice timeline)."""
    rec = next_followup(status, due_date, history, today)
    if rec is None:
        return {}
    plan = {rec.stage.number: rec.due_on}
    # Later stages can't happen before the next one is sent, which is today at the earliest.
    prev = max(rec.due_on, today)
    for stage in STAGES[rec.stage.number:]:
        prev = _stage_date(stage, due_date, prev)
        plan[stage.number] = prev
    return plan
