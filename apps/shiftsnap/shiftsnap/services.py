"""Database operations. All queries use parameter binding (no string-built SQL values).

Multi-business: every row belongs to one account (business). Every query in this module is
filtered by the current account, `aid()`, which comes from the signed-in user. A record from
another account behaves exactly like a record that doesn't exist.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, timedelta

from flask import current_app, g, has_app_context, has_request_context

from . import scheduling as sch
from .db import get_db


class NoAccountError(RuntimeError):
    """Raised when data is accessed without a current account (fail closed, never 'all accounts')."""


def aid() -> int:
    """The current account id. Set from the signed-in user in auth.load_current_user."""
    account_id = g.get("account_id") if has_app_context() else None
    if account_id is None and has_app_context() and not has_request_context() and current_app.testing:
        # Test helpers that build data outside any web request. Never used while serving a page.
        account_id = current_app.config.get("TEST_ACCOUNT_ID")
    if account_id is None:
        raise NoAccountError("No current account: refusing to read or write business data.")
    return account_id


@contextmanager
def use_account(account_id: int):
    """Run code (CLI commands, tests) as a specific account."""
    previous = g.get("account_id")
    g.account_id = account_id
    try:
        yield
    finally:
        g.account_id = previous


def today() -> date:
    fixed = current_app.config.get("FIXED_TODAY")
    return sch.parse_date(fixed) if fixed else date.today()


# ---- accounts ---------------------------------------------------------------

def get_account(account_id: int):
    return get_db().execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()


def update_account(name: str, email: str) -> None:
    db = get_db()
    with db:
        db.execute("UPDATE accounts SET name = ?, email = ? WHERE id = ?", (name, email, aid()))


# ---- settings ---------------------------------------------------------------

def get_setting(key: str, default: str | None = None) -> str | None:
    row = get_db().execute("SELECT value FROM settings WHERE account_id = ? AND key = ?",
                           (aid(), key)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    db = get_db()
    db.execute("INSERT INTO settings (account_id, key, value) VALUES (?, ?, ?) "
               "ON CONFLICT(account_id, key) DO UPDATE SET value = excluded.value", (aid(), key, value))
    db.commit()


def demo_mode() -> bool:
    return get_setting("demo_mode", "0") == "1"


# ---- employees --------------------------------------------------------------

def list_employees(active_only: bool = False):
    sql = "SELECT * FROM employees WHERE account_id = ?"
    if active_only:
        sql += " AND active = 1"
    sql += " ORDER BY active DESC, name COLLATE NOCASE"
    return get_db().execute(sql, (aid(),)).fetchall()


def employees_by_id() -> dict:
    return {e["id"]: e for e in list_employees()}


def get_employee(employee_id: int):
    return get_db().execute("SELECT * FROM employees WHERE id = ? AND account_id = ?",
                            (employee_id, aid())).fetchone()


def get_availability(employee_id: int) -> dict:
    rows = get_db().execute("SELECT weekday, start_time, end_time FROM availability "
                            "WHERE employee_id = ? AND account_id = ?", (employee_id, aid())).fetchall()
    return {r["weekday"]: (r["start_time"], r["end_time"]) for r in rows}


def availability_map() -> dict:
    out: dict = {}
    for r in get_db().execute("SELECT employee_id, weekday, start_time, end_time FROM availability "
                              "WHERE account_id = ?", (aid(),)):
        out.setdefault(r["employee_id"], {})[r["weekday"]] = (r["start_time"], r["end_time"])
    return out


def _save_availability(db, employee_id: int, availability: dict) -> None:
    db.execute("DELETE FROM availability WHERE employee_id = ? AND account_id = ?", (employee_id, aid()))
    db.executemany("INSERT INTO availability (account_id, employee_id, weekday, start_time, end_time) "
                   "VALUES (?, ?, ?, ?, ?)",
                   [(aid(), employee_id, wd, s, e) for wd, (s, e) in sorted(availability.items())])


def create_employee(data: dict) -> int:
    db = get_db()
    role = canonical_role(data["role"])
    with db:
        cur = db.execute(
            "INSERT INTO employees (account_id, name, email, phone, role, notes, track_availability) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (aid(), data["name"], data["email"], data["phone"], role, data["notes"],
             int(data["track_availability"])))
        _save_availability(db, cur.lastrowid, data["availability"])
    return cur.lastrowid


def update_employee(employee_id: int, data: dict) -> None:
    db = get_db()
    role = canonical_role(data["role"])
    with db:
        cur = db.execute("UPDATE employees SET name = ?, email = ?, phone = ?, role = ?, notes = ?, "
                         "track_availability = ? WHERE id = ? AND account_id = ?",
                         (data["name"], data["email"], data["phone"], role, data["notes"],
                          int(data["track_availability"]), employee_id, aid()))
        if cur.rowcount:
            _save_availability(db, employee_id, data["availability"])


def set_employee_active(employee_id: int, active: bool) -> None:
    db = get_db()
    with db:
        db.execute("UPDATE employees SET active = ? WHERE id = ? AND account_id = ?",
                   (int(active), employee_id, aid()))


def delete_employee(employee_id: int) -> int:
    """Delete an employee. Their shifts are kept but become unfilled. Returns affected shift count."""
    db = get_db()
    with db:
        n = db.execute("SELECT COUNT(*) FROM shifts WHERE employee_id = ? AND account_id = ?",
                       (employee_id, aid())).fetchone()[0]
        db.execute("DELETE FROM employees WHERE id = ? AND account_id = ?", (employee_id, aid()))
    return n


def future_shift_count(employee_id: int) -> int:
    return get_db().execute("SELECT COUNT(*) FROM shifts WHERE employee_id = ? AND account_id = ? "
                            "AND shift_date >= ?", (employee_id, aid(), today().isoformat())).fetchone()[0]


def list_roles() -> list[str]:
    """Distinct roles, case-insensitively (first spelling seen wins)."""
    rows = get_db().execute("SELECT role FROM employees WHERE account_id = ? AND role != '' UNION ALL "
                            "SELECT role FROM shifts WHERE account_id = ? AND role != ''",
                            (aid(), aid())).fetchall()
    seen: dict[str, str] = {}
    for r in rows:
        seen.setdefault(r["role"].lower(), r["role"])
    return sorted(seen.values(), key=str.lower)


def canonical_role(role: str) -> str:
    """Reuse an existing role's spelling so 'cook' and 'Cook' don't become two roles."""
    if not role:
        return role
    return {r.lower(): r for r in list_roles()}.get(role.lower(), role)


class OverlapError(Exception):
    def __init__(self, overlaps):
        super().__init__("Shift overlaps an existing shift for the same employee.")
        self.overlaps = overlaps


@contextmanager
def _write_transaction(db):
    """BEGIN IMMEDIATE takes the write lock up front, so check-then-insert is atomic."""
    if db.in_transaction:
        db.commit()
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


# ---- shifts -----------------------------------------------------------------

# The join also requires the same account, so a mismatched row can never show another
# business's employee name.
SHIFT_SELECT = ("SELECT s.*, e.name AS employee_name, e.active AS employee_active, "
                "e.track_availability AS track_availability "
                "FROM shifts s LEFT JOIN employees e ON e.id = s.employee_id AND e.account_id = s.account_id")
SHIFT_ORDER = " ORDER BY s.shift_date, s.start_time, e.name COLLATE NOCASE"


def get_shift(shift_id: int):
    return get_db().execute(SHIFT_SELECT + " WHERE s.id = ? AND s.account_id = ?",
                            (shift_id, aid())).fetchone()


def shifts_between(start: date, end: date, employee: str | None = None, role: str | None = None):
    """Shifts whose start date is within [start, end] inclusive, optionally filtered.

    `employee` is an employee id string, 'unfilled', or empty for all.
    """
    sql = SHIFT_SELECT + " WHERE s.account_id = ? AND s.shift_date BETWEEN ? AND ?"
    params: list = [aid(), start.isoformat(), end.isoformat()]
    if employee == "unfilled":
        sql += " AND s.employee_id IS NULL"
    elif employee:
        sql += " AND s.employee_id = ?"
        params.append(int(employee))
    if role:
        sql += " AND s.role = ? COLLATE NOCASE"
        params.append(role)
    return get_db().execute(sql + SHIFT_ORDER, params).fetchall()


def find_overlapping_shifts(employee_id: int, shift_date: str, start: str, end: str,
                            exclude_id: int | None = None) -> list:
    """Existing shifts for the employee that overlap the proposed time.

    Shifts are always under 24h, so only the previous, same and next day can collide.
    """
    d = sch.parse_date(shift_date)
    rows = get_db().execute(
        SHIFT_SELECT + " WHERE s.account_id = ? AND s.employee_id = ? AND s.shift_date BETWEEN ? AND ? "
        "AND s.id != ?" + SHIFT_ORDER,
        (aid(), employee_id, (d - timedelta(days=1)).isoformat(), (d + timedelta(days=1)).isoformat(),
         exclude_id or -1)).fetchall()
    return sch.find_overlaps(d, start, end, rows)


def warnings_for(shift, avail_by_employee: dict | None = None) -> list[str]:
    """Soft warnings for a shift row (joined with employee columns) or validated shift dict."""
    if shift["employee_id"] is None:
        return []
    emp = get_employee(shift["employee_id"]) if "employee_name" not in shift.keys() else None
    if "employee_name" not in shift.keys() and emp is None:
        return []
    name = emp["name"] if emp else shift["employee_name"]
    track = emp["track_availability"] if emp else shift["track_availability"]
    active = emp["active"] if emp else shift["employee_active"]
    if avail_by_employee is None:
        avail = get_availability(shift["employee_id"])
    else:
        avail = avail_by_employee.get(shift["employee_id"], {})
    out = sch.availability_warnings(sch.parse_date(shift["shift_date"]), shift["start_time"],
                                    shift["end_time"], employee_name=name,
                                    track_availability=bool(track), availability=avail)
    if not active:
        out.append(f"{name} is inactive.")
    return out


def decorate(shifts) -> list[dict]:
    """Turn shift rows into dicts with computed hours, overnight flag and warnings."""
    avail = availability_map()
    out = []
    for s in shifts:
        d = dict(s)
        sd = sch.parse_date(s["shift_date"])
        d["date"] = sd
        d["hours"] = sch.shift_hours(sd, s["start_time"], s["end_time"])
        d["overnight"] = sch.is_overnight(s["start_time"], s["end_time"])
        d["warnings"] = warnings_for(s, avail)
        out.append(d)
    return out


def _check_overlap(data: dict, exclude_id: int | None) -> None:
    if data["employee_id"] is None:
        return
    hits = find_overlapping_shifts(data["employee_id"], data["shift_date"], data["start_time"],
                                   data["end_time"], exclude_id=exclude_id)
    if hits:
        raise OverlapError(hits)


def _check_employee_in_account(employee_id: int | None) -> None:
    if employee_id is not None and get_employee(employee_id) is None:
        raise ValueError("That employee doesn't exist in this business.")


def create_shift(data: dict) -> int:
    """Insert a shift. Raises OverlapError if it overlaps the employee's other shifts."""
    db = get_db()
    role = canonical_role(data["role"])
    with _write_transaction(db):
        _check_employee_in_account(data["employee_id"])
        _check_overlap(data, None)
        cur = db.execute("INSERT INTO shifts (account_id, employee_id, shift_date, start_time, end_time, "
                         "role, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (aid(), data["employee_id"], data["shift_date"], data["start_time"],
                          data["end_time"], role, data["notes"]))
    return cur.lastrowid


def update_shift(shift_id: int, data: dict) -> None:
    """Update a shift. Raises OverlapError if it would overlap another shift."""
    db = get_db()
    role = canonical_role(data["role"])
    with _write_transaction(db):
        _check_employee_in_account(data["employee_id"])
        _check_overlap(data, shift_id)
        db.execute("UPDATE shifts SET employee_id = ?, shift_date = ?, start_time = ?, end_time = ?, "
                   "role = ?, notes = ? WHERE id = ? AND account_id = ?",
                   (data["employee_id"], data["shift_date"], data["start_time"], data["end_time"],
                    role, data["notes"], shift_id, aid()))


def delete_shift(shift_id: int) -> None:
    db = get_db()
    with db:
        db.execute("DELETE FROM shifts WHERE id = ? AND account_id = ?", (shift_id, aid()))


# ---- summaries --------------------------------------------------------------

def weekly_hours(monday: date) -> list[dict]:
    """Hours per employee for the week starting `monday`.

    Includes every active employee (even at 0h) plus inactive ones with shifts that week.
    """
    shifts = shifts_between(monday, monday + timedelta(days=6))
    totals = sch.hours_by_employee(shifts)
    counts: dict = {}
    for s in shifts:
        counts[s["employee_id"]] = counts.get(s["employee_id"], 0) + 1
    rows = []
    for e in list_employees():
        if e["active"] or e["id"] in totals:
            rows.append({"employee": e, "hours": totals.get(e["id"], 0.0),
                         "shifts": counts.get(e["id"], 0)})
    rows.sort(key=lambda r: (-r["hours"], r["employee"]["name"].lower()))
    return rows


UPCOMING_LIMIT = 15


def week_summary(shifts) -> dict:
    """Split a set of shifts' hours into assigned vs unfilled."""
    by_emp = sch.hours_by_employee(shifts)
    unfilled = by_emp.pop(None, 0.0)
    assigned = round(sum(by_emp.values()), 2)
    return {"assigned_hours": assigned, "unfilled_hours": unfilled,
            "total_hours": round(assigned + unfilled, 2),
            "unfilled_count": sum(1 for s in shifts if s["employee_id"] is None)}


def dashboard_data() -> dict:
    t = today()
    monday = sch.week_start(t)
    week = shifts_between(monday, monday + timedelta(days=6))
    # Overnight shifts from yesterday that are still running today also count as "today".
    yesterday = [s for s in shifts_between(t - timedelta(days=1), t - timedelta(days=1))
                 if sch.is_overnight(s["start_time"], s["end_time"])]
    todays = shifts_between(t, t)
    db = get_db()
    unfilled = db.execute(SHIFT_SELECT + " WHERE s.account_id = ? AND s.employee_id IS NULL "
                          "AND s.shift_date >= ?" + SHIFT_ORDER, (aid(), t.isoformat())).fetchall()
    upcoming_range = (aid(), t.isoformat(), (t + timedelta(days=7)).isoformat())
    upcoming = db.execute(SHIFT_SELECT + " WHERE s.account_id = ? AND s.shift_date > ? AND s.shift_date <= ?"
                          + SHIFT_ORDER + f" LIMIT {UPCOMING_LIMIT}", upcoming_range).fetchall()
    upcoming_total = db.execute("SELECT COUNT(*) FROM shifts WHERE account_id = ? AND shift_date > ? "
                                "AND shift_date <= ?", upcoming_range).fetchone()[0]
    summary = week_summary(week)
    return {
        "today": t,
        "monday": monday,
        "carry_over": decorate(yesterday),
        "today_shifts": decorate(todays),
        "week_hours": summary["total_hours"],
        "assigned_hours": summary["assigned_hours"],
        "unfilled_hours": summary["unfilled_hours"],
        "week_shift_count": len(week),
        "upcoming_total": upcoming_total,
        "unfilled": decorate(unfilled),
        "upcoming": decorate(upcoming),
        "employee_count": db.execute("SELECT COUNT(*) FROM employees WHERE account_id = ? AND active = 1",
                                     (aid(),)).fetchone()[0],
    }


def my_dashboard_data(employee_id: int) -> dict:
    """What an employee sees: only their own shifts."""
    t = today()
    monday = sch.week_start(t)
    emp = str(employee_id)
    yesterday = [s for s in shifts_between(t - timedelta(days=1), t - timedelta(days=1), emp)
                 if sch.is_overnight(s["start_time"], s["end_time"])]
    week = shifts_between(monday, monday + timedelta(days=6), emp)
    return {
        "today": t,
        "monday": monday,
        "employee": get_employee(employee_id),
        "carry_over": decorate(yesterday),
        "today_shifts": decorate(shifts_between(t, t, emp)),
        "upcoming": decorate(shifts_between(t + timedelta(days=1), t + timedelta(days=14), emp)),
        "week_hours": sch.total_hours(week),
        "week_shift_count": len(week),
    }


def logins_by_employee() -> dict:
    """employee_id -> sign-in (username, role, last login) for this business. No password data."""
    rows = get_db().execute("SELECT employee_id, username, role, last_login_at FROM users "
                            "WHERE account_id = ? AND employee_id IS NOT NULL", (aid(),)).fetchall()
    return {r["employee_id"]: r for r in rows}


def counts() -> dict:
    db = get_db()
    return {"employees": db.execute("SELECT COUNT(*) FROM employees WHERE account_id = ?", (aid(),)).fetchone()[0],
            "shifts": db.execute("SELECT COUNT(*) FROM shifts WHERE account_id = ?", (aid(),)).fetchone()[0]}


def clear_all_data() -> None:
    """Delete this business's employees and shifts (never another business's).

    Employees who have a *manager* sign-in are kept, so no manager is ever locked out.
    """
    db = get_db()
    with db:
        db.execute("DELETE FROM shifts WHERE account_id = ?", (aid(),))
        db.execute("DELETE FROM employees WHERE account_id = ? AND id NOT IN "
                   "(SELECT employee_id FROM users WHERE account_id = ? AND role = 'manager' "
                   "AND employee_id IS NOT NULL)", (aid(), aid()))
        db.execute("DELETE FROM settings WHERE account_id = ? AND key = 'demo_mode'", (aid(),))
