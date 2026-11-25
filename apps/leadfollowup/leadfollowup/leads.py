"""Dashboard, follow-ups, lead CRUD, contact history and message generation.

Every query is scoped by the logged-in user's id. A lead that belongs to
someone else is indistinguishable from a lead that doesn't exist (404).
"""
import logging
import re
from datetime import timedelta

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for

from . import ai
from .db import get_db, utcnow_iso
from .followups import (
    CLOSED_STATUSES,
    CONTACT_METHODS,
    FOLLOWUP_STATES,
    NOTE_METHOD,
    SOURCES,
    STATE_SQL,
    STATUSES,
    parse_date,
    preset_date,
    today,
)
from .security import login_required, safe_next

bp = Blueprint("leads", __name__)
log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^[0-9+()\-.\s/]*$")
MAX_SQLITE_INT = 2**63 - 1
SORTS = {
    "followup": "next_follow_up IS NULL, next_follow_up ASC, updated_at DESC",
    "followup_desc": "next_follow_up IS NULL, next_follow_up DESC, updated_at DESC",
    "updated": "updated_at DESC",
    "created": "created_at DESC",
    "name": "name = '', name COLLATE NOCASE ASC",
}
STATUS_FILTERS = ["active", *STATUSES]
LIMITS = {"name": 120, "phone": 40, "email": 254, "service": 160, "notes": 5000}
# Phone numbers are compared with these separators removed, so "0815550199"
# finds "081 555 0199" and "081-555-0199".
_PHONE_DIGITS_SQL = "REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(phone, ' ', ''), '-', ''), '(', ''), ')', ''), '.', '')"


@bp.app_template_global()
def lead_name(lead) -> str:
    """Name to show for a lead; falls back to its contact details."""
    return (lead["name"] or "").strip() or lead["phone"] or lead["email"] or "Unnamed lead"


def get_lead_or_404(lead_id: int):
    if lead_id > MAX_SQLITE_INT:
        abort(404)
    lead = get_db().execute(
        "SELECT * FROM leads WHERE id = ? AND user_id = ?", (lead_id, g.user["id"])
    ).fetchone()
    if lead is None:
        abort(404)
    return lead


def _touch(lead_id: int, **fields):
    fields["updated_at"] = utcnow_iso()
    cols = ", ".join(f"{k} = :{k}" for k in fields)
    get_db().execute(
        f"UPDATE leads SET {cols} WHERE id = :id AND user_id = :uid",
        {**fields, "id": lead_id, "uid": g.user["id"]},
    )
    get_db().commit()


def _back(lead_id: int, anchor: str = ""):
    """Redirect to ?next= if it is a safe local path, otherwise to the lead."""
    target = safe_next(request.form.get("next") or request.args.get("next"), None)
    return redirect(target or url_for("leads.lead_detail", lead_id=lead_id) + anchor)


def _parse_lead_form(form):
    data = {k: " ".join(form.get(k, "").split()) if k != "notes" else form.get(k, "").strip()
            for k in ("name", "phone", "email", "source", "service", "notes", "status")}
    data["next_follow_up"] = form.get("next_follow_up", "").strip()
    errors = {}
    if not (data["name"] or data["phone"] or data["email"]):
        errors["name"] = "Enter the customer's name, or at least a phone number or email."
    for key, limit in LIMITS.items():
        if len(data[key]) > limit:
            errors[key] = f"Must be {limit} characters or fewer."
    if data["phone"] and "phone" not in errors:
        digits = sum(ch.isdigit() for ch in data["phone"])
        if not PHONE_RE.match(data["phone"]) or digits < 6:
            errors["phone"] = "Enter a valid phone number (digits, spaces, + - ( ) only), or leave it blank."
    if data["email"] and "email" not in errors and not EMAIL_RE.match(data["email"]):
        errors["email"] = "Enter a valid email address, or leave it blank."
    if data["source"] not in SOURCES:
        errors["source"] = "Choose where this lead came from."
    if data["status"] not in STATUSES:
        errors["status"] = "Choose a status."
    try:
        due = parse_date(data["next_follow_up"])
        data["next_follow_up"] = due.isoformat() if due else None
    except ValueError:
        errors["next_follow_up"] = "Enter a valid date between 2000 and 2100, or leave it blank."
    return data, errors


def _form_page(lead, errors, mode, status=200):
    return render_template("leads/form.html", lead=lead, errors=errors, mode=mode,
                           sources=SOURCES, statuses=STATUSES), status


def _counts(params):
    return get_db().execute(
        f"""SELECT COALESCE(SUM(status NOT IN ('Won', 'Lost')), 0) AS active,
                   COALESCE(SUM({STATE_SQL['today']}), 0) AS due_today,
                   COALESCE(SUM({STATE_SQL['overdue']}), 0) AS overdue,
                   COALESCE(SUM({STATE_SQL['upcoming']}), 0) AS upcoming,
                   COALESCE(SUM({STATE_SQL['unscheduled']}), 0) AS unscheduled,
                   COALESCE(SUM(status = 'Won'), 0) AS won,
                   COALESCE(SUM(status = 'Lost'), 0) AS lost,
                   COUNT(*) AS total
            FROM leads WHERE user_id = :uid""",
        params,
    ).fetchone()


def _params():
    return {"uid": g.user["id"], "today": today().isoformat()}


@bp.app_context_processor
def nav_counts():
    """Number of follow-ups needing attention, for the navigation badge."""
    if g.get("user") is None:
        return {}
    due = get_db().execute(
        f"SELECT COUNT(*) FROM leads WHERE user_id = :uid AND ({STATE_SQL['overdue']} OR {STATE_SQL['today']})",
        _params(),
    ).fetchone()[0]
    return {"nav_due_count": due}


# ---------------------------------------------------------------- dashboard


@bp.route("/")
def dashboard():
    # Logged-out visitors get the public landing page; everyone else their dashboard.
    if g.user is None:
        return render_template("landing.html")
    db = get_db()
    params = _params()
    stats = _counts(params)
    # Priority order: 1. overdue (most overdue first), 2. due today, 3. upcoming, 4. other.
    overdue = db.execute(
        f"""SELECT * FROM leads WHERE user_id = :uid AND {STATE_SQL['overdue']}
            ORDER BY next_follow_up ASC, updated_at ASC""",
        params,
    ).fetchall()
    due_today = db.execute(
        f"""SELECT * FROM leads WHERE user_id = :uid AND {STATE_SQL['today']}
            ORDER BY updated_at ASC""",
        params,
    ).fetchall()
    upcoming = db.execute(
        f"""SELECT * FROM leads WHERE user_id = :uid AND {STATE_SQL['upcoming']}
            ORDER BY next_follow_up ASC, updated_at ASC LIMIT 5""",
        params,
    ).fetchall()
    unscheduled = db.execute(
        f"""SELECT * FROM leads WHERE user_id = :uid AND {STATE_SQL['unscheduled']}
            ORDER BY created_at DESC LIMIT 5""",
        params,
    ).fetchall()
    return render_template("dashboard.html", stats=stats, overdue=overdue, due_today=due_today,
                           upcoming=upcoming, unscheduled=unscheduled)


@bp.route("/follow-ups")
@login_required
def followups():
    db = get_db()
    params = _params()
    groups = {}
    for state in ("overdue", "today", "upcoming", "unscheduled"):
        order = "created_at DESC" if state == "unscheduled" else "next_follow_up ASC, name"
        groups[state] = db.execute(
            f"SELECT * FROM leads WHERE user_id = :uid AND {STATE_SQL[state]} ORDER BY {order}", params
        ).fetchall()
    return render_template("followups.html", groups=groups)


# ---------------------------------------------------------------- lead list


@bp.route("/leads")
@login_required
def lead_list():
    q = " ".join(request.args.get("q", "").split())[:100]
    status = request.args.get("status", "")
    state = request.args.get("followup", "")
    sort = request.args.get("sort", "followup")
    if status not in STATUS_FILTERS:
        status = ""
    if state not in FOLLOWUP_STATES:
        state = ""
    if sort not in SORTS:
        sort = "followup"

    where = ["user_id = :uid"]
    params = _params()
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params["q"] = f"%{escaped}%"
        clauses = [f"{c} LIKE :q ESCAPE '\\'" for c in ("name", "phone", "email", "service", "notes")]
        digits = re.sub(r"\D", "", q)
        if len(digits) >= 3 and PHONE_RE.match(q):
            params["qdigits"] = f"%{digits}%"
            clauses.append(f"{_PHONE_DIGITS_SQL} LIKE :qdigits")
        where.append("(" + " OR ".join(clauses) + ")")
    if status == "active":
        where.append("status NOT IN ('Won', 'Lost')")
    elif status:
        params["status"] = status
        where.append("status = :status")
    if state:
        where.append(STATE_SQL[state])

    db = get_db()
    leads = db.execute(
        f"SELECT * FROM leads WHERE {' AND '.join(where)} ORDER BY {SORTS[sort]}", params
    ).fetchall()
    has_any = leads or db.execute("SELECT 1 FROM leads WHERE user_id = ? LIMIT 1", (g.user["id"],)).fetchone()
    return render_template(
        "leads/list.html",
        leads=leads,
        has_any=bool(has_any),
        filters={"q": q, "status": status, "followup": state, "sort": sort},
        statuses=STATUSES,
    )


# ---------------------------------------------------------------- create / read / update / delete


@bp.route("/leads/new", methods=["GET", "POST"])
@login_required
def lead_new():
    if request.method == "POST":
        data, errors = _parse_lead_form(request.form)
        if errors:
            return _form_page(data, errors, "new", 400)
        now = utcnow_iso()
        db = get_db()
        cur = db.execute(
            """INSERT INTO leads (user_id, name, phone, email, source, service, notes, status,
                                  next_follow_up, created_at, updated_at)
               VALUES (:uid, :name, :phone, :email, :source, :service, :notes, :status,
                       :next_follow_up, :now, :now)""",
            {**data, "uid": g.user["id"], "now": now},
        )
        db.commit()
        flash(f"Lead “{lead_name(data)}” added.", "success")
        return redirect(url_for("leads.lead_detail", lead_id=cur.lastrowid))
    blank = {"name": "", "phone": "", "email": "", "source": "", "service": "", "notes": "",
             "status": "New", "next_follow_up": today().isoformat()}
    return _form_page(blank, {}, "new")


@bp.route("/leads/<int:lead_id>")
@login_required
def lead_detail(lead_id):
    lead = get_lead_or_404(lead_id)
    history = get_db().execute(
        """SELECT * FROM lead_notes WHERE lead_id = ?
           ORDER BY COALESCE(contact_date, substr(created_at, 1, 10)) DESC, id DESC""",
        (lead_id,),
    ).fetchall()
    last_contact = next((h for h in history if h["method"] != NOTE_METHOD), None)
    return render_template(
        "leads/detail.html", lead=lead, history=history, last_contact=last_contact,
        statuses=STATUSES, styles=ai.STYLES, methods=CONTACT_METHODS, note_method=NOTE_METHOD,
        suggested_next=(today() + timedelta(days=3)).isoformat(),
    )


@bp.route("/leads/<int:lead_id>/edit", methods=["GET", "POST"])
@login_required
def lead_edit(lead_id):
    lead = get_lead_or_404(lead_id)
    if request.method == "POST":
        data, errors = _parse_lead_form(request.form)
        if errors:
            data["id"] = lead_id
            return _form_page(data, errors, "edit", 400)
        _touch(lead_id, **data)
        flash("Lead updated.", "success")
        return redirect(url_for("leads.lead_detail", lead_id=lead_id))
    return _form_page(lead, {}, "edit")


@bp.route("/leads/<int:lead_id>/status", methods=["POST"])
@login_required
def lead_status(lead_id):
    lead = get_lead_or_404(lead_id)
    status = request.form.get("status", "")
    if status not in STATUSES:
        flash("Choose a valid status.", "error")
    elif status != lead["status"]:
        _touch(lead_id, status=status)
        msg = f"Status changed to {status}."
        if status in CLOSED_STATUSES:
            msg += " This lead is now closed and no longer appears in follow-ups."
        elif lead["status"] in CLOSED_STATUSES:
            msg += " The lead is active again"
            msg += " and back in your follow-ups." if lead["next_follow_up"] else "; set a follow-up date to schedule it."
        flash(msg, "success")
    return _back(lead_id)


@bp.route("/leads/<int:lead_id>/follow-up", methods=["POST"])
@login_required
def lead_followup(lead_id):
    lead = get_lead_or_404(lead_id)
    preset = request.form.get("preset", "")
    if preset == "clear":
        _touch(lead_id, next_follow_up=None)
        flash("Follow-up date cleared.", "success")
        return _back(lead_id)
    try:
        due = preset_date(preset) if preset else parse_date(request.form.get("next_follow_up"))
    except ValueError:
        due = None
    if due is None:
        flash("Choose a valid follow-up date.", "error")
    else:
        _touch(lead_id, next_follow_up=due.isoformat())
        msg = f"Next follow-up for {lead_name(lead)} set for {due.strftime('%a %d %b %Y')}."
        if lead["status"] in CLOSED_STATUSES:
            msg += f" It won't appear in follow-ups while the lead is {lead['status']}."
        flash(msg, "success")
    return _back(lead_id)


@bp.route("/leads/<int:lead_id>/activity", methods=["POST"])
@bp.route("/leads/<int:lead_id>/notes", methods=["POST"])
@login_required
def lead_add_activity(lead_id):
    """Record a contact (or an internal note) and optionally reschedule."""
    lead = get_lead_or_404(lead_id)
    form = request.form
    method = form.get("method", NOTE_METHOD).strip() or NOTE_METHOD
    body = form.get("body", "").strip()
    errors = {}
    if method != NOTE_METHOD and method not in CONTACT_METHODS:
        errors["method"] = "Choose how you contacted them."
    if not body:
        errors["body"] = "Write a short summary before saving."
    elif len(body) > 2000:
        errors["body"] = "Keep the summary to 2000 characters or fewer."
    try:
        contact_date = parse_date(form.get("contact_date")) or today()
        if contact_date > today():
            errors["contact_date"] = "The contact date can't be in the future."
    except ValueError:
        contact_date = None
        errors["contact_date"] = "Enter a valid contact date."
    # Only a real contact reschedules; an internal note never moves the follow-up date.
    reschedule = method != NOTE_METHOD and "next_follow_up" in form
    next_due = None
    if reschedule:
        try:
            next_due = parse_date(form.get("next_follow_up"))
        except ValueError:
            errors["next_follow_up"] = "Enter a valid follow-up date, or leave it blank."

    if errors:
        flash("The contact wasn't saved: " + " ".join(errors.values()), "error")
        return redirect(url_for("leads.lead_detail", lead_id=lead_id) + "#history")

    db = get_db()
    db.execute(
        "INSERT INTO lead_notes (lead_id, body, created_at, method, contact_date) VALUES (?, ?, ?, ?, ?)",
        (lead_id, body, utcnow_iso(), method, contact_date.isoformat()),
    )
    db.commit()
    changes, msgs = {}, ["Contact saved." if method != NOTE_METHOD else "Note saved."]
    if method != NOTE_METHOD and lead["status"] == "New":
        changes["status"] = "Contacted"
        msgs.append("Status changed to Contacted.")
    if reschedule:
        changes["next_follow_up"] = next_due.isoformat() if next_due else None
        msgs.append(f"Next follow-up: {next_due.strftime('%a %d %b %Y')}." if next_due else "No further follow-up scheduled.")
    _touch(lead_id, **changes)
    if request.form.get("then") == "dashboard":
        flash(f"{lead_name(lead)}: " + " ".join(msgs), "success")
        return redirect(url_for("leads.dashboard"))
    flash(" ".join(msgs), "success")
    return redirect(url_for("leads.lead_detail", lead_id=lead_id) + "#history")


@bp.route("/leads/<int:lead_id>/delete", methods=["GET", "POST"])
@login_required
def lead_delete(lead_id):
    lead = get_lead_or_404(lead_id)
    if request.method == "POST":
        db = get_db()
        db.execute("DELETE FROM leads WHERE id = ? AND user_id = ?", (lead_id, g.user["id"]))
        db.commit()
        flash(f"Lead “{lead_name(lead)}” deleted.", "success")
        return redirect(url_for("leads.lead_list"))
    return render_template("leads/delete.html", lead=lead)


# ---------------------------------------------------------------- follow-up message


@bp.route("/leads/<int:lead_id>/generate", methods=["POST"])
@login_required
def lead_generate(lead_id):
    lead = get_lead_or_404(lead_id)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        payload = request.form
    style = payload.get("style")
    style = style.lower() if isinstance(style, str) and style else "friendly"
    if style not in ai.STYLES:
        return jsonify(error="Choose Friendly, Professional or Short."), 400
    last = get_db().execute(
        """SELECT contact_date, method, body FROM lead_notes WHERE lead_id = ? AND method != ?
           ORDER BY contact_date DESC, id DESC LIMIT 1""",
        (lead_id, NOTE_METHOD),
    ).fetchone()
    latest = {"date": last["contact_date"], "method": last["method"], "summary": last["body"]} if last else None
    try:
        result = ai.generate_followup(lead, style, g.user["business_name"], latest)
    except Exception as exc:  # noqa: BLE001 - never show a raw error to the user
        log.error("Message generation failed (%s)", type(exc).__name__)
        return jsonify(error="Couldn't create a message right now. Please try again, or write your own."), 500
    return jsonify(result)
