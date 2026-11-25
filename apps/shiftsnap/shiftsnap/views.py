import csv
import io
from datetime import timedelta
from urllib.parse import urlsplit

from flask import (Blueprint, Response, abort, current_app, flash, g, redirect, render_template,
                   request, url_for)

from . import scheduling as sch
from . import services
from . import auth
from .auth import get_user_for_employee, is_manager, manager_required
from .demo import load_demo_data
from .validation import validate_employee_form, validate_shift_form

bp = Blueprint("main", __name__)


def register_template_helpers(app):
    @app.template_filter("hours")
    def fmt_hours(value):
        value = float(value or 0)
        return f"{value:g}h" if value != int(value) else f"{int(value)}h"

    @app.template_filter("dshort")
    def fmt_dshort(d):
        return f"{sch.WEEKDAYS_SHORT[d.weekday()]} {d.day} {d.strftime('%b')}"

    @app.template_filter("week_label")
    def fmt_week_label(monday):
        """'Mon 26 Oct – Sun 1 Nov 2026', with both years when the week spans New Year."""
        sunday = monday + timedelta(days=6)
        first = fmt_dshort(monday) + (f" {monday.year}" if monday.year != sunday.year else "")
        return f"{first} – {fmt_dshort(sunday)} {sunday.year}"

    @app.template_filter("dlong")
    def fmt_dlong(d):
        return f"{sch.WEEKDAYS[d.weekday()]}, {d.day} {d.strftime('%B %Y')}"

    @app.context_processor
    def inject_globals():
        try:
            demo = services.demo_mode() if g.get("account_id") is not None else False
        except Exception:  # keep error pages renderable even if the database is broken
            demo = False
        return {"demo_mode": demo, "today": services.today(), "WEEKDAYS": sch.WEEKDAYS, "current_url": here}


def _safe_next(default: str) -> str:
    """Only allow same-site relative redirect targets.

    Browsers strip tabs/newlines and treat backslashes as slashes, so "/\\t/evil" would
    become "//evil" (another host). Reject any such characters outright.
    """
    nxt = request.values.get("next", "")
    if not nxt or any(ch.isspace() or ord(ch) < 32 or ch == "\\" for ch in nxt):
        return default
    parts = urlsplit(nxt)
    if parts.scheme or parts.netloc or not nxt.startswith("/") or nxt.startswith("//"):
        return default
    # Under an address prefix (e.g. /shiftsnap on the shared host) only stay inside ShiftSnap,
    # never jump to another app on the same site.
    root = request.script_root
    if root and nxt != root and not nxt.startswith(root + "/"):
        return default
    return nxt


def here() -> str:
    """This page's full address including any prefix, for 'next' links back to it."""
    path = request.script_root + request.full_path
    return path[:-1] if path.endswith("?") else path


def _int_or_404(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        abort(404)


# ---- dashboard --------------------------------------------------------------

@bp.get("/")
def dashboard():
    if g.user is None:  # visitors get the public landing page
        return render_template("landing.html", price=current_app.config["PRICE_TEXT"],
                               trial_days=current_app.config["TRIAL_DAYS"])
    if is_manager():
        return render_template("dashboard.html", d=services.dashboard_data())
    return render_template("my_dashboard.html", d=services.my_dashboard_data(g.user["employee_id"]))


def _redact_for_employee(shifts: list[dict]) -> list[dict]:
    """Employees see the team schedule, but other people's notes and availability warnings
    (which reveal private availability) are hidden."""
    me = g.user["employee_id"]
    for s in shifts:
        s["is_mine"] = s["employee_id"] == me
        if not s["is_mine"]:
            s["notes"] = ""
            s["warnings"] = []
    return shifts


# ---- employees --------------------------------------------------------------

@bp.get("/employees")
@manager_required
def employees():
    monday = sch.week_start(services.today())
    hours = {r["employee"]["id"]: r["hours"] for r in services.weekly_hours(monday)}
    avail = services.availability_map()
    emps = services.list_employees()
    return render_template("employees.html",
                           active=[e for e in emps if e["active"]],
                           inactive=[e for e in emps if not e["active"]],
                           hours=hours, avail=avail, monday=monday,
                           logins=services.logins_by_employee())


def _employee_values(emp=None, availability=None) -> dict:
    if emp is None:
        return {"track_availability": "", "name": "", "email": "", "phone": "", "role": "", "notes": ""}
    vals = {k: emp[k] for k in ("name", "email", "phone", "role", "notes")}
    vals["track_availability"] = "1" if emp["track_availability"] else ""
    for wd, (s, e) in (availability or {}).items():
        vals[f"avail_{wd}_on"] = "1"
        vals[f"avail_{wd}_start"] = s
        vals[f"avail_{wd}_end"] = e
    return vals


@bp.route("/employees/new", methods=["GET", "POST"])
@manager_required
def employee_new():
    if request.method == "POST":
        data, errors = validate_employee_form(request.form)
        if not errors:
            services.create_employee(data)
            flash(f"Added {data['name']}.", "success")
            return redirect(url_for("main.employees"))
        return render_template("employee_form.html", values=request.form, errors=errors,
                               employee=None, roles=services.list_roles()), 400
    return render_template("employee_form.html", values=_employee_values(), errors={},
                           employee=None, roles=services.list_roles())


@bp.route("/employees/<int:employee_id>/edit", methods=["GET", "POST"])
@manager_required
def employee_edit(employee_id):
    emp = services.get_employee(employee_id) or abort(404)
    if request.method == "POST":
        data, errors = validate_employee_form(request.form)
        if not errors:
            services.update_employee(employee_id, data)
            flash(f"Saved changes to {data['name']}.", "success")
            return redirect(url_for("main.employees"))
        return render_template("employee_form.html", values=request.form, errors=errors,
                               employee=emp, roles=services.list_roles()), 400
    return render_template("employee_form.html",
                           values=_employee_values(emp, services.get_availability(employee_id)),
                           errors={}, employee=emp, roles=services.list_roles())


def _is_me(employee_id: int) -> bool:
    """True when this employee record is the signed-in manager's own (they'd lock themselves out)."""
    return g.user["employee_id"] == employee_id


def _refuse_self(action: str):
    flash(f"You can't {action} your own record while signed in with it. Ask another manager.", "error")
    return redirect(url_for("main.employees"))


@bp.post("/employees/<int:employee_id>/toggle-active")
@manager_required
def employee_toggle_active(employee_id):
    emp = services.get_employee(employee_id) or abort(404)
    if emp["active"] and _is_me(employee_id):
        return _refuse_self("deactivate")
    services.set_employee_active(employee_id, not emp["active"])
    if emp["active"]:
        n = services.future_shift_count(employee_id)
        msg = f"Deactivated {emp['name']}. They can no longer be assigned new shifts."
        if n:
            msg += f" They still have {n} upcoming shift(s) — reassign or delete them on the schedule."
        flash(msg, "warning" if n else "success")
    else:
        flash(f"Reactivated {emp['name']}.", "success")
    return redirect(url_for("main.employees"))


@bp.route("/employees/<int:employee_id>/delete", methods=["GET", "POST"])
@manager_required
def employee_delete(employee_id):
    emp = services.get_employee(employee_id) or abort(404)
    if _is_me(employee_id):
        return _refuse_self("remove")
    if request.method == "POST":
        n = services.delete_employee(employee_id)
        msg = f"Removed {emp['name']}."
        if n:
            msg += f" {n} of their shift(s) are now unfilled."
        flash(msg, "success")
        return redirect(url_for("main.employees"))
    return render_template("employee_delete.html", employee=emp,
                           future=services.future_shift_count(employee_id),
                           has_login=get_user_for_employee(employee_id) is not None)


@bp.route("/employees/<int:employee_id>/login", methods=["GET", "POST"])
@manager_required
def employee_login(employee_id):
    """Create a sign-in for an employee, or reset its password. Managers never see passwords
    after setting them; the employee must choose their own at first sign-in."""
    emp = services.get_employee(employee_id) or abort(404)
    user = get_user_for_employee(employee_id)
    if user is not None and user["id"] == g.user["id"]:
        flash("To change your own password, use your Account page.", "warning")
        return redirect(url_for("auth.account"))
    errors: dict[str, str] = {}
    username = user["username"] if user else ""
    role = user["role"] if user else ((request.form.get("role") or "employee") if request.method == "POST"
                                      else "employee")
    if request.method == "POST":
        password = request.form.get("password") or ""
        if user is None:
            username = (request.form.get("username") or "").strip()
            if role not in ("employee", "manager"):
                errors["role"] = "Choose Employee or Manager."
            if (msg := auth.validate_username(username)):
                errors["username"] = msg
            elif auth.username_taken(username):
                errors["username"] = "That username is already taken. Try adding your business name, e.g. maria.cafe."
        if not errors and (msg := auth.validate_password(password, username)):
            errors["password"] = msg
        if not errors:
            if user is None:
                auth.create_user(username, password, role, employee_id, must_change_password=True)
                kind = "Manager sign-in" if role == "manager" else "Sign-in"
                flash(f"{kind} created for {emp['name']} (username “{username}”). Give them the "
                      "temporary password in person; they must change it when they first sign in.",
                      "success")
            else:
                auth.set_password(user["id"], password, must_change=True)
                flash(f"Temporary password set for {emp['name']}. Their other sessions were signed out.",
                      "success")
            return redirect(url_for("main.employees"))
    return render_template("employee_login.html", employee=emp, user=user, errors=errors,
                           username=username, role=role), (400 if errors else 200)


@bp.post("/employees/<int:employee_id>/login/revoke")
@manager_required
def employee_login_revoke(employee_id):
    emp = services.get_employee(employee_id) or abort(404)
    user = get_user_for_employee(employee_id) or abort(404)
    if user["id"] == g.user["id"]:
        return _refuse_self("remove the sign-in of")
    auth.delete_user(user["id"])
    flash(f"Removed {emp['name']}'s sign-in. Their employee record and shifts are unchanged.", "success")
    return redirect(url_for("main.employees"))


# ---- schedule ---------------------------------------------------------------

def _schedule_params():
    view = request.args.get("view", "week")
    if view not in ("week", "day"):
        view = "week"
    d = sch.parse_date(request.args.get("date")) or services.today()
    employee = request.args.get("employee", "").strip()
    if employee and employee != "unfilled" and not employee.isdigit():
        employee = ""
    role = request.args.get("role", "").strip()
    return view, d, employee, role


@bp.get("/schedule")
def schedule():
    view, d, employee, role = _schedule_params()
    filters = {"employee": employee or None, "role": role or None}
    monday = sch.week_start(d)
    sunday = monday + timedelta(days=6)
    step = 7 if view == "week" else 1
    prev_d, next_d = d - timedelta(days=step), d + timedelta(days=step)
    # Fetch the filtered week once: it feeds the grid, the day strip counts and the day view.
    week_shifts = services.decorate(services.shifts_between(monday, sunday, employee, role))
    shifts = week_shifts if view == "week" else [s for s in week_shifts if s["date"] == d]
    carry_over = []
    if view == "day":
        carry_over = [s for s in services.decorate(
            services.shifts_between(d - timedelta(days=1), d - timedelta(days=1), employee, role))
            if s["overnight"]]
    weekly_hours = services.weekly_hours(monday)
    if not is_manager():
        _redact_for_employee(week_shifts)
        _redact_for_employee(carry_over)
        weekly_hours = [r for r in weekly_hours if r["employee"]["id"] == g.user["employee_id"]]
    by_day = {day: [] for day in sch.week_days(monday)}
    for s in week_shifts:
        by_day[s["date"]].append(s)
    return render_template(
        "schedule.html", view=view, d=d, monday=monday, days=sch.week_days(monday),
        by_day=by_day, shifts=shifts, carry_over=carry_over, filters=filters,
        summary=services.week_summary(services.shifts_between(monday, sunday)),
        employee=employee, role=role, prev_d=prev_d, next_d=next_d,
        total_hours=sum(s["hours"] for s in shifts),
        weekly_hours=weekly_hours,
        employees=services.list_employees(), roles=services.list_roles(),
        has_any_employee=bool(services.list_employees()))


@bp.get("/schedule/print")
@manager_required
def schedule_print():
    _, d, employee, role = _schedule_params()
    monday = sch.week_start(d)
    days = sch.week_days(monday)
    shifts = services.decorate(services.shifts_between(monday, days[-1], employee, role))
    rows: dict = {}
    for s in shifts:
        key = s["employee_id"]
        row = rows.setdefault(key, {"name": s["employee_name"] or "Unfilled", "hours": 0.0,
                                    "cells": {day: [] for day in days}})
        row["cells"][s["date"]].append(s)
        row["hours"] = round(row["hours"] + s["hours"], 2)
    ordered = sorted(rows.items(), key=lambda kv: (kv[0] is None, kv[1]["name"].lower()))
    emp_name = None
    if employee == "unfilled":
        emp_name = "Unfilled shifts"
    elif employee:
        e = services.get_employee(int(employee))
        emp_name = e["name"] if e else None
    return render_template("print.html", days=days, monday=monday, rows=[r for _, r in ordered],
                           total=round(sum(s["hours"] for s in shifts), 2), employee_name=emp_name,
                           role=role, employee=employee)


def _csv_cell(value) -> str:
    """Neutralise spreadsheet formula injection."""
    s = "" if value is None else str(value)
    if s and s[0] in "=+-@\t\r":
        s = "'" + s
    return s


@bp.get("/schedule/export.csv")
@manager_required
def schedule_export():
    _, d, employee, role = _schedule_params()
    monday = sch.week_start(d)
    shifts = services.decorate(services.shifts_between(monday, monday + timedelta(days=6), employee, role))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Day", "Start", "End", "Ends next day", "Hours", "Employee", "Role",
                "Notes", "Warnings"])
    for s in shifts:
        w.writerow([_csv_cell(v) for v in (
            s["shift_date"], sch.WEEKDAYS[s["date"].weekday()], s["start_time"], s["end_time"],
            "yes" if s["overnight"] else "no", f"{s['hours']:g}", s["employee_name"] or "UNFILLED",
            s["role"], s["notes"], "; ".join(s["warnings"]))])
    filename = f"shiftsnap-week-{monday.isoformat()}.csv"
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---- shifts -----------------------------------------------------------------

def _shift_form_context(values, errors, shift=None, warnings=None, overlaps=None):
    current = shift["employee_id"] if shift else None
    emps = [e for e in services.list_employees() if e["active"] or e["id"] == current]
    return dict(values=values, errors=errors, shift=shift, warnings=warnings or [],
                overlaps=services.decorate(overlaps or []), employees=emps,
                roles=services.list_roles(), next=_safe_next(""))


OVERLAP_MSG = "This overlaps another shift for the same employee. Change the time or choose someone else."


def _handle_shift_post(shift=None):
    current = shift["employee_id"] if shift else None
    data, errors = validate_shift_form(request.form, services.employees_by_id(), current)
    overlaps = []
    if not errors and data["employee_id"] is not None:
        overlaps = services.find_overlapping_shifts(
            data["employee_id"], data["shift_date"], data["start_time"], data["end_time"],
            exclude_id=shift["id"] if shift else None)
        if overlaps:
            errors["employee_id"] = OVERLAP_MSG
    if errors:
        return render_template("shift_form.html",
                               **_shift_form_context(request.form, errors, shift, overlaps=overlaps)), 400

    warnings = services.warnings_for(data)
    if warnings and request.form.get("confirm_warnings") != "1":
        return render_template("shift_form.html",
                               **_shift_form_context(request.form, {}, shift, warnings=warnings))
    try:
        # The save re-checks overlaps inside a locked transaction (e.g. a second browser tab).
        if shift:
            services.update_shift(shift["id"], data)
            flash("Shift updated.", "success")
        else:
            services.create_shift(data)
            flash("Shift added.", "success")
    except services.OverlapError as exc:
        return render_template("shift_form.html", **_shift_form_context(
            request.form, {"employee_id": OVERLAP_MSG}, shift, overlaps=exc.overlaps)), 409
    if warnings:
        flash("Saved with warning: " + " ".join(warnings), "warning")
    return redirect(_safe_next(url_for("main.schedule", date=data["shift_date"])))


@bp.route("/shifts/new", methods=["GET", "POST"])
@manager_required
def shift_new():
    if request.method == "POST":
        return _handle_shift_post()
    d = sch.parse_date(request.args.get("date")) or services.today()
    emp = request.args.get("employee", "")
    values = {"shift_date": d.isoformat(), "start_time": "09:00", "end_time": "17:00",
              "employee_id": emp if emp.isdigit() else "", "role": "", "notes": ""}
    return render_template("shift_form.html", **_shift_form_context(values, {}))


@bp.route("/shifts/<int:shift_id>/edit", methods=["GET", "POST"])
@manager_required
def shift_edit(shift_id):
    shift = services.get_shift(shift_id) or abort(404)
    if request.method == "POST":
        return _handle_shift_post(shift)
    values = {k: shift[k] for k in ("shift_date", "start_time", "end_time", "role", "notes")}
    values["employee_id"] = str(shift["employee_id"] or "")
    values["ends_next_day"] = "1" if sch.is_overnight(shift["start_time"], shift["end_time"]) else ""
    return render_template("shift_form.html", **_shift_form_context(values, {}, shift))


@bp.post("/shifts/<int:shift_id>/delete")
@manager_required
def shift_delete(shift_id):
    shift = services.get_shift(shift_id) or abort(404)
    services.delete_shift(shift_id)
    flash("Shift deleted.", "success")
    return redirect(_safe_next(url_for("main.schedule", date=shift["shift_date"])))


# ---- demo / data ------------------------------------------------------------

@bp.get("/data")
@manager_required
def data_page():
    return render_template("data.html", counts=services.counts())


def _require_ack():
    if request.form.get("acknowledge") != "1":
        flash("Tick the confirmation box first — this replaces or deletes all data.", "error")
        return redirect(url_for("main.data_page"))
    return None


@bp.post("/data/demo")
@manager_required
def data_load_demo():
    if (resp := _require_ack()) is not None:
        return resp
    load_demo_data()
    flash("Demo mode on: sample employees and shifts loaded. Names starting with "
          "“Sample:” are not real people.", "success")
    return redirect(url_for("main.dashboard"))


@bp.post("/data/clear")
@manager_required
def data_clear():
    if (resp := _require_ack()) is not None:
        return resp
    services.clear_all_data()
    flash("All employees and shifts deleted. Demo mode is off.", "success")
    return redirect(url_for("main.dashboard"))
