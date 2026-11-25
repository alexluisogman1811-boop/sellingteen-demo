"""All HTTP routes."""
import re
import secrets
from datetime import date, timedelta

from flask import (
    Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request,
    send_from_directory, session, url_for,
)

from .constants import (
    CATEGORIES, FREQUENCIES, ISSUE_STATUSES, PLANS, PLATFORMS, RESULTS, SEVERITIES,
    SEVERITY_ORDER_SQL, WEBSITE_STATUSES,
)
from .db import get_db, wipe_account
from .demo import load_demo
from .helpers import (
    OCCURRENCE_SELECT, aid, attachment_files, fetch_or_404, horizon, int_arg, like, log_event, remove_files,
    sync_schedule, today,
)
from .reports import (
    build_client_report, build_report, client_report_csv, history_csv, parse_period, period_label, report_csv,
)
from .scheduling import (
    clear_pending, describe_schedule, ensure_occurrences, period_days, restart_generation,
)
from .uploads import IMAGE_TYPES, UploadError, store_files, validate_files
from .validation import Form, parse_month

bp = Blueprint("main", __name__)

ESC = " ESCAPE '\\'"
SEVERITY_ORDER = SEVERITY_ORDER_SQL.format(col="i.severity")
UPCOMING_DAYS = 14

SITE_SELECT = """
SELECT w.*, c.name AS client_name,
  (SELECT COUNT(*) FROM occurrences o JOIN tasks t ON t.id = o.task_id
     WHERE t.website_id = w.id AND o.status = 'pending' AND o.due_date < :today) AS overdue,
  (SELECT MIN(o.due_date) FROM occurrences o JOIN tasks t ON t.id = o.task_id
     WHERE t.website_id = w.id AND t.active = 1 AND o.status = 'pending' AND o.due_date >= :today) AS next_due,
  (SELECT MAX(o.completed_on) FROM occurrences o JOIN tasks t ON t.id = o.task_id
     WHERE t.website_id = w.id AND o.status = 'completed') AS last_done,
  (SELECT COUNT(*) FROM issues i WHERE i.website_id = w.id AND i.status IN ('open', 'in_progress')) AS open_issues
FROM websites w JOIN clients c ON c.id = w.client_id
WHERE w.account_id = :aid
"""


def _client_options(db):
    return db.execute(
        "SELECT id, name FROM clients WHERE account_id = ? ORDER BY name COLLATE NOCASE", (aid(),)
    ).fetchall()


def _website_options(db):
    return db.execute(
        "SELECT w.id, w.name, w.domain, w.status, c.name AS client_name FROM websites w "
        "JOIN clients c ON c.id = w.client_id WHERE w.account_id = ? ORDER BY w.name COLLATE NOCASE",
        (aid(),),
    ).fetchall()


def _own(db, table, row_id, columns="*"):
    """Fetch one row of `table` that belongs to the signed-in account, or 404.

    Another account's id gets exactly the same 404 as an id that doesn't exist.
    """
    return fetch_or_404(db, f"SELECT {columns} FROM {table} WHERE id = ? AND account_id = ?", (row_id, aid()))


def _csv_response(text, filename):
    return Response(
        "﻿" + text,  # BOM so Excel opens UTF-8 correctly
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _count_occurrences(db, where, params):
    return db.execute(
        "SELECT COUNT(*) FROM occurrences o JOIN tasks t ON t.id = o.task_id "
        f"JOIN websites w ON w.id = t.website_id WHERE o.account_id = ? AND ({where})",
        (aid(), *params),
    ).fetchone()[0]


# ---------------------------------------------------------------- dashboard

@bp.get("/")
def dashboard():
    if g.get("account") is None:
        return render_template("landing.html")
    db = get_db()
    sync_schedule(db)
    t = today()
    ts = t.isoformat()
    soon = (t + timedelta(days=UPCOMING_DAYS)).isoformat()
    month_start = t.replace(day=1).isoformat()
    live = " AND w.status = 'active' AND t.active = 1"

    stats = {
        "active_websites": db.execute(
            "SELECT COUNT(*) FROM websites WHERE account_id = ? AND status = 'active'", (aid(),)
        ).fetchone()[0],
        "overdue": _count_occurrences(db, "o.status = 'pending' AND o.due_date < ?", (ts,)),
        "upcoming": _count_occurrences(db, "o.status = 'pending' AND o.due_date BETWEEN ? AND ?" + live, (ts, soon)),
        "completed_month": _count_occurrences(
            db, "o.status = 'completed' AND o.completed_on BETWEEN ? AND ?", (month_start, ts)
        ),
        "open_issues": db.execute(
            "SELECT COUNT(*) FROM issues WHERE account_id = ? AND status IN ('open', 'in_progress')", (aid(),)
        ).fetchone()[0],
    }
    overdue = db.execute(
        OCCURRENCE_SELECT + " WHERE o.account_id = ? AND o.status = 'pending' AND o.due_date < ?"
        + " ORDER BY o.due_date, t.title LIMIT 25", (aid(), ts),
    ).fetchall()
    upcoming = db.execute(
        OCCURRENCE_SELECT + " WHERE o.account_id = ? AND o.status = 'pending' AND o.due_date BETWEEN ? AND ?" + live
        + " ORDER BY o.due_date, t.title LIMIT 25", (aid(), ts, soon),
    ).fetchall()
    completed = db.execute(
        OCCURRENCE_SELECT + " WHERE o.account_id = ? AND o.status = 'completed' ORDER BY o.completed_on DESC, o.recorded_at DESC LIMIT 8",
        (aid(),),
    ).fetchall()
    issues = db.execute(
        "SELECT i.*, w.name AS website_name FROM issues i JOIN websites w ON w.id = i.website_id "
        f"WHERE i.account_id = ? AND i.status IN ('open', 'in_progress') ORDER BY {SEVERITY_ORDER}, i.reported_on LIMIT 8",
        (aid(),),
    ).fetchall()
    sites = db.execute(
        SITE_SELECT + " AND w.status = 'active' ORDER BY overdue DESC, w.name COLLATE NOCASE",
        {"today": ts, "aid": aid()},
    ).fetchall()
    has_clients = db.execute("SELECT 1 FROM clients WHERE account_id = ? LIMIT 1", (aid(),)).fetchone() is not None
    return render_template(
        "dashboard.html", stats=stats, overdue=overdue, upcoming=upcoming, completed=completed,
        issues=issues, sites=sites, has_clients=has_clients, upcoming_days=UPCOMING_DAYS, today=t,
    )


# ---------------------------------------------------------------- clients

def _client_form(db, src, client_id=None):
    f = Form(src)
    name = f.text("name", "Client name", required=True, max_len=120)
    if name and db.execute(
        "SELECT 1 FROM clients WHERE account_id = ? AND lower(name) = lower(?) AND id != ?",
        (aid(), name, client_id or 0)
    ).fetchone():
        f.error("name", "A client with this name already exists.")
    f.text("contact_name", "Contact name", max_len=120)
    f.email("contact_email", "Contact email")
    f.text("notes", "Notes", max_len=2000, multiline=True, no_secrets=True)
    return f


@bp.get("/clients")
def clients_list():
    db = get_db()
    q = request.args.get("q", "").strip()[:100]
    sql = (
        "SELECT c.*, (SELECT COUNT(*) FROM websites w WHERE w.client_id = c.id) AS site_count, "
        "(SELECT COUNT(*) FROM websites w WHERE w.client_id = c.id AND w.status = 'active') AS active_count "
        "FROM clients c WHERE c.account_id = ?"
    )
    params = [aid()]
    if q:
        sql += (
            " AND (c.name LIKE ?" + ESC + " OR c.contact_name LIKE ?" + ESC
            + " OR c.contact_email LIKE ?" + ESC + " OR c.notes LIKE ?" + ESC + ")"
        )
        params += [like(q)] * 4
    clients = db.execute(sql + " ORDER BY c.name COLLATE NOCASE", params).fetchall()
    return render_template("clients/list.html", clients=clients, q=q)


@bp.route("/clients/new", methods=["GET", "POST"])
def client_new():
    db = get_db()
    errors = {}
    if request.method == "POST":
        f = _client_form(db, request.form)
        if f.ok:
            d = f.data
            cur = db.execute(
                "INSERT INTO clients (account_id, name, contact_name, contact_email, notes) VALUES (?, ?, ?, ?, ?)",
                (aid(), d["name"], d["contact_name"], d["contact_email"], d["notes"]),
            )
            db.commit()
            flash(f"Client “{d['name']}” added.", "success")
            return redirect(url_for("main.client_detail", client_id=cur.lastrowid))
        errors = f.errors
    return render_template("clients/form.html", values=request.form, errors=errors, client=None)


@bp.get("/clients/<int:client_id>")
def client_detail(client_id):
    db = get_db()
    client = _own(db, "clients", client_id)
    sync_schedule(db)
    sites = db.execute(
        SITE_SELECT + " AND w.client_id = :cid ORDER BY w.name COLLATE NOCASE",
        {"today": today().isoformat(), "cid": client_id, "aid": aid()},
    ).fetchall()
    return render_template("clients/detail.html", client=client, sites=sites)


@bp.route("/clients/<int:client_id>/edit", methods=["GET", "POST"])
def client_edit(client_id):
    db = get_db()
    client = _own(db, "clients", client_id)
    values, errors = dict(client), {}
    if request.method == "POST":
        f = _client_form(db, request.form, client_id)
        if f.ok:
            d = f.data
            db.execute(
                "UPDATE clients SET name = ?, contact_name = ?, contact_email = ?, notes = ? WHERE id = ? AND account_id = ?",
                (d["name"], d["contact_name"], d["contact_email"], d["notes"], client_id, aid()),
            )
            db.commit()
            flash("Client updated.", "success")
            return redirect(url_for("main.client_detail", client_id=client_id))
        values, errors = request.form, f.errors
    return render_template("clients/form.html", values=values, errors=errors, client=client)


@bp.post("/clients/<int:client_id>/delete")
def client_delete(client_id):
    db = get_db()
    client = _own(db, "clients", client_id)
    files = attachment_files(db, "w.client_id = ?", (client_id,))
    db.execute("DELETE FROM clients WHERE id = ? AND account_id = ?", (client_id, aid()))
    db.commit()
    remove_files(files)
    flash(f"Client “{client['name']}” and all of its websites were deleted.", "success")
    return redirect(url_for("main.clients_list"))


# ---------------------------------------------------------------- websites

def _website_form(db, src, website_id=None):
    f = Form(src)
    client_id = f.integer("client_id", "Client", 1, 2**31)
    if client_id and not db.execute(
        "SELECT 1 FROM clients WHERE id = ? AND account_id = ?", (client_id, aid())
    ).fetchone():
        f.error("client_id", "Choose an existing client.")
    f.text("name", "Website name", required=True, max_len=120)
    domain = f.domain("domain")
    if "domain" not in f.errors:
        dup = db.execute(
            "SELECT 1 FROM websites WHERE account_id = ? AND domain = ? AND id != ?", (aid(), domain, website_id or 0)
        ).fetchone()
        if dup:
            f.error("domain", "Another website already uses this domain.")
    f.choice("platform", "Platform", PLATFORMS)
    f.choice("plan", "Maintenance plan", PLANS)
    f.choice("status", "Status", WEBSITE_STATUSES)
    f.text("notes", "Notes", max_len=2000, multiline=True, no_secrets=True)
    return f


@bp.get("/websites")
def websites_list():
    db = get_db()
    sync_schedule(db)
    args = request.args
    q = args.get("q", "").strip()[:100]
    filters = {
        "client_id": int_arg(args, "client_id"),
        "platform": args.get("platform") if args.get("platform") in PLATFORMS else "",
        "plan": args.get("plan") if args.get("plan") in PLANS else "",
        "status": args.get("status") if args.get("status") in WEBSITE_STATUSES else "",
    }
    where, params = [], {"today": today().isoformat(), "aid": aid()}
    if q:
        where.append(
            "(w.name LIKE :q" + ESC + " OR w.domain LIKE :q" + ESC
            + " OR w.notes LIKE :q" + ESC + " OR c.name LIKE :q" + ESC + ")"
        )
        params["q"] = like(q)
    for key in ("client_id", "platform", "plan", "status"):
        if filters[key]:
            where.append(f"w.{key} = :{key}")
            params[key] = filters[key]
    sql = SITE_SELECT + (" AND " + " AND ".join(where) if where else "")
    sites = db.execute(sql + " ORDER BY w.name COLLATE NOCASE", params).fetchall()
    return render_template(
        "websites/list.html", sites=sites, q=q, filters=filters, clients=_client_options(db),
    )


@bp.route("/websites/new", methods=["GET", "POST"])
def website_new():
    db = get_db()
    clients = _client_options(db)
    if not clients:
        flash("Add a client first - every website belongs to a client.", "info")
        return redirect(url_for("main.client_new"))
    values = {"client_id": request.args.get("client_id", ""), "status": "active", "plan": "standard", "platform": "wordpress"}
    errors = {}
    if request.method == "POST":
        f = _website_form(db, request.form)
        if f.ok:
            d = f.data
            cur = db.execute(
                "INSERT INTO websites (account_id, client_id, name, domain, platform, plan, status, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (aid(), d["client_id"], d["name"], d["domain"], d["platform"], d["plan"], d["status"], d["notes"]),
            )
            db.commit()
            flash(f"Website {d['domain']} added. Next, add its recurring maintenance tasks.", "success")
            return redirect(url_for("main.website_detail", website_id=cur.lastrowid))
        values, errors = request.form, f.errors
    return render_template("websites/form.html", values=values, errors=errors, clients=clients, site=None)


@bp.get("/websites/<int:website_id>")
def website_detail(website_id):
    db = get_db()
    sync_schedule(db)
    t = today()
    ts = t.isoformat()
    site = fetch_or_404(db, SITE_SELECT + " AND w.id = :id", {"today": ts, "id": website_id, "aid": aid()})
    tasks = db.execute(
        "SELECT t.*, "
        "(SELECT MIN(o.due_date) FROM occurrences o WHERE o.task_id = t.id AND o.status = 'pending' AND o.due_date >= ?) AS next_due, "
        "(SELECT MAX(o.completed_on) FROM occurrences o WHERE o.task_id = t.id AND o.status = 'completed') AS last_done, "
        "(SELECT COUNT(*) FROM occurrences o WHERE o.task_id = t.id AND o.status = 'pending' AND o.due_date < ?) AS overdue "
        "FROM tasks t WHERE t.website_id = ? AND t.account_id = ? ORDER BY t.active DESC, t.title COLLATE NOCASE",
        (ts, ts, website_id, aid()),
    ).fetchall()
    due = db.execute(
        OCCURRENCE_SELECT + " WHERE o.account_id = ? AND t.website_id = ? AND o.status = 'pending' AND o.due_date <= ? "
        "AND (o.due_date < ? OR (t.active = 1 AND w.status = 'active')) ORDER BY o.due_date, t.title",
        (aid(), website_id, (t + timedelta(days=30)).isoformat(), ts),
    ).fetchall()
    history = db.execute(
        OCCURRENCE_SELECT + " WHERE o.account_id = ? AND t.website_id = ? AND o.status IN ('completed', 'skipped') "
        "ORDER BY COALESCE(o.completed_on, o.due_date) DESC, o.id DESC LIMIT 100",
        (aid(), website_id),
    ).fetchall()
    issues = db.execute(
        "SELECT i.* FROM issues i WHERE i.website_id = ? AND i.account_id = ? "
        f"ORDER BY (i.status IN ('open', 'in_progress')) DESC, {SEVERITY_ORDER}, i.reported_on DESC",
        (website_id, aid()),
    ).fetchall()
    return render_template(
        "websites/detail.html", site=site, tasks=tasks, due=due, history=history, issues=issues,
        today=t, describe_schedule=describe_schedule, month=t.strftime("%Y-%m"),
    )


@bp.route("/websites/<int:website_id>/edit", methods=["GET", "POST"])
def website_edit(website_id):
    db = get_db()
    site = _own(db, "websites", website_id)
    values, errors = dict(site), {}
    if request.method == "POST":
        f = _website_form(db, request.form, website_id)
        if f.ok:
            d = f.data
            db.execute(
                "UPDATE websites SET client_id = ?, name = ?, domain = ?, platform = ?, plan = ?, status = ?, notes = ? "
                "WHERE id = ? AND account_id = ?",
                (d["client_id"], d["name"], d["domain"], d["platform"], d["plan"], d["status"], d["notes"], website_id,
                 aid()),
            )
            task_ids = [r["id"] for r in db.execute(
                "SELECT id FROM tasks WHERE website_id = ? AND account_id = ?", (website_id, aid()))]
            if site["status"] == "active" and d["status"] != "active":
                clear_pending(db, task_ids, today())  # nothing new is due while paused/archived
                missed = _count_occurrences(
                    db, "t.website_id = ? AND o.status = 'pending' AND o.due_date < ?", (website_id, today().isoformat())
                )
                if missed:
                    flash(f"{missed} past-due task(s) were never recorded. They stay listed as overdue "
                          "until you record or skip them.", "info")
            elif site["status"] != "active" and d["status"] == "active":
                restart_generation(db, task_ids, today())
            db.commit()
            flash("Website updated.", "success")
            return redirect(url_for("main.website_detail", website_id=website_id))
        values, errors = request.form, f.errors
    return render_template(
        "websites/form.html", values=values, errors=errors, clients=_client_options(db), site=site,
    )


@bp.post("/websites/<int:website_id>/delete")
def website_delete(website_id):
    db = get_db()
    site = _own(db, "websites", website_id)
    files = attachment_files(db, "w.id = ?", (website_id,))
    db.execute("DELETE FROM websites WHERE id = ? AND account_id = ?", (website_id, aid()))
    db.commit()
    remove_files(files)
    flash(f"Website {site['domain']} and its history were deleted.", "success")
    return redirect(url_for("main.websites_list"))


@bp.get("/websites/<int:website_id>/history.csv")
def website_history_csv(website_id):
    db = get_db()
    site = _own(db, "websites", website_id)
    rows = db.execute(
        OCCURRENCE_SELECT + " WHERE o.account_id = ? AND t.website_id = ? AND o.status IN ('completed', 'skipped') "
        "ORDER BY COALESCE(o.completed_on, o.due_date), o.id",
        (aid(), website_id),
    ).fetchall()
    return _csv_response(history_csv(rows), f"history-{site['domain']}.csv")


# ---------------------------------------------------------------- tasks

def _date_arg(args, name):
    """Optional YYYY-MM-DD filter value; invalid input is reported and ignored."""
    raw = (args.get(name) or "").strip()
    if not raw:
        return None
    try:
        value = date.fromisoformat(raw)
        if 2000 <= value.year <= 2100:
            return value
    except ValueError:
        pass
    flash(f"Ignored invalid date \u201c{raw[:20]}\u201d - use YYYY-MM-DD.", "error")
    return None


TASK_VIEWS = {
    "due": "Due & overdue",
    "overdue": "Overdue",
    "upcoming": f"Next {UPCOMING_DAYS} days",
    "completed": "Completed",
    "skipped": "Skipped",
    "history": "All history",
}


def _task_form(src, t):
    f = Form(src)
    f.text("title", "Task title", required=True, max_len=120)
    f.choice("category", "Category", CATEGORIES)
    f.text("description", "Checklist / instructions", max_len=2000, multiline=True, no_secrets=True)
    if f.choice("frequency", "Frequency", FREQUENCIES) == "custom":
        f.integer("interval_days", "Interval (days)", 1, 365)
    else:
        f.data["interval_days"] = None
    start = f.date("start_date", "First due date")
    if start and start < t - timedelta(days=366):
        f.error("start_date", "First due date can't be more than a year in the past.")
    elif start and start > t + timedelta(days=730):
        f.error("start_date", "First due date can't be more than two years ahead.")
    return f


def _check_duplicate_task(db, f, website_id, task_id=None):
    title = f.data.get("title")
    if title and "title" not in f.errors and db.execute(
        "SELECT 1 FROM tasks WHERE account_id = ? AND website_id = ? AND active = 1 AND lower(title) = lower(?) AND id != ?",
        (aid(), website_id, title, task_id or 0),
    ).fetchone():
        f.error("title", "This website already has an active task with this title. Edit that task instead.")


@bp.get("/tasks")
def tasks_list():
    db = get_db()
    sync_schedule(db)
    args = request.args
    view = args.get("view", "due") if args.get("view", "due") in TASK_VIEWS else "due"
    q = args.get("q", "").strip()[:100]
    website_id = int_arg(args, "website_id")
    client_id = int_arg(args, "client_id")
    category = args.get("category") if args.get("category") in CATEGORIES else ""
    t = today()
    ts, soon = t.isoformat(), (t + timedelta(days=UPCOMING_DAYS)).isoformat()
    date_from, date_to = _date_arg(args, "date_from"), _date_arg(args, "date_to")
    if date_from and date_to and date_from > date_to:
        flash("The 'from' date is after the 'to' date, so the date filter was ignored.", "error")
        date_from = date_to = None

    live = "w.status = 'active' AND t.active = 1 AND o.status = 'pending'"
    where, params = {
        "due": (["o.status = 'pending'", "o.due_date <= ?", f"(o.due_date < ? OR ({live}))"], [soon, ts]),
        "overdue": (["o.status = 'pending'", "o.due_date < ?"], [ts]),
        "upcoming": ([live, "o.due_date BETWEEN ? AND ?"], [ts, soon]),
        "completed": (["o.status = 'completed'"], []),
        "skipped": (["o.status = 'skipped'"], []),
        "history": (["o.status IN ('completed', 'skipped')"], []),
    }[view]
    if q:
        where.append(
            "(t.title LIKE ?" + ESC + " OR w.name LIKE ?" + ESC + " OR w.domain LIKE ?" + ESC
            + " OR c.name LIKE ?" + ESC + " OR o.notes LIKE ?" + ESC + ")"
        )
        params += [like(q)] * 5
    for column, value in (("t.website_id", website_id), ("w.client_id", client_id), ("t.category", category)):
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    # Date range applies to the completion date for recorded work, otherwise the due date.
    if date_from:
        where.append("COALESCE(o.completed_on, o.due_date) >= ?")
        params.append(date_from.isoformat())
    if date_to:
        where.append("COALESCE(o.completed_on, o.due_date) <= ?")
        params.append(date_to.isoformat())
    order = (
        "o.due_date, t.title" if view in ("due", "overdue", "upcoming")
        else "COALESCE(o.completed_on, o.due_date) DESC, o.id DESC"
    )
    rows = db.execute(
        OCCURRENCE_SELECT + " WHERE o.account_id = ? AND " + " AND ".join(where) + f" ORDER BY {order} LIMIT 500",
        [aid(), *params],
    ).fetchall()
    return render_template(
        "tasks/list.html", rows=rows, view=view, views=TASK_VIEWS, q=q, website_id=website_id,
        client_id=client_id, category=category, websites=_website_options(db),
        clients=_client_options(db), today=t, date_from=date_from, date_to=date_to,
    )


@bp.get("/tasks/recurring")
def schedules_list():
    db = get_db()
    sync_schedule(db)
    args = request.args
    q = args.get("q", "").strip()[:100]
    website_id = int_arg(args, "website_id")
    category = args.get("category") if args.get("category") in CATEGORIES else ""
    state = args.get("state") if args.get("state") in ("active", "paused") else ""
    ts = today().isoformat()
    where, params = [], [ts, ts, aid()]
    if q:
        where.append("(t.title LIKE ?" + ESC + " OR t.description LIKE ?" + ESC + " OR w.name LIKE ?" + ESC + " OR w.domain LIKE ?" + ESC + ")")
        params += [like(q)] * 4
    if website_id:
        where.append("t.website_id = ?")
        params.append(website_id)
    if category:
        where.append("t.category = ?")
        params.append(category)
    if state:
        where.append("t.active = ?")
        params.append(1 if state == "active" else 0)
    tasks = db.execute(
        "SELECT t.*, w.name AS website_name, w.domain, w.status AS website_status, "
        "(SELECT MIN(o.due_date) FROM occurrences o WHERE o.task_id = t.id AND o.status = 'pending' AND o.due_date >= ?) AS next_due, "
        "(SELECT COUNT(*) FROM occurrences o WHERE o.task_id = t.id AND o.status = 'pending' AND o.due_date < ?) AS overdue "
        "FROM tasks t JOIN websites w ON w.id = t.website_id WHERE t.account_id = ?" + "".join(" AND " + c for c in where)
        + " ORDER BY w.name COLLATE NOCASE, t.title COLLATE NOCASE",
        params,
    ).fetchall()
    return render_template(
        "tasks/schedules.html", tasks=tasks, q=q, website_id=website_id, category=category,
        state=state, websites=_website_options(db), describe_schedule=describe_schedule,
    )


@bp.route("/websites/<int:website_id>/tasks/new", methods=["GET", "POST"])
def task_new(website_id):
    db = get_db()
    site = _own(db, "websites", website_id)
    t = today()
    category = request.args.get("category", "")
    values = {
        "frequency": "weekly", "start_date": t.isoformat(), "category": category,
        "title": CATEGORIES.get(category, "") if category not in ("", "other") else "",
    }
    errors = {}
    if request.method == "POST":
        f = _task_form(request.form, t)
        _check_duplicate_task(db, f, website_id)
        if f.ok:
            d = f.data
            cur = db.execute(
                "INSERT INTO tasks (account_id, website_id, title, category, description, frequency, interval_days, "
                "start_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (aid(), website_id, d["title"], d["category"], d["description"], d["frequency"], d["interval_days"],
                 d["start_date"]),
            )
            db.commit()
            ensure_occurrences(db, horizon(), task_id=cur.lastrowid)
            msg = f"Recurring task “{d['title']}” scheduled ({describe_schedule(d['frequency'], d['interval_days']).lower()})."
            if site["status"] != "active":
                msg += " The website isn't active, so nothing will be due until you set it to Active."
            flash(msg, "success")
            return redirect(url_for("main.website_detail", website_id=website_id))
        values, errors = request.form, f.errors
    return render_template("tasks/form.html", values=values, errors=errors, site=site, task=None)


@bp.route("/tasks/<int:task_id>/edit", methods=["GET", "POST"])
def task_edit(task_id):
    db = get_db()
    task = _own(db, "tasks", task_id)
    site = _own(db, "websites", task["website_id"])
    t = today()
    values, errors = dict(task), {}
    if request.method == "POST":
        f = _task_form(request.form, t)
        if task["active"]:
            _check_duplicate_task(db, f, task["website_id"], task_id)
        if f.ok:
            d = f.data
            schedule_changed = (
                d["frequency"] != task["frequency"]
                or d["interval_days"] != task["interval_days"]
                or d["start_date"] != task["start_date"]
            )
            db.execute(
                "UPDATE tasks SET title = ?, category = ?, description = ?, frequency = ?, interval_days = ?, "
                "start_date = ? WHERE id = ? AND account_id = ?",
                (d["title"], d["category"], d["description"], d["frequency"], d["interval_days"], d["start_date"], task_id,
                 aid()),
            )
            if schedule_changed:
                # Replace unrecorded future due dates; recorded history and past overdue items stay.
                clear_pending(db, [task_id], from_date=t)
                restart_generation(db, [task_id], t)
            db.commit()
            ensure_occurrences(db, horizon(), task_id=task_id)
            flash("Task updated." + (" Upcoming due dates were recalculated." if schedule_changed else ""), "success")
            return redirect(url_for("main.website_detail", website_id=site["id"]))
        values, errors = request.form, f.errors
    return render_template("tasks/form.html", values=values, errors=errors, site=site, task=task)


@bp.post("/tasks/<int:task_id>/toggle")
def task_toggle(task_id):
    db = get_db()
    task = _own(db, "tasks", task_id)
    if task["active"]:
        clear_pending(db, [task_id], today())
        db.execute("UPDATE tasks SET active = 0 WHERE id = ? AND account_id = ?", (task_id, aid()))
        flash(f"“{task['title']}” paused. Future due dates were removed; history and any "
              "past-due unrecorded items are kept.", "success")
    else:
        dup = db.execute(
            "SELECT 1 FROM tasks WHERE account_id = ? AND website_id = ? AND active = 1 AND lower(title) = lower(?) AND id != ?",
            (aid(), task["website_id"], task["title"], task_id),
        ).fetchone()
        if dup:
            flash("Another active task on this website has the same title. Rename one before resuming.", "error")
            return redirect(url_for("main.website_detail", website_id=task["website_id"]))
        restart_generation(db, [task_id], today())
        db.execute("UPDATE tasks SET active = 1 WHERE id = ? AND account_id = ?", (task_id, aid()))
        flash(f"“{task['title']}” resumed from today.", "success")
    db.commit()
    ensure_occurrences(db, horizon(), task_id=task_id)
    return redirect(url_for("main.website_detail", website_id=task["website_id"]))


@bp.post("/tasks/<int:task_id>/delete")
def task_delete(task_id):
    db = get_db()
    task = _own(db, "tasks", task_id)
    recorded = db.execute(
        "SELECT COUNT(*) FROM occurrences WHERE task_id = ? AND account_id = ? AND status IN ('completed', 'skipped')",
        (task_id, aid()),
    ).fetchone()[0]
    if recorded:
        flash(f"“{task['title']}” has {recorded} recorded entr{'y' if recorded == 1 else 'ies'} used in reports, "
              "so it can't be deleted. Pause it instead - history is kept.", "error")
        return redirect(url_for("main.website_detail", website_id=task["website_id"]))
    files = attachment_files(db, "t.id = ?", (task_id,))
    db.execute("DELETE FROM tasks WHERE id = ? AND account_id = ?", (task_id, aid()))
    db.commit()
    remove_files(files)
    flash(f"Task “{task['title']}” and its history were deleted.", "success")
    return redirect(url_for("main.website_detail", website_id=task["website_id"]))


# ---------------------------------------------------------------- recording work

def _occurrence(db, occ_id):
    return fetch_or_404(db, OCCURRENCE_SELECT + " WHERE o.account_id = ? AND o.id = ?", (aid(), occ_id))


def _recorder_field(f):
    """Who did the work - shown on reports so clients know results are self-reported."""
    name = f.text("recorded_by", "Recorded by", required=True, max_len=80, no_secrets=True)
    if f.ok or "recorded_by" not in f.errors:
        session["recorded_by"] = name
    return name


@bp.route("/occurrences/<int:occ_id>/record", methods=["GET", "POST"])
def occurrence_record(occ_id):
    db = get_db()
    occ = _occurrence(db, occ_id)
    attachments = db.execute(
        "SELECT * FROM attachments WHERE occurrence_id = ? AND account_id = ? ORDER BY id", (occ_id, aid())
    ).fetchall()
    events = db.execute(
        "SELECT * FROM occurrence_events WHERE occurrence_id = ? AND account_id = ? ORDER BY id DESC", (occ_id, aid())
    ).fetchall()
    t = today()
    due = date.fromisoformat(occ["due_date"])
    # Work done more than one schedule period early belongs to an earlier occurrence.
    earliest = due - timedelta(days=period_days(occ["frequency"], occ["interval_days"]))
    values = {
        "completed_on": occ["completed_on"] or min(t, max(due, earliest)).isoformat(),
        "result": occ["result"] or "",
        "notes": occ["notes"] if occ["status"] == "completed" else "",
        "recorded_by": occ["recorded_by"] or session.get("recorded_by", ""),
    }
    errors = {}
    if request.method == "POST":
        f = Form(request.form)
        done_on = f.date("completed_on", "Completion date", not_after=t)
        if done_on and done_on < earliest:
            f.error("completed_on", f"Completion date can't be before {earliest.isoformat()} - that is more than "
                                    "one schedule period before this item was due. Record it on the earlier occurrence.")
        result = f.choice("result", "Result", RESULTS)
        notes = f.text("notes", "Notes", max_len=4000, multiline=True, no_secrets=True)
        recorder = _recorder_field(f)
        if result in ("issues_found", "failed") and not notes:
            f.error("notes", "Describe what was found so the report is accurate.")
        elif result == "not_applicable" and not notes:
            f.error("notes", "Say why this task didn't apply this time.")
        cfg = current_app.config
        try:
            accepted = validate_files(
                request.files.getlist("evidence"), cfg["MAX_UPLOAD_BYTES"],
                cfg["MAX_FILES_PER_RECORD"] - len(attachments),
                known_hashes=[a["sha256"] for a in attachments if a["sha256"]],
            )
        except UploadError as exc:
            accepted = []
            f.error("evidence", str(exc))
        if f.ok:
            stored = []
            try:
                db.execute(
                    "UPDATE occurrences SET status = 'completed', completed_on = ?, result = ?, notes = ?, "
                    "recorded_by = ?, recorded_at = datetime('now', 'localtime') WHERE id = ? AND account_id = ?",
                    (f.data["completed_on"], result, notes, recorder, occ_id, aid()),
                )
                store_files(db, occ_id, accepted, cfg["UPLOAD_FOLDER"], stored, aid())
                log_event(db, occ_id, "corrected" if occ["status"] == "completed" else "recorded")
                db.commit()
            except Exception:
                db.rollback()
                remove_files(stored)
                raise
            flash(f"Recorded \u201c{occ['title']}\u201d as “{RESULTS[result]}” (manual record).", "success")
            if result in ("issues_found", "failed"):
                flash("Something needs attention? Log it as an issue so it shows in the monthly report.", "info")
            return redirect(url_for("main.website_detail", website_id=occ["website_id"]))
        values, errors = request.form, f.errors
    return render_template(
        "tasks/record.html", occ=occ, attachments=attachments, events=events, values=values, errors=errors,
        today=t, earliest=earliest, remaining=current_app.config["MAX_FILES_PER_RECORD"] - len(attachments),
    )


@bp.post("/occurrences/<int:occ_id>/skip")
def occurrence_skip(occ_id):
    db = get_db()
    occ = _occurrence(db, occ_id)
    if occ["status"] != "pending":
        flash("Only pending tasks can be skipped.", "error")
        return redirect(url_for("main.website_detail", website_id=occ["website_id"]))
    f = Form(request.form)
    reason = f.text("reason", "Reason for skipping", required=True, max_len=500, no_secrets=True)
    recorder = _recorder_field(f)
    if not f.ok:
        for message in f.errors.values():
            flash(message, "error")
        return redirect(url_for("main.occurrence_record", occ_id=occ_id))
    db.execute(
        "UPDATE occurrences SET status = 'skipped', completed_on = NULL, result = NULL, notes = ?, "
        "recorded_by = ?, recorded_at = datetime('now', 'localtime') WHERE id = ? AND account_id = ?",
        (reason, recorder, occ_id, aid()),
    )
    log_event(db, occ_id, "skipped")
    db.commit()
    flash(f"\u201c{occ['title']}\u201d due {occ['due_date']} marked as skipped.", "success")
    return redirect(url_for("main.website_detail", website_id=occ["website_id"]))


@bp.post("/occurrences/<int:occ_id>/reopen")
def occurrence_reopen(occ_id):
    db = get_db()
    occ = _occurrence(db, occ_id)
    if occ["status"] == "pending":
        return redirect(url_for("main.occurrence_record", occ_id=occ_id))
    db.execute(
        "UPDATE occurrences SET status = 'pending', completed_on = NULL, result = NULL, recorded_at = NULL "
        "WHERE id = ? AND account_id = ?",
        (occ_id, aid()),
    )
    log_event(db, occ_id, "reopened")
    db.commit()
    flash(f"\u201c{occ['title']}\u201d is pending again. The earlier record stays in its history.", "success")
    return redirect(url_for("main.occurrence_record", occ_id=occ_id))


@bp.get("/attachments/<int:att_id>")
def attachment_download(att_id):
    db = get_db()
    att = _own(db, "attachments", att_id)
    return send_from_directory(
        current_app.config["UPLOAD_FOLDER"], att["stored_name"], mimetype=att["content_type"],
        as_attachment=att["content_type"] not in IMAGE_TYPES, download_name=att["original_name"],
    )


@bp.post("/attachments/<int:att_id>/delete")
def attachment_delete(att_id):
    db = get_db()
    att = _own(db, "attachments", att_id)
    db.execute("DELETE FROM attachments WHERE id = ? AND account_id = ?", (att_id, aid()))
    log_event(db, att["occurrence_id"], "evidence_removed")
    db.commit()
    remove_files([att["stored_name"]])
    flash(f"Removed {att['original_name']}.", "success")
    return redirect(url_for("main.occurrence_record", occ_id=att["occurrence_id"]))


# ---------------------------------------------------------------- issues

def _issue_form(db, src, t):
    f = Form(src)
    website_id = f.integer("website_id", "Website", 1, 2**31)
    if website_id and not db.execute(
        "SELECT 1 FROM websites WHERE id = ? AND account_id = ?", (website_id, aid())
    ).fetchone():
        f.error("website_id", "Choose an existing website.")
    f.text("title", "Issue title", required=True, max_len=160)
    f.text("description", "Description", max_len=4000, multiline=True, no_secrets=True)
    f.choice("severity", "Severity", SEVERITIES)
    status = f.choice("status", "Status", ISSUE_STATUSES)
    reported = f.date("reported_on", "Reported on", not_after=t)
    notes = f.text("resolution_notes", "Resolution notes", max_len=4000, multiline=True, no_secrets=True)
    if status in ("resolved", "wont_fix"):
        resolved = f.date("resolved_on", "Resolved on", not_after=t)
        if resolved and reported and resolved < reported:
            f.error("resolved_on", "Resolved date can't be before the reported date.")
        if status == "resolved" and not notes:
            f.error("resolution_notes", "Add a short note on how it was resolved.")
    else:
        f.data["resolved_on"] = None
    # Optional link to the maintenance check that found the issue (same website only).
    occ_id = f.integer("occurrence_id", "Related check", 1, 2**31, required=False)
    if occ_id and website_id and not db.execute(
        "SELECT 1 FROM occurrences o JOIN tasks t ON t.id = o.task_id "
        "WHERE o.id = ? AND t.website_id = ? AND o.account_id = ?",
        (occ_id, website_id, aid()),
    ).fetchone():
        f.data["occurrence_id"] = None
    return f


def _related_checks(db, website_id):
    if not website_id:
        return []
    return db.execute(
        OCCURRENCE_SELECT + " WHERE o.account_id = ? AND t.website_id = ? AND o.status = 'completed' "
        "ORDER BY o.completed_on DESC, o.id DESC LIMIT 30",
        (aid(), website_id),
    ).fetchall()


@bp.get("/issues")
def issues_list():
    db = get_db()
    args = request.args
    q = args.get("q", "").strip()[:100]
    status = args.get("status", "active")
    if status not in ISSUE_STATUSES and status not in ("active", "all"):
        status = "active"
    severity = args.get("severity") if args.get("severity") in SEVERITIES else ""
    website_id = int_arg(args, "website_id")
    where, params = [], [aid()]
    if status == "active":
        where.append("i.status IN ('open', 'in_progress')")
    elif status != "all":
        where.append("i.status = ?")
        params.append(status)
    if severity:
        where.append("i.severity = ?")
        params.append(severity)
    if website_id:
        where.append("i.website_id = ?")
        params.append(website_id)
    if q:
        where.append(
            "(i.title LIKE ?" + ESC + " OR i.description LIKE ?" + ESC + " OR i.resolution_notes LIKE ?" + ESC
            + " OR w.name LIKE ?" + ESC + " OR w.domain LIKE ?" + ESC + ")"
        )
        params += [like(q)] * 5
    issues = db.execute(
        "SELECT i.*, w.name AS website_name, w.domain, c.name AS client_name FROM issues i "
        "JOIN websites w ON w.id = i.website_id JOIN clients c ON c.id = w.client_id WHERE i.account_id = ?"
        + "".join(" AND " + c for c in where)
        + f" ORDER BY (i.status IN ('open', 'in_progress')) DESC, {SEVERITY_ORDER}, i.reported_on DESC",
        params,
    ).fetchall()
    return render_template(
        "issues/list.html", issues=issues, q=q, status=status, severity=severity,
        website_id=website_id, websites=_website_options(db),
    )


@bp.route("/issues/new", methods=["GET", "POST"])
def issue_new():
    db = get_db()
    websites = _website_options(db)
    if not websites:
        flash("Add a website before logging issues.", "info")
        return redirect(url_for("main.websites_list"))
    t = today()
    values = {
        "website_id": request.args.get("website_id", ""), "severity": "medium", "status": "open",
        "reported_on": t.isoformat(), "occurrence_id": request.args.get("occurrence_id", ""),
    }
    errors = {}
    if request.method == "POST":
        f = _issue_form(db, request.form, t)
        if f.ok and f.data["status"] in ("open", "in_progress") and db.execute(
            "SELECT 1 FROM issues WHERE account_id = ? AND website_id = ? AND lower(title) = lower(?) "
            "AND status IN ('open', 'in_progress')",
            (aid(), f.data["website_id"], f.data["title"]),
        ).fetchone():
            f.error("title", "An open issue with this title already exists for this website.")
        if f.ok:
            d = f.data
            db.execute(
                "INSERT INTO issues (account_id, website_id, title, description, severity, status, reported_on, "
                "resolved_on, resolution_notes, occurrence_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (aid(), d["website_id"], d["title"], d["description"], d["severity"], d["status"],
                 d["reported_on"], d["resolved_on"], d["resolution_notes"], d["occurrence_id"]),
            )
            db.commit()
            flash("Issue recorded.", "success")
            return redirect(url_for("main.website_detail", website_id=d["website_id"]))
        values, errors = request.form, f.errors
    checks = _related_checks(db, int_arg(values, "website_id"))
    return render_template(
        "issues/form.html", values=values, errors=errors, websites=websites, issue=None, today=t, checks=checks,
    )


@bp.route("/issues/<int:issue_id>/edit", methods=["GET", "POST"])
def issue_edit(issue_id):
    db = get_db()
    issue = _own(db, "issues", issue_id)
    t = today()
    values, errors = dict(issue), {}
    if request.method == "POST":
        f = _issue_form(db, request.form, t)
        if f.ok:
            d = f.data
            db.execute(
                "UPDATE issues SET website_id = ?, title = ?, description = ?, severity = ?, status = ?, "
                "reported_on = ?, resolved_on = ?, resolution_notes = ?, occurrence_id = ? WHERE id = ? AND account_id = ?",
                (d["website_id"], d["title"], d["description"], d["severity"], d["status"],
                 d["reported_on"], d["resolved_on"], d["resolution_notes"], d["occurrence_id"], issue_id, aid()),
            )
            db.commit()
            flash("Issue updated.", "success")
            return redirect(url_for("main.website_detail", website_id=d["website_id"]))
        values, errors = request.form, f.errors
    return render_template(
        "issues/form.html", values=values, errors=errors, websites=_website_options(db), issue=issue, today=t,
        checks=_related_checks(db, issue["website_id"]),
    )


@bp.post("/issues/<int:issue_id>/delete")
def issue_delete(issue_id):
    db = get_db()
    issue = _own(db, "issues", issue_id)
    db.execute("DELETE FROM issues WHERE id = ? AND account_id = ?", (issue_id, aid()))
    db.commit()
    flash("Issue deleted.", "success")
    return redirect(url_for("main.website_detail", website_id=issue["website_id"]))


# ---------------------------------------------------------------- reports

@bp.get("/reports")
def reports_index():
    db = get_db()
    t = today()
    client_id = int_arg(request.args, "client_id")
    if client_id and ("month" in request.args or "start" in request.args):
        # The "Client report" form submits here; forward to that client's report.
        start, end = request.args.get("start", "").strip(), request.args.get("end", "").strip()
        period = {"start": start, "end": end} if (start or end) else {"month": request.args.get("month", "")}
        return redirect(url_for("main.client_report", client_id=client_id, **period))
    last_month = (t.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    saved = db.execute(
        "SELECT sr.id, sr.title, sr.period_start, sr.period_end, sr.created_at, c.name AS client_name "
        "FROM saved_reports sr JOIN clients c ON c.id = sr.client_id WHERE sr.account_id = ? ORDER BY sr.id DESC LIMIT 10",
        (aid(),),
    ).fetchall()
    return render_template(
        "reports/index.html", websites=_website_options(db), this_month=t.strftime("%Y-%m"),
        last_month=last_month, clients=_client_options(db), saved=saved,
        selected_client=int_arg(request.args, "client_id"),
    )


def _requested_report():
    """Return the report for ?website_id=&month=, or a redirect response for bad input."""
    website_id = int_arg(request.args, "website_id")
    month_raw = request.args.get("month") or today().strftime("%Y-%m")
    parsed = parse_month(month_raw)
    if not website_id or not parsed:
        flash("Choose a website and a valid month (YYYY-MM).", "error")
        return None, redirect(url_for("main.reports_index"))
    report = build_report(get_db(), website_id, parsed[0], parsed[1], today(), account_id=aid())
    if report is None:
        abort(404)
    return report, None


@bp.get("/reports/view")
def report_view():
    report, bounce = _requested_report()
    if bounce:
        return bounce
    return render_template("reports/view.html", r=report)


@bp.get("/reports/print")
def report_print():
    report, bounce = _requested_report()
    if bounce:
        return bounce
    download = request.args.get("download") == "1"
    html = render_template("reports/print.html", r=report, download=download)
    resp = Response(html, mimetype="text/html")
    # The printable page is self-contained, so it carries its own inline stylesheet.
    resp.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; img-src 'self' data:; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    if download:
        name = f"maintenance-report-{report['website']['domain']}-{report['month_key']}.html"
        resp.headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return resp


@bp.get("/reports/csv")
def report_csv_download():
    report, bounce = _requested_report()
    if bounce:
        return bounce
    return _csv_response(
        report_csv(report), f"maintenance-report-{report['website']['domain']}-{report['month_key']}.csv"
    )


# ---------------------------------------------------------------- settings / demo

@bp.get("/settings")
def settings():
    db = get_db()
    counts = {
        table: db.execute(f"SELECT COUNT(*) FROM {table} WHERE account_id = ?", (aid(),)).fetchone()[0]  # constant names
        for table in ("clients", "websites", "tasks", "occurrences", "attachments", "issues")
    }
    return render_template("settings.html", counts=counts)


@bp.post("/settings/demo")
def settings_demo():
    db = get_db()
    has_data = db.execute("SELECT 1 FROM clients WHERE account_id = ? LIMIT 1", (aid(),)).fetchone() is not None
    if has_data and request.form.get("replace") != "yes":
        flash("Tick “replace my current data” to load demo data over existing records.", "error")
        return redirect(url_for("main.settings"))
    load_demo(db, current_app.config["UPLOAD_FOLDER"], today(), aid())
    flash("Demo data loaded. Demo records are labelled [Demo] - they are samples, not real checks.", "success")
    return redirect(url_for("main.dashboard"))


@bp.post("/settings/reset")
def settings_reset():
    if request.form.get("confirm", "").strip() != "RESET":
        flash("Type RESET in capitals to confirm deleting all data.", "error")
        return redirect(url_for("main.settings"))
    wipe_account(get_db(), aid(), current_app.config["UPLOAD_FOLDER"])
    flash("All data and evidence files were deleted.", "success")
    return redirect(url_for("main.dashboard"))


# ---------------------------------------------------------------- client reports & saved reports

SHARE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{40,64}$")
PRINT_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; img-src 'self' data:; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
SNAPSHOT_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"


def _client_report_from(source, client_id):
    """Build the client report described by `source` (query args or form). Returns (report, redirect)."""
    db = get_db()
    _own(db, "clients", client_id, "id")
    period, error = parse_period(source, today())
    if error:
        flash(error, "error")
        return None, redirect(url_for("main.reports_index", client_id=client_id))
    website_id = int_arg(source, "website_id")
    if source.get("website_id") and website_id is None:
        abort(404)
    report = build_client_report(db, client_id, period[0], period[1], today(), website_id, account_id=aid())
    if report is None:
        abort(404)  # website belongs to a different client
    return report, None


def _period_args(cr):
    args = {"start": cr["start"].isoformat(), "end": cr["end"].isoformat()}
    if cr["website_id"]:
        args["website_id"] = cr["website_id"]
    return args


def _report_filename(cr):
    slug = re.sub(r"[^a-z0-9]+", "-", cr["client"]["name"].lower()).strip("-")[:60] or "client"
    return f"maintenance-report-{slug}-{cr['period_key']}"


def _html_response(html, csp):
    resp = Response(html, mimetype="text/html")
    resp.headers["Content-Security-Policy"] = csp
    return resp


@bp.get("/reports/client/<int:client_id>")
def client_report(client_id):
    cr, bounce = _client_report_from(request.args, client_id)
    if bounce:
        return bounce
    db = get_db()
    previous = db.execute(
        "SELECT id, title, period_start, period_end, created_at FROM saved_reports WHERE client_id = ? "
        "AND account_id = ? ORDER BY id DESC LIMIT 5",
        (client_id, aid()),
    ).fetchall()
    websites = db.execute(
        "SELECT id, name, domain FROM websites WHERE client_id = ? AND account_id = ? ORDER BY name COLLATE NOCASE",
        (client_id, aid()),
    ).fetchall()
    return render_template("reports/client.html", cr=cr, args=_period_args(cr), previous=previous, websites=websites)


@bp.get("/reports/client/<int:client_id>/print")
def client_report_print(client_id):
    cr, bounce = _client_report_from(request.args, client_id)
    if bounce:
        return bounce
    download = request.args.get("download") == "1"
    resp = _html_response(render_template("reports/client_print.html", cr=cr, download=download), PRINT_CSP)
    if download:
        resp.headers["Content-Disposition"] = f'attachment; filename="{_report_filename(cr)}.html"'
    return resp


@bp.get("/reports/client/<int:client_id>/csv")
def client_report_csv_download(client_id):
    cr, bounce = _client_report_from(request.args, client_id)
    if bounce:
        return bounce
    return _csv_response(client_report_csv(cr), f"{_report_filename(cr)}.csv")


@bp.post("/reports/client/<int:client_id>/save")
def client_report_save(client_id):
    cr, bounce = _client_report_from(request.form, client_id)
    if bounce:
        return bounce
    f = Form(request.form)
    title = f.text("title", "Report title", max_len=160, no_secrets=True)
    if not f.ok:
        flash(f.errors["title"], "error")
        return redirect(url_for("main.client_report", client_id=client_id, **_period_args(cr)))
    if not title:
        site = f" - {cr['sites'][0]['website']['name']}" if cr["website_id"] and cr["sites"] else ""
        title = f"{cr['client']['name']}{site} - {cr['period_label']}"
    html = render_template("reports/client_print.html", cr=cr, download=True, snapshot_title=title)
    db = get_db()
    cur = db.execute(
        "INSERT INTO saved_reports (account_id, client_id, website_id, period_start, period_end, title, summary, html, "
        "fingerprint) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (aid(), client_id, cr["website_id"], cr["start"].isoformat(), cr["end"].isoformat(), title, cr["summary"], html,
         cr["fingerprint"]),
    )
    db.commit()
    flash("Report saved. The saved copy will not change, even if records are edited later.", "success")
    return redirect(url_for("main.saved_report_detail", report_id=cur.lastrowid))


@bp.get("/reports/saved")
def saved_reports():
    db = get_db()
    q = request.args.get("q", "").strip()[:100]
    client_id = int_arg(request.args, "client_id")
    where, params = [], [aid()]
    if q:
        where.append("(sr.title LIKE ?" + ESC + " OR c.name LIKE ?" + ESC + ")")
        params += [like(q)] * 2
    if client_id:
        where.append("sr.client_id = ?")
        params.append(client_id)
    reports = db.execute(
        "SELECT sr.id, sr.title, sr.period_start, sr.period_end, sr.created_at, sr.share_token IS NOT NULL AS shared, "
        "c.name AS client_name, w.name AS website_name FROM saved_reports sr "
        "JOIN clients c ON c.id = sr.client_id LEFT JOIN websites w ON w.id = sr.website_id WHERE sr.account_id = ?"
        + "".join(" AND " + c for c in where) + " ORDER BY sr.id DESC LIMIT 300",
        params,
    ).fetchall()
    return render_template("reports/saved_list.html", reports=reports, q=q, client_id=client_id,
                           clients=_client_options(db))


def _saved(db, report_id):
    return fetch_or_404(
        db,
        "SELECT sr.*, c.name AS client_name, w.name AS website_name FROM saved_reports sr "
        "JOIN clients c ON c.id = sr.client_id LEFT JOIN websites w ON w.id = sr.website_id "
        "WHERE sr.id = ? AND sr.account_id = ?",
        (report_id, aid()),
    )


@bp.get("/reports/saved/<int:report_id>")
def saved_report_detail(report_id):
    db = get_db()
    sr = _saved(db, report_id)
    start, end = date.fromisoformat(sr["period_start"]), date.fromisoformat(sr["period_end"])
    live = build_client_report(db, sr["client_id"], start, end, today(), sr["website_id"], account_id=aid())
    changed = live is None or live["fingerprint"] != sr["fingerprint"]
    share_url = url_for("main.shared_report", token=sr["share_token"], _external=True) if sr["share_token"] else None
    live_args = {"start": sr["period_start"], "end": sr["period_end"]}
    if sr["website_id"]:
        live_args["website_id"] = sr["website_id"]
    return render_template(
        "reports/saved_detail.html", sr=sr, changed=changed, share_url=share_url, live_args=live_args,
        period=period_label(start, end),
    )


@bp.get("/reports/saved/<int:report_id>/view")
def saved_report_view(report_id):
    return _snapshot_response(_saved(get_db(), report_id)["html"])


@bp.post("/reports/saved/<int:report_id>/share")
def saved_report_share(report_id):
    db = get_db()
    sr = _saved(db, report_id)
    if not sr["share_token"]:
        db.execute("UPDATE saved_reports SET share_token = ? WHERE id = ? AND account_id = ?",
                   (secrets.token_urlsafe(32), report_id, aid()))
        db.commit()
        flash("Share link created. Anyone with the link can view this one report - send it only to the client.", "success")
    return redirect(url_for("main.saved_report_detail", report_id=report_id))


@bp.post("/reports/saved/<int:report_id>/unshare")
def saved_report_unshare(report_id):
    db = get_db()
    _saved(db, report_id)
    db.execute("UPDATE saved_reports SET share_token = NULL WHERE id = ? AND account_id = ?", (report_id, aid()))
    db.commit()
    flash("Share link turned off. The old link no longer works.", "success")
    return redirect(url_for("main.saved_report_detail", report_id=report_id))


@bp.post("/reports/saved/<int:report_id>/delete")
def saved_report_delete(report_id):
    db = get_db()
    sr = _saved(db, report_id)
    db.execute("DELETE FROM saved_reports WHERE id = ? AND account_id = ?", (report_id, aid()))
    db.commit()
    flash(f"Deleted saved report \u201c{sr['title']}\u201d.", "success")
    return redirect(url_for("main.saved_reports"))


def _snapshot_response(html):
    resp = _html_response(html, SNAPSHOT_CSP)
    resp.headers["Cache-Control"] = "private, no-store"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@bp.get("/r/<token>")
def shared_report(token):
    """Public, read-only view of ONE saved report. Only the unguessable token grants access.

    Deliberately not tied to the owner's sign-in or subscription: a report already sent to a
    client keeps working after the trial ends, and always shows the frozen copy.
    """
    if not SHARE_TOKEN_RE.match(token):
        abort(404)
    row = get_db().execute("SELECT html FROM saved_reports WHERE share_token = ?", (token,)).fetchone()
    if row is None:
        abort(404)
    return _snapshot_response(row["html"])
