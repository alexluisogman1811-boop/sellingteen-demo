"""Server-side form validation. Each validator returns (clean_data, errors)."""
from __future__ import annotations

import re
from datetime import time

from .scheduling import WEEKDAYS, parse_date, parse_time

MIDNIGHT = time(0, 0)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^[0-9+()\-.\s]{5,30}$")

MAX_NAME = 100
MAX_ROLE = 50
MAX_NOTES = 500


def _text(form, key: str) -> str:
    return (form.get(key) or "").strip()


def validate_employee_form(form) -> tuple[dict, dict]:
    errors: dict[str, str] = {}
    data = {
        "name": _text(form, "name"),
        "email": _text(form, "email"),
        "phone": _text(form, "phone"),
        "role": " ".join(_text(form, "role").split()),
        "notes": _text(form, "notes"),
        "track_availability": form.get("track_availability") == "1",
        "availability": {},
    }
    if not data["name"]:
        errors["name"] = "Name is required."
    elif len(data["name"]) > MAX_NAME:
        errors["name"] = f"Name must be {MAX_NAME} characters or fewer."
    if data["email"] and (len(data["email"]) > 254 or not EMAIL_RE.match(data["email"])):
        errors["email"] = "Enter a valid email address (or leave it blank)."
    if data["phone"] and not PHONE_RE.match(data["phone"]):
        errors["phone"] = "Phone may contain digits, spaces, +, -, ( ) and . (5–30 characters)."
    if len(data["role"]) > MAX_ROLE:
        errors["role"] = f"Role must be {MAX_ROLE} characters or fewer."
    if len(data["notes"]) > MAX_NOTES:
        errors["notes"] = f"Notes must be {MAX_NOTES} characters or fewer."

    if data["track_availability"]:
        for wd, day in enumerate(WEEKDAYS):
            if form.get(f"avail_{wd}_on") != "1":
                continue
            start, end = _text(form, f"avail_{wd}_start"), _text(form, f"avail_{wd}_end")
            ts, te = parse_time(start), parse_time(end)
            if ts is None or te is None:
                errors[f"avail_{wd}"] = f"{day}: enter a valid start and end time (HH:MM)."
                continue
            data["availability"][wd] = (ts.strftime("%H:%M"), te.strftime("%H:%M"))
    return data, errors


def validate_shift_form(form, employees_by_id: dict, current_employee_id: int | None = None) -> tuple[dict, dict]:
    """Validate shift fields. Overlap checks need the database and happen in services.

    `current_employee_id` is the employee already on the shift being edited; keeping
    an inactive employee on an existing shift is allowed, newly assigning one is not.
    """
    errors: dict[str, str] = {}
    data = {
        "employee_id": None,
        "shift_date": None,
        "start_time": None,
        "end_time": None,
        "role": " ".join(_text(form, "role").split()),
        "notes": _text(form, "notes"),
        "ends_next_day": form.get("ends_next_day") == "1",
    }

    raw_emp = _text(form, "employee_id")
    employee = None
    if raw_emp:
        if not raw_emp.isdigit() or int(raw_emp) not in employees_by_id:
            errors["employee_id"] = "Choose an employee from the list, or leave the shift unfilled."
        else:
            employee = employees_by_id[int(raw_emp)]
            data["employee_id"] = employee["id"]
            if not employee["active"] and employee["id"] != current_employee_id:
                errors["employee_id"] = (f"{employee['name']} is inactive. Reactivate them on the "
                                         "Employees page or choose someone else.")

    d = parse_date(_text(form, "shift_date"))
    if d is None:
        errors["shift_date"] = "Enter a valid date (YYYY-MM-DD)."
    else:
        data["shift_date"] = d.isoformat()

    ts, te = parse_time(_text(form, "start_time")), parse_time(_text(form, "end_time"))
    if ts is None:
        errors["start_time"] = "Enter a valid start time (HH:MM, 24-hour)."
    if te is None:
        errors["end_time"] = "Enter a valid end time (HH:MM, 24-hour)."
    if ts is not None and te is not None:
        data["start_time"], data["end_time"] = ts.strftime("%H:%M"), te.strftime("%H:%M")
        if ts == te:
            errors["end_time"] = "Start and end time cannot be the same."
        elif te == MIDNIGHT:
            pass  # ending at 00:00 means "until midnight"; no checkbox needed
        elif te < ts and not data["ends_next_day"]:
            errors["end_time"] = ("End time is before start time. If this shift runs past "
                                  "midnight, tick “Ends next day”.")
        elif te > ts and data["ends_next_day"]:
            errors["end_time"] = ("“Ends next day” is ticked but the end time is later "
                                  "than the start time on the same day. Untick it or fix the times.")

    if not data["role"] and employee is not None:
        data["role"] = employee["role"]
    if not data["role"]:
        errors["role"] = "Role is required (e.g. Cashier, Cook)."
    elif len(data["role"]) > MAX_ROLE:
        errors["role"] = f"Role must be {MAX_ROLE} characters or fewer."
    if len(data["notes"]) > MAX_NOTES:
        errors["notes"] = f"Notes must be {MAX_NOTES} characters or fewer."
    return data, errors
