"""Owner area for ONE business account. Every route except signup/login requires a logged-in
account, and every query is limited to that account (current_account_id()). Ids from another
account's data are treated as "not found" (404)."""
import csv
import hashlib
import io
import math
import secrets
import zipfile
from datetime import datetime, time, timedelta

from flask import (
    Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, session, url_for,
)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

from . import mailer
from .accounts import (
    MIN_PASSWORD, AccountError, change_slug, create_account, get_account_by_email, set_password,
)

from .availability import available_dates, slots_for_date
from .bookings import (
    STATUSES, TRANSITIONS, BookingError, change_status, create_booking, get_booking, load_schedule,
    reschedule_booking, update_booking_details,
)
from .db import DEFAULT_SETTINGS, get_db, set_settings, transaction
from .public import TOKEN_RE
from .security import admin_required, current_account_id, is_safe_local_path, limiter, login_account, rate_limit
from .timeutil import (
    from_iso, is_valid_timezone, local_to_utc_lenient, now_utc, parse_date, parse_time,
    timezone_choices, to_iso,
)
from .validation import clean_email, clean_int, clean_name, clean_price, clean_text

bp = Blueprint("admin", __name__, url_prefix="/admin")
PER_PAGE = 25
ACTION_TO_STATUS = {"confirm": "confirmed", "cancel": "cancelled", "complete": "completed"}


DUMMY_HASH = generate_password_hash("not-a-real-password")


def _safe_next(target):
    # Local paths only (they already include request.script_root, e.g. /slotsimple/admin/...).
    return target if is_safe_local_path(target) else url_for("admin.dashboard")


def _actor():
    return g.account["email"]


def _local_day_bounds(day):
    start = local_to_utc_lenient(datetime.combine(day, time()), g.tz)
    end = local_to_utc_lenient(datetime.combine(day + timedelta(days=1), time()), g.tz)
    return to_iso(start), to_iso(end)


def _today():
    return now_utc().astimezone(g.tz).date()


# --------------------------------------------------------------------- auth

@bp.route("/setup")
def setup():
    # Replaced by self-service sign-up (one account per business).
    return redirect(url_for("admin.signup"))


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if g.account is not None:
        return redirect(url_for("admin.dashboard"))
    errors, form = {}, {"name": "", "email": ""}
    if request.method == "POST":
        form = {"name": request.form.get("name", ""), "email": request.form.get("email", "")}
        password = request.form.get("password", "")
        try:
            name = clean_text(form["name"], "Business name", 100, required=True)
        except ValueError as e:
            errors["name"] = str(e)
        try:
            email = clean_email(form["email"])
        except ValueError as e:
            errors["email"] = str(e)
        if len(password) < MIN_PASSWORD or len(password) > 200:
            errors["password"] = f"Password must be {MIN_PASSWORD}-200 characters."
        if not errors:
            if not rate_limit("signup", 10, 3600):
                abort(429, description="Too many sign-ups from this connection. Please try again later.")
            try:
                row = create_account(get_db(), name, email, password, now_utc())
            except AccountError as e:
                errors["email"] = str(e)
            else:
                login_account(row)
                flash("Welcome to SlotSimple! Set your timezone and opening hours, then add a service.", "success")
                return redirect(url_for("admin.settings_view"))
    return render_template("admin/signup.html", errors=errors, form=form), (400 if errors else 200)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.account is not None:
        return redirect(url_for("admin.dashboard"))
    error, email = None, ""
    if request.method == "POST":
        if not rate_limit("login", 10, 900):
            abort(429, description="Too many login attempts. Please wait 15 minutes.")
        email = request.form.get("email", "").strip().lower()
        row = get_account_by_email(get_db(), email)
        # Always run a hash check so response time doesn't reveal which emails exist.
        valid = check_password_hash(row["password_hash"] if row else DUMMY_HASH, request.form.get("password", ""))
        if row and valid:
            login_account(row)
            return redirect(_safe_next(request.args.get("next")))
        error = "Invalid email or password."
    return render_template("admin/login.html", error=error, email=email)


@bp.route("/logout", methods=["POST"])
def logout():
    from flask import session
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("admin.login"))


@bp.route("/account", methods=["GET", "POST"])
@admin_required
def account():
    errors = {}
    if request.method == "POST":
        if not check_password_hash(g.account["password_hash"], request.form.get("current", "")):
            errors["current"] = "Current password is incorrect."
        new = request.form.get("password", "")
        if len(new) < MIN_PASSWORD or len(new) > 200:
            errors["password"] = f"Password must be {MIN_PASSWORD}-200 characters."
        elif new != request.form.get("confirm", ""):
            errors["confirm"] = "Passwords do not match."
        if not errors:
            db = get_db()
            set_password(db, current_account_id(), new)
            login_account(db.execute("SELECT * FROM accounts WHERE id = ?", (current_account_id(),)).fetchone())
            flash("Password updated. Other sessions have been signed out.", "success")
            return redirect(url_for("admin.account"))
    return render_template("admin/account.html", errors=errors)


# ------------------------------------------------- export / delete account

# Every table that holds an account's data, in a safe delete order (children first).
ACCOUNT_TABLES = ("booking_events", "bookings", "services", "blocked_periods", "availability_rules",
                  "opening_hours", "settings")
BOM = "﻿"  # so Excel opens the CSV files as UTF-8


def _table_csv(db, table, account_id):
    cur = db.execute(f"SELECT * FROM {table} WHERE account_id = ? ORDER BY rowid", (account_id,))
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([d[0] for d in cur.description])
    for row in cur.fetchall():
        writer.writerow([_csv_safe(v) for v in row])
    return BOM + out.getvalue()


@bp.route("/account/export.zip")
@admin_required
def export_data():
    """All of this account's data as a ZIP of CSV files (one per table). Works after the trial too."""
    db = get_db()
    aid = current_account_id()
    acc = db.execute("SELECT id, slug, email, is_demo, paid_until, created_at FROM accounts WHERE id = ?",
                     (aid,)).fetchone()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(acc.keys())
    writer.writerow([_csv_safe(v) for v in acc])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("account.csv", BOM + out.getvalue())
        for table in reversed(ACCOUNT_TABLES):
            zf.writestr(f"{table}.csv", _table_csv(db, table, aid))
    filename = f"slotsimple-export-{_today().isoformat()}.zip"
    return Response(buf.getvalue(), mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"',
                             "Cache-Control": "no-store"})


@bp.route("/account/delete", methods=["GET", "POST"])
@admin_required
def delete_account():
    db = get_db()
    aid = current_account_id()
    if g.account["is_demo"]:
        flash("The shared demo business can't be deleted. Use Demo, then Clear, to empty it.", "warning")
        return redirect(url_for("admin.account"))
    counts = {
        "bookings": db.execute("SELECT COUNT(*) FROM bookings WHERE account_id = ?", (aid,)).fetchone()[0],
        "services": db.execute("SELECT COUNT(*) FROM services WHERE account_id = ?", (aid,)).fetchone()[0],
    }
    errors = {}
    if request.method == "POST":
        if not rate_limit("delete-account", 10, 900):
            abort(429, description="Too many attempts. Please wait 15 minutes.")
        typed = request.form.get("confirm", "").strip()
        if typed != "DELETE" and typed.lower() != g.account["email"].lower():
            errors["confirm"] = "Type DELETE (or your email) to confirm."
        if not check_password_hash(g.account["password_hash"], request.form.get("password", "")):
            errors["password"] = "That password isn't right. Your account has not been deleted."
        if not errors:
            with transaction(db):
                for table in ACCOUNT_TABLES:
                    db.execute(f"DELETE FROM {table} WHERE account_id = ?", (aid,))
                db.execute("DELETE FROM accounts WHERE id = ?", (aid,))
            session.clear()
            flash("Your account and all its data have been deleted.", "info")
            return redirect(url_for("site.landing"))
    return render_template("admin/delete_account.html", counts=counts, errors=errors), (400 if errors else 200)


# --------------------------------------------- forgot password (optional email)

RESET_MAX_AGE = 3600  # links work for 1 hour


def _mail_on():
    return mailer.mail_enabled(current_app.config.get("MAIL"))


def _reset_serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="pw-reset")


def _pw_fingerprint(password_hash):
    # Changes whenever the password changes, so a used link stops working.
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()[:16]


def _forgot_allowed(email):
    """At most 5 requests per IP per 15 minutes and 3 per email per hour."""
    if not current_app.config.get("RESET_LIMITS", current_app.config.get("RATE_LIMITS", True)):
        return True
    ip_ok = limiter.hit(f"forgot-ip:{request.remote_addr}", 5, 900)
    email_ok = limiter.hit(f"forgot-email:{email}", 3, 3600)
    return ip_ok and email_ok


@bp.route("/forgot", methods=["GET", "POST"])
def forgot_password():
    if not _mail_on():
        abort(404)
    sent = False
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()[:254]
        if email and _forgot_allowed(email):
            row = get_account_by_email(get_db(), email)
            if row is not None and not row["is_demo"]:
                token = _reset_serializer().dumps({"email": row["email"],
                                                   "pw": _pw_fingerprint(row["password_hash"])})
                link = url_for("admin.reset_password", token=token, _external=True)
                mailer.send_mail(current_app.config["MAIL"], row["email"], "Reset your SlotSimple password",
                                 "Someone asked to reset the password for your SlotSimple account.\n\n"
                                 f"To choose a new password, open this link within 1 hour:\n{link}\n\n"
                                 "If this wasn't you, ignore this email. Your password has not changed.\n")
        sent = True  # always the same message, so nobody can tell which emails have accounts
    return render_template("admin/forgot.html", sent=sent)


@bp.route("/reset/<token>", methods=["GET", "POST"])
def reset_password(token):
    if not _mail_on():
        abort(404)
    row = None
    try:
        data = _reset_serializer().loads(token, max_age=RESET_MAX_AGE)
        row = get_account_by_email(get_db(), data.get("email", ""))
        if row is not None and data.get("pw") != _pw_fingerprint(row["password_hash"]):
            row = None
    except (SignatureExpired, BadSignature, AttributeError, TypeError):
        row = None
    if row is None:
        return render_template("admin/reset.html", invalid=True, errors={}), 400
    errors = {}
    if request.method == "POST":
        new = request.form.get("password", "")
        if len(new) < MIN_PASSWORD or len(new) > 200:
            errors["password"] = f"Password must be {MIN_PASSWORD}-200 characters."
        elif new != request.form.get("confirm", ""):
            errors["confirm"] = "Passwords do not match."
        if not errors:
            set_password(get_db(), row["id"], new)
            session.clear()
            flash("Your password has been changed. Please log in.", "success")
            return redirect(url_for("admin.login"))
    return render_template("admin/reset.html", invalid=False, errors=errors), (400 if errors else 200)


# ---------------------------------------------------------------- dashboard

@bp.route("/")
@admin_required
def dashboard():
    db = get_db()
    A = current_account_id()
    now = now_utc()
    today = _today()
    day_start, day_end = _local_day_bounds(today)
    week_end = _local_day_bounds(today + timedelta(days=6))[1]
    todays = db.execute(
        "SELECT * FROM bookings WHERE account_id = ? AND start_utc >= ? AND start_utc < ? "
        "AND status != 'cancelled' ORDER BY start_utc",
        (A, day_start, day_end),
    ).fetchall()
    pending = db.execute(
        "SELECT * FROM bookings WHERE account_id = ? AND status = 'pending' AND start_utc >= ? "
        "ORDER BY start_utc LIMIT 10",
        (A, to_iso(now)),
    ).fetchall()
    stats = {
        "today": len(todays),
        "pending": db.execute("SELECT COUNT(*) FROM bookings WHERE account_id = ? AND status='pending' "
                              "AND start_utc >= ?", (A, to_iso(now))).fetchone()[0],
        "week": db.execute("SELECT COUNT(*) FROM bookings WHERE account_id = ? AND status IN ('pending','confirmed') "
                           "AND start_utc >= ? AND start_utc < ?", (A, to_iso(now), week_end)).fetchone()[0],
        "services": db.execute("SELECT COUNT(*) FROM services WHERE account_id = ? AND active = 1",
                               (A,)).fetchone()[0],
        "past_open": db.execute("SELECT COUNT(*) FROM bookings WHERE account_id = ? AND status IN "
                                "('pending','confirmed') AND end_utc < ?", (A, to_iso(now))).fetchone()[0],
    }
    needs_setup = db.execute("SELECT COUNT(*) FROM availability_rules WHERE account_id = ?",
                             (A,)).fetchone()[0] == 0 or stats["services"] == 0
    return render_template("admin/dashboard.html", todays=todays, pending=pending, stats=stats,
                           today=today, week_last=today + timedelta(days=6), needs_setup=needs_setup,
                           booking_url=url_for("public.index", slug=g.account["slug"], _external=True))


# ----------------------------------------------------------------- bookings

VIEWS = {
    "upcoming": "Upcoming",
    "past": "Past - not closed",
    "completed": "Completed",
    "cancelled": "Cancelled",
}


def _view_sql(view, now_iso):
    if view == "upcoming":
        return "status IN ('pending','confirmed') AND end_utc >= ?", [now_iso]
    if view == "past":
        return "status IN ('pending','confirmed') AND end_utc < ?", [now_iso]
    if view in ("completed", "cancelled"):
        return "status = ?", [view]
    return None, []


def _booking_query(args):
    """Translate filter args into SQL. Returns (where_sql, params, filters)."""
    filters = {k: (args.get(k) or "").strip()
               for k in ("date_from", "date_to", "service_id", "q", "status", "view")}
    if "filtered" not in args:
        filters["view"] = filters["view"] or "upcoming"
    where, params = ["account_id = ?"], [current_account_id()]
    sql, extra = _view_sql(filters["view"], to_iso(now_utc()))
    if sql:
        where.append(sql)
        params += extra
    else:
        filters["view"] = ""
    d_from, d_to = parse_date(filters["date_from"]), parse_date(filters["date_to"])
    if d_from:
        where.append("start_utc >= ?")
        params.append(_local_day_bounds(d_from)[0])
    else:
        filters["date_from"] = ""
    if d_to:
        where.append("start_utc < ?")
        params.append(_local_day_bounds(d_to)[1])
    else:
        filters["date_to"] = ""
    if filters["service_id"].isdigit():
        where.append("service_id = ?")
        params.append(int(filters["service_id"]))
    if filters["status"] in STATUSES:
        where.append("status = ?")
        params.append(filters["status"])
    elif filters["status"] == "active":
        where.append("status IN ('pending','confirmed')")
    else:
        filters["status"] = ""
    if filters["q"]:
        term = "%" + filters["q"][:100].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(customer_name LIKE ? ESCAPE '\\' OR customer_email LIKE ? ESCAPE '\\' "
                     "OR reference LIKE ? ESCAPE '\\')")
        params += [term, term, term]
    return " WHERE " + " AND ".join(where), params, filters


@bp.route("/bookings")
@admin_required
def bookings():
    db = get_db()
    where, params, filters = _booking_query(request.args)
    total = db.execute(f"SELECT COUNT(*) FROM bookings{where}", params).fetchone()[0]
    pages = max(1, math.ceil(total / PER_PAGE))
    try:
        page = min(max(1, int(request.args.get("page", 1))), pages)
    except ValueError:
        page = 1
    # Upcoming reads soonest-first; history views read most-recent-first.
    order = "DESC" if filters["view"] in ("past", "completed", "cancelled") else "ASC"
    rows = db.execute(
        f"SELECT * FROM bookings{where} ORDER BY start_utc {order} LIMIT ? OFFSET ?",
        params + [PER_PAGE, (page - 1) * PER_PAGE],
    ).fetchall()
    services = db.execute("SELECT id, name FROM services WHERE account_id = ? ORDER BY name",
                          (current_account_id(),)).fetchall()
    query = {k: v for k, v in filters.items() if v}
    query["filtered"] = "1"
    now_iso = to_iso(now_utc())
    view_counts = {}
    for key in VIEWS:
        sql, extra = _view_sql(key, now_iso)
        view_counts[key] = db.execute(f"SELECT COUNT(*) FROM bookings WHERE account_id = ? AND {sql}",
                                      [current_account_id()] + extra).fetchone()[0]
    return render_template("admin/bookings.html", rows=rows, filters=filters, services=services,
                           total=total, page=page, pages=pages, query=query, statuses=STATUSES,
                           views=VIEWS, view_counts=view_counts, now_iso=now_iso)


def _admin_booking_token(source):
    token = source.get("request_token", "")
    return token if TOKEN_RE.match(token) else secrets.token_urlsafe(18)


def _customer_fields(form, errors):
    values = {}
    for key, fn in (("name", clean_name), ("email", clean_email),
                    ("notes", lambda v: clean_text(v, "Notes", 1000, multiline=True))):
        try:
            values[key] = fn(form.get(key, ""))
        except ValueError as e:
            errors[key] = str(e)
    return values


@bp.route("/bookings/new", methods=["GET", "POST"])
@admin_required
def booking_new():
    """Staff-created appointment (phone / walk-in). Same availability rules as
    online bookings, except minimum notice and the booking window."""
    db = get_db()
    A = current_account_id()
    services = db.execute("SELECT * FROM services WHERE account_id = ? AND active = 1 ORDER BY sort_order, name",
                          (A,)).fetchall()
    if not services:
        flash("Create an active service before adding appointments.", "error")
        return redirect(url_for("admin.services"))
    src = request.form if request.method == "POST" else request.args
    by_id = {str(s["id"]): s for s in services}
    service = by_id.get(src.get("service_id", ""), services[0])
    now = now_utc()
    schedule = load_schedule(db, A, now, g.settings)
    today = now.astimezone(schedule.tz).date()
    requested = parse_date(src.get("date", ""))
    # Only the GET picker defaults/clamps the date; a submitted date is used
    # exactly as given so a booking can never silently land on another day.
    selected = max(requested or today, today)
    form = {k: src.get(k, "") for k in ("name", "email", "notes", "time", "status")}
    form["status"] = form["status"] if form["status"] in ("pending", "confirmed") else "confirmed"
    token = _admin_booking_token(src)
    errors = {}

    if request.method == "POST":
        values = _customer_fields(request.form, errors)
        at = parse_time(form["time"])
        if requested is None:
            errors["time"] = "Choose a valid date."
        elif requested < today:
            errors["time"] = "That date is in the past."
        elif at is None:
            errors["time"] = "Choose one of the available times."
        if not errors:
            try:
                booking = create_booking(
                    db, A, service["id"], datetime.combine(requested, at), values["name"], values["email"],
                    values["notes"], now, actor=_actor(), request_token=token,
                    enforce_notice=False, status=form["status"],
                )
            except BookingError as e:
                errors["time"] = str(e)
            else:
                flash(f"Appointment {booking['reference']} created. No message was sent to the customer.",
                      "success")
                return redirect(url_for("admin.booking_detail", booking_id=booking["id"]))

    slots = slots_for_date(schedule, service["duration_minutes"], selected, now, enforce_notice=False)
    upcoming = available_dates(schedule, service["duration_minutes"], today, today + timedelta(days=30),
                               now, enforce_notice=False)
    return render_template("admin/booking_new.html", services=services, service=service, selected=selected,
                           slots=slots, upcoming=upcoming[:21], today=today, form=form, errors=errors,
                           token=token)


@bp.route("/bookings/<int:booking_id>/edit", methods=["GET", "POST"])
@admin_required
def booking_edit(booking_id):
    db = get_db()
    booking = get_booking(db, current_account_id(), booking_id) or abort(404)
    errors = {}
    form = {"name": booking["customer_name"], "email": booking["customer_email"], "notes": booking["notes"]}
    if request.method == "POST":
        form = {k: request.form.get(k, "") for k in form}
        values = _customer_fields(request.form, errors)
        if not errors:
            try:
                update_booking_details(db, current_account_id(), booking_id, values["name"], values["email"],
                                       values["notes"], now_utc(), actor=_actor())
            except BookingError as e:
                errors["form"] = str(e)
            else:
                flash("Appointment details saved.", "success")
                return redirect(url_for("admin.booking_detail", booking_id=booking_id))
    return render_template("admin/booking_edit.html", booking=booking, form=form, errors=errors)


def _csv_safe(value):
    text = "" if value is None else str(value)
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text  # neutralise spreadsheet formula injection
    return text


@bp.route("/bookings/export.csv")
@admin_required
def export_csv():
    db = get_db()
    where, params, _ = _booking_query(request.args)
    rows = db.execute(f"SELECT * FROM bookings{where} ORDER BY start_utc", params).fetchall()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Reference", "Date", "Start", "End", "Timezone", "Service", "Duration (min)", "Price",
                     "Status", "Customer name", "Customer email", "Notes", "Cancel reason", "Created (UTC)"])
    tzname = g.settings["timezone"]
    for r in rows:
        start, end = from_iso(r["start_utc"]).astimezone(g.tz), from_iso(r["end_utc"]).astimezone(g.tz)
        writer.writerow([_csv_safe(v) for v in (
            r["reference"], start.strftime("%Y-%m-%d"), start.strftime("%H:%M"), end.strftime("%H:%M"),
            tzname, r["service_name"], r["duration_minutes"], f"{r['price_cents'] / 100:.2f}", r["status"],
            r["customer_name"], r["customer_email"], r["notes"], r["cancel_reason"], r["created_at"],
        )])
    filename = f"slotsimple-bookings-{_today().isoformat()}.csv"
    return Response(
        "﻿" + out.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


@bp.route("/bookings/<int:booking_id>")
@admin_required
def booking_detail(booking_id):
    db = get_db()
    booking = get_booking(db, current_account_id(), booking_id) or abort(404)
    events = db.execute(
        "SELECT * FROM booking_events WHERE booking_id = ? AND account_id = ? ORDER BY id DESC",
        (booking_id, current_account_id()),
    ).fetchall()
    return render_template("admin/booking_detail.html", booking=booking, events=events,
                           allowed=TRANSITIONS[booking["status"]],
                           started=from_iso(booking["start_utc"]) <= now_utc())


@bp.route("/bookings/<int:booking_id>/status", methods=["POST"])
@admin_required
def booking_status(booking_id):
    new_status = ACTION_TO_STATUS.get(request.form.get("action", ""))
    if new_status is None:
        abort(400)
    if get_booking(get_db(), current_account_id(), booking_id) is None:
        abort(404)
    try:
        reason = clean_text(request.form.get("reason", ""), "Reason", 300)
        change_status(get_db(), current_account_id(), booking_id, new_status, now_utc(), actor=_actor(),
                      reason=reason)
        flash(f"Booking {new_status}.", "success")
    except (BookingError, ValueError) as e:
        flash(str(e), "error")
    return redirect(url_for("admin.booking_detail", booking_id=booking_id))


@bp.route("/bookings/<int:booking_id>/reschedule", methods=["GET", "POST"])
@admin_required
def booking_reschedule(booking_id):
    db = get_db()
    A = current_account_id()
    booking = get_booking(db, A, booking_id) or abort(404)
    if booking["status"] not in ("pending", "confirmed"):
        flash(f"A {booking['status']} booking cannot be rescheduled.", "error")
        return redirect(url_for("admin.booking_detail", booking_id=booking_id))
    now = now_utc()

    if request.method == "POST":
        day, at = parse_date(request.form.get("date", "")), parse_time(request.form.get("time", ""))
        if day is None or at is None:
            flash("Choose a valid date and time.", "error")
        else:
            try:
                reschedule_booking(db, A, booking_id, datetime.combine(day, at), now, actor=_actor())
                flash("Booking rescheduled. Remember to let the customer know - no message is sent automatically.",
                      "success")
                return redirect(url_for("admin.booking_detail", booking_id=booking_id))
            except BookingError as e:
                flash(str(e), "error")
        return redirect(url_for("admin.booking_reschedule", booking_id=booking_id,
                                date=request.form.get("date", "")))

    schedule = load_schedule(db, A, now, g.settings, exclude_booking_id=booking_id)
    today = now.astimezone(schedule.tz).date()
    selected = parse_date(request.args.get("date", "")) or from_iso(booking["start_utc"]).astimezone(schedule.tz).date()
    selected = max(selected, today)
    slots = slots_for_date(schedule, booking["duration_minutes"], selected, now, enforce_notice=False)
    upcoming = available_dates(schedule, booking["duration_minutes"], today, today + timedelta(days=30),
                               now, enforce_notice=False)
    return render_template("admin/reschedule.html", booking=booking, selected=selected, slots=slots,
                           upcoming=upcoming[:21], today=today)


@bp.route("/schedule")
@admin_required
def schedule():
    db = get_db()
    day = parse_date(request.args.get("date", "")) or _today()
    start, end = _local_day_bounds(day)
    rows = db.execute(
        "SELECT * FROM bookings WHERE account_id = ? AND start_utc >= ? AND start_utc < ? "
        "AND status != 'cancelled' ORDER BY start_utc",
        (current_account_id(), start, end),
    ).fetchall()
    day_from, day_to = f"{day}T00:00", f"{day + timedelta(days=1)}T00:00"
    blocks = db.execute(
        "SELECT * FROM blocked_periods WHERE account_id = ? AND start_local < ? AND end_local > ? "
        "ORDER BY start_local",
        (current_account_id(), day_to, day_from),
    ).fetchall()
    return render_template("admin/schedule.html", day=day, rows=rows, blocks=blocks,
                           prev_day=day - timedelta(days=1), next_day=day + timedelta(days=1),
                           printed_at=now_utc())


# ----------------------------------------------------------------- services

def _service_form(form):
    errors, values = {}, {}
    for key, fn in (
        ("name", lambda v: clean_text(v, "Name", 100, required=True)),
        ("description", lambda v: clean_text(v, "Description", 1000, multiline=True)),
        ("price_cents", lambda _v: clean_price(form.get("price", ""))),
        ("duration_minutes", lambda v: clean_int(v, "Duration", 5, 480)),
        ("sort_order", lambda v: clean_int(v or "0", "Display order", 0, 999)),
    ):
        try:
            values[key] = fn(form.get(key, ""))
        except ValueError as e:
            errors[key] = str(e)
    if "duration_minutes" in values and values["duration_minutes"] % 5:
        errors["duration_minutes"] = "Duration must be a multiple of 5 minutes."
    values["active"] = 1 if form.get("active") else 0
    return values, errors


@bp.route("/services")
@admin_required
def services():
    rows = get_db().execute(
        "SELECT s.*, (SELECT COUNT(*) FROM bookings b WHERE b.service_id = s.id AND b.account_id = s.account_id) "
        "AS booking_count FROM services s WHERE s.account_id = ? ORDER BY s.sort_order, s.name",
        (current_account_id(),),
    ).fetchall()
    return render_template("admin/services.html", rows=rows)


@bp.route("/services/new", methods=["GET", "POST"])
@bp.route("/services/<int:service_id>/edit", methods=["GET", "POST"])
@admin_required
def service_edit(service_id=None):
    db = get_db()
    service = None
    if service_id is not None:
        service = db.execute("SELECT * FROM services WHERE id = ? AND account_id = ?",
                             (service_id, current_account_id())).fetchone() or abort(404)
    errors = {}
    if request.method == "POST":
        values, errors = _service_form(request.form)
        if not errors:
            stamp = to_iso(now_utc())
            with transaction(db):
                if service is None:
                    db.execute(
                        "INSERT INTO services(account_id, name, description, price_cents, duration_minutes, active, "
                        "sort_order, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (current_account_id(), values["name"], values["description"], values["price_cents"], values["duration_minutes"],
                         values["active"], values["sort_order"], stamp, stamp),
                    )
                else:
                    db.execute(
                        "UPDATE services SET name=?, description=?, price_cents=?, duration_minutes=?, active=?, "
                        "sort_order=?, updated_at=? WHERE id=? AND account_id=?",
                        (values["name"], values["description"], values["price_cents"], values["duration_minutes"],
                         values["active"], values["sort_order"], stamp, service_id, current_account_id()),
                    )
            flash("Service saved. Existing bookings keep their original duration and price.", "success")
            return redirect(url_for("admin.services"))
        form = dict(request.form)
    elif service is not None:
        form = dict(service)
        form["price"] = f"{service['price_cents'] / 100:.2f}"
    else:
        form = {"duration_minutes": g.settings["default_duration"], "active": 1, "price": "0.00", "sort_order": 0}
    return render_template("admin/service_form.html", service=service, form=form, errors=errors)


@bp.route("/services/<int:service_id>/toggle", methods=["POST"])
@admin_required
def service_toggle(service_id):
    db = get_db()
    with transaction(db):
        cur = db.execute("UPDATE services SET active = 1 - active, updated_at = ? WHERE id = ? AND account_id = ?",
                         (to_iso(now_utc()), service_id, current_account_id()))
    if cur.rowcount == 0:
        abort(404)
    flash("Service updated.", "success")
    return redirect(url_for("admin.services"))


@bp.route("/services/<int:service_id>/delete", methods=["POST"])
@admin_required
def service_delete(service_id):
    db = get_db()
    A = current_account_id()
    with transaction(db):
        if db.execute("SELECT 1 FROM services WHERE id = ? AND account_id = ?", (service_id, A)).fetchone() is None:
            abort(404)
        if db.execute("SELECT 1 FROM bookings WHERE service_id = ? AND account_id = ?", (service_id, A)).fetchone():
            flash("This service has bookings, so it can't be deleted. Deactivate it instead.", "error")
        else:
            db.execute("DELETE FROM services WHERE id = ? AND account_id = ?", (service_id, A))
            flash("Service deleted.", "success")
    return redirect(url_for("admin.services"))


# ------------------------------------------------------------- availability

@bp.route("/availability")
@admin_required
def availability():
    db = get_db()
    A = current_account_id()
    rules = db.execute("SELECT * FROM availability_rules WHERE account_id = ? ORDER BY weekday, start_time",
                       (A,)).fetchall()
    today = _today()
    blocks = db.execute(
        "SELECT * FROM blocked_periods WHERE account_id = ? AND end_local > ? ORDER BY start_local",
        (A, f"{today}T00:00"),
    ).fetchall()
    opening = {r["weekday"]: r for r in db.execute("SELECT * FROM opening_hours WHERE account_id = ?", (A,))}
    return render_template("admin/availability.html", rules=rules, blocks=blocks, opening=opening, today=today)


@bp.route("/availability/rules", methods=["POST"])
@admin_required
def rule_add():
    weekdays = sorted({int(d) for d in request.form.getlist("weekday") if d in {"0", "1", "2", "3", "4", "5", "6"}})
    start, end = parse_time(request.form.get("start_time", "")), parse_time(request.form.get("end_time", ""))
    if not weekdays:
        flash("Choose at least one weekday.", "error")
    elif start is None or end is None or start >= end:
        flash("End time must be after start time (overnight windows are not supported).", "error")
    else:
        db = get_db()
        with transaction(db):
            for wd in weekdays:
                db.execute("INSERT INTO availability_rules(account_id, weekday, start_time, end_time) "
                           "VALUES (?, ?, ?, ?)",
                           (current_account_id(), wd, start.strftime("%H:%M"), end.strftime("%H:%M")))
        flash("Availability added. Slots are limited to opening hours on each day.", "success")
    return redirect(url_for("admin.availability"))


@bp.route("/availability/rules/<int:rule_id>/delete", methods=["POST"])
@admin_required
def rule_delete(rule_id):
    db = get_db()
    with transaction(db):
        cur = db.execute("DELETE FROM availability_rules WHERE id = ? AND account_id = ?",
                         (rule_id, current_account_id()))
    if cur.rowcount == 0:
        abort(404)
    flash("Availability window removed. Existing bookings are unaffected.", "success")
    return redirect(url_for("admin.availability"))


@bp.route("/availability/blocks", methods=["POST"])
@admin_required
def block_add():
    f = request.form
    start_day = parse_date(f.get("start_date", ""))
    end_day = parse_date(f.get("end_date", "")) or start_day
    try:
        reason = clean_text(f.get("reason", ""), "Reason", 200)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("admin.availability"))
    if start_day is None:
        flash("Choose a start date for the block.", "error")
        return redirect(url_for("admin.availability"))
    if f.get("all_day"):
        start = datetime.combine(start_day, time())
        end = datetime.combine(end_day + timedelta(days=1), time())
    else:
        st, et = parse_time(f.get("start_time", "")), parse_time(f.get("end_time", ""))
        if st is None or et is None:
            flash("Enter start and end times, or tick 'All day'.", "error")
            return redirect(url_for("admin.availability"))
        start, end = datetime.combine(start_day, st), datetime.combine(end_day, et)
    if end <= start:
        flash("The block must end after it starts.", "error")
        return redirect(url_for("admin.availability"))
    if (end - start).days > 366:
        flash("Blocks can be at most one year long.", "error")
        return redirect(url_for("admin.availability"))

    db = get_db()
    with transaction(db):
        db.execute("INSERT INTO blocked_periods(account_id, start_local, end_local, reason, created_at) "
                   "VALUES (?, ?, ?, ?, ?)",
                   (current_account_id(), start.strftime("%Y-%m-%dT%H:%M"), end.strftime("%Y-%m-%dT%H:%M"), reason, to_iso(now_utc())))
    overlapping = db.execute(
        "SELECT COUNT(*) FROM bookings WHERE account_id = ? AND status IN ('pending','confirmed') "
        "AND start_utc < ? AND end_utc > ?",
        (current_account_id(), to_iso(local_to_utc_lenient(end, g.tz)), to_iso(local_to_utc_lenient(start, g.tz))),
    ).fetchone()[0]
    flash("Blocked time added.", "success")
    if overlapping:
        flash(f"{overlapping} existing booking(s) fall inside this block. They were not changed - "
              "review them in Bookings and reschedule or cancel as needed.", "warning")
    return redirect(url_for("admin.availability"))


@bp.route("/availability/blocks/<int:block_id>/delete", methods=["POST"])
@admin_required
def block_delete(block_id):
    db = get_db()
    with transaction(db):
        cur = db.execute("DELETE FROM blocked_periods WHERE id = ? AND account_id = ?",
                         (block_id, current_account_id()))
    if cur.rowcount == 0:
        abort(404)
    flash("Blocked time removed.", "success")
    return redirect(url_for("admin.availability"))


# ----------------------------------------------------------------- settings

SETTING_RULES = {
    "business_name": lambda v: clean_text(v, "Business name", 100, required=True),
    "contact_email": lambda v: clean_email(v, required=False),
    "contact_phone": lambda v: clean_text(v, "Phone", 40),
    "address": lambda v: clean_text(v, "Address", 200),
    "about": lambda v: clean_text(v, "About", 1000, multiline=True),
    "currency_symbol": lambda v: clean_text(v, "Currency symbol", 4),
    "default_duration": lambda v: str(clean_int(v, "Default appointment duration", 5, 480)),
    "slot_interval": lambda v: str(clean_int(v, "Slot interval", 5, 120)),
    "buffer_minutes": lambda v: str(clean_int(v, "Buffer", 0, 240)),
    "min_notice_minutes": lambda v: str(clean_int(v, "Minimum notice", 0, 60 * 24 * 30)),
    "max_advance_days": lambda v: str(clean_int(v, "Booking window", 1, 365)),
    "customer_cancel_hours": lambda v: str(clean_int(v, "Customer cancellation notice", 0, 720)),
}


@bp.route("/settings", methods=["GET", "POST"])
@admin_required
def settings_view():
    db = get_db()
    errors = {}
    A = current_account_id()
    form = dict(g.settings, booking_slug=g.account["slug"])
    opening = {r["weekday"]: dict(r) for r in db.execute("SELECT * FROM opening_hours WHERE account_id = ?", (A,))}
    if request.method == "POST":
        form = {k: request.form.get(k, "") for k in DEFAULT_SETTINGS}
        form["booking_slug"] = request.form.get("booking_slug", g.account["slug"]).strip().lower()
        cleaned = {}
        for key, fn in SETTING_RULES.items():
            try:
                cleaned[key] = fn(form[key])
            except ValueError as e:
                errors[key] = str(e)
        if is_valid_timezone(form["timezone"]):
            cleaned["timezone"] = form["timezone"]
        else:
            errors["timezone"] = "Choose a timezone from the list."
        cleaned["auto_confirm"] = "1" if request.form.get("auto_confirm") else "0"
        form["auto_confirm"] = cleaned["auto_confirm"]

        for wd in range(7):
            is_open = bool(request.form.get(f"open_{wd}"))
            o_raw, c_raw = request.form.get(f"from_{wd}", ""), request.form.get(f"to_{wd}", "")
            o, c = parse_time(o_raw), parse_time(c_raw)
            prev = opening.get(wd) or {}
            if is_open and (o is None or c is None or o >= c):
                errors[f"hours_{wd}"] = "Closing time must be after opening time."
                opening[wd] = {"weekday": wd, "is_open": 1, "open_time": o_raw[:5], "close_time": c_raw[:5]}
            else:
                # Closed days keep valid times only, so junk input is never stored.
                opening[wd] = {
                    "weekday": wd, "is_open": int(is_open),
                    "open_time": o.strftime("%H:%M") if o else (prev.get("open_time") if parse_time(prev.get("open_time") or "") else "09:00"),
                    "close_time": c.strftime("%H:%M") if c else (prev.get("close_time") if parse_time(prev.get("close_time") or "") else "17:00"),
                }

        if not errors and form["booking_slug"] != g.account["slug"]:
            try:
                change_slug(db, A, form["booking_slug"])
            except AccountError as e:
                errors["booking_slug"] = str(e)
        if not errors:
            tz_changed = cleaned["timezone"] != g.settings["timezone"]
            with transaction(db):
                set_settings(db, A, cleaned)
                for wd, row in opening.items():
                    db.execute(
                        "UPDATE opening_hours SET is_open = ?, open_time = ?, close_time = ? "
                        "WHERE weekday = ? AND account_id = ?",
                        (row["is_open"], row["open_time"] or "09:00", row["close_time"] or "17:00", wd, A),
                    )
            flash("Settings saved.", "success")
            if tz_changed:
                flash("Timezone changed. Existing bookings keep their exact moment in time, so their local "
                      "times are now shown in the new timezone. Opening hours and availability are read as "
                      "local times in the new timezone.", "warning")
            return redirect(url_for("admin.settings_view"))
        flash("Please fix the highlighted fields.", "error")
    return render_template("admin/settings.html", form=form, errors=errors, opening=opening,
                           timezones=timezone_choices())


# --------------------------------------------------------------------- demo

def _require_demo():
    # Only the demo account, and only in demo mode: a real business can never wipe its data here.
    if not current_app.config["DEMO_MODE"] or not g.account["is_demo"]:
        abort(404)


@bp.route("/demo")
@admin_required
def demo():
    _require_demo()
    db = get_db()
    A = current_account_id()
    counts = {
        "bookings": db.execute("SELECT COUNT(*) FROM bookings WHERE account_id = ?", (A,)).fetchone()[0],
        "sample": db.execute("SELECT COUNT(*) FROM bookings WHERE account_id = ? AND is_demo = 1",
                             (A,)).fetchone()[0],
        "services": db.execute("SELECT COUNT(*) FROM services WHERE account_id = ?", (A,)).fetchone()[0],
    }
    return render_template("admin/demo.html", counts=counts)


@bp.route("/demo/reset", methods=["POST"])
@admin_required
def demo_reset():
    _require_demo()
    from .demo import reset_demo
    reset_demo(get_db(), current_account_id(), now_utc())
    flash("Demo data has been reset to fresh sample data.", "success")
    return redirect(url_for("admin.dashboard"))


@bp.route("/demo/clear", methods=["POST"])
@admin_required
def demo_clear():
    _require_demo()
    from .demo import clear_data
    clear_data(get_db(), current_account_id())
    flash("All bookings, services, availability and blocks were removed.", "success")
    return redirect(url_for("admin.demo"))
