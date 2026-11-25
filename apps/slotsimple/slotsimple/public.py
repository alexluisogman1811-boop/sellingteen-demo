"""Customer-facing pages for ONE business, at /book/<slug>/...: service list,
booking flow, confirmation and self-service lookup/cancellation.

The business comes only from the slug in the URL (g.biz); every query below is
limited to g.biz["id"], so references, services and bookings of other
businesses can never be seen or changed from here. These pages stay open even
when the owner's trial has ended."""
import calendar
import re
import secrets
from datetime import date, datetime, time, timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for

from .availability import available_dates, last_bookable_day, slots_for_date, validate_booking_time
from .bookings import (
    BUSY_MESSAGE, BookingError, create_booking, customer_can_cancel, customer_cancel, find_customer_booking,
    load_schedule,
)
from .db import get_db
from .security import rate_limit
from .timeutil import local_to_utc_lenient, now_utc, parse_date, parse_time
from .validation import clean_email, clean_name, clean_text, normalize_reference

bp = Blueprint("public", __name__, url_prefix="/book/<slug>")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


@bp.url_value_preprocessor
def _pull_slug(_endpoint, values):
    g.slug = (values or {}).pop("slug", None)


@bp.url_defaults
def _add_slug(_endpoint, values):
    # Links to this business's pages keep its slug without every url_for() naming it.
    if "slug" not in values and g.get("biz") is not None:
        values["slug"] = g.biz["slug"]


def _biz_id():
    return g.biz["id"]


def _remember(key, reference, limit=10):
    refs = [r for r in session.get(key, []) if r != reference]
    refs.append(reference)
    session[key] = refs[-limit:]


def _active_service(service_id):
    service = get_db().execute(
        "SELECT * FROM services WHERE id = ? AND account_id = ? AND active = 1", (service_id, _biz_id())
    ).fetchone()
    if service is None:
        abort(404)
    return service


def _opening_rows(db):
    return db.execute("SELECT * FROM opening_hours WHERE account_id = ? ORDER BY weekday", (_biz_id(),)).fetchall()


@bp.route("")
def index():
    db = get_db()
    services = db.execute(
        "SELECT * FROM services WHERE account_id = ? AND active = 1 ORDER BY sort_order, name", (_biz_id(),)
    ).fetchall()
    return render_template("public/index.html", services=services, opening=_opening_rows(db))


@bp.route("/<int:service_id>")
def choose_time(service_id):
    db = get_db()
    service = _active_service(service_id)
    now = now_utc()
    schedule = load_schedule(db, _biz_id(), now, g.settings)
    today = now.astimezone(schedule.tz).date()
    last_day = last_bookable_day(schedule, now)

    selected = parse_date(request.args.get("date", ""))
    month_arg = request.args.get("month", "")
    try:
        month_start = datetime.strptime(month_arg, "%Y-%m").date() if month_arg else None
    except ValueError:
        month_start = None
    if month_start is None:
        month_start = (selected or today).replace(day=1)
    month_start = min(max(month_start, today.replace(day=1)), last_day.replace(day=1))
    month_end = month_start.replace(day=calendar.monthrange(month_start.year, month_start.month)[1])

    open_days = set(available_dates(schedule, service["duration_minutes"], month_start, month_end, now))
    if selected is None and open_days:
        selected = min(open_days)
    slots = slots_for_date(schedule, service["duration_minutes"], selected, now) if selected else []

    prev_month = (month_start - timedelta(days=1)).replace(day=1)
    next_month = month_end + timedelta(days=1)
    weeks = calendar.Calendar(firstweekday=0).monthdatescalendar(month_start.year, month_start.month)
    return render_template(
        "public/choose_time.html",
        service=service, weeks=weeks, month_start=month_start, open_days=open_days,
        selected=selected, slots=slots, today=today, tz_label=_tz_label(selected or today),
        prev_month=prev_month if prev_month >= today.replace(day=1) else None,
        next_month=next_month if next_month <= last_day else None,
    )


def _requested_start():
    day = parse_date(request.values.get("date", ""))
    at = parse_time(request.values.get("time", ""))
    if day is None or at is None:
        return None
    return datetime.combine(day, at)


def _tz_label(day):
    """Timezone abbreviation in force on ``day`` (EST vs EDT etc.)."""
    return local_to_utc_lenient(datetime.combine(day, time(12)), g.tz).astimezone(g.tz).strftime("%Z")


@bp.route("/<int:service_id>/details", methods=["GET", "POST"])
def details(service_id):
    db = get_db()
    service = _active_service(service_id)
    start_local = _requested_start()
    if start_local is None:
        flash("Please choose a date and time.", "error")
        return redirect(url_for("public.choose_time", service_id=service_id))
    back = url_for("public.choose_time", service_id=service_id, date=start_local.date().isoformat()) + "#times"

    form, errors = {}, {}
    token = request.form.get("request_token", "")
    if not TOKEN_RE.match(token):
        token = secrets.token_urlsafe(18)

    if request.method == "GET":
        # Early, friendly check only. The authoritative check runs again inside
        # the write lock in create_booking() when the form is submitted.
        now = now_utc()
        reason = validate_booking_time(load_schedule(db, _biz_id(), now, g.settings), service["duration_minutes"],
                                       start_local, now)
        if reason:
            flash(reason + " Please pick another time.", "error")
            return redirect(back)
    else:
        if request.form.get("website"):  # honeypot field, hidden from humans
            abort(400)
        form = {k: request.form.get(k, "") for k in ("name", "email", "notes")}
        try:
            name = clean_name(form["name"])
        except ValueError as e:
            errors["name"] = str(e)
        try:
            email = clean_email(form["email"])
        except ValueError as e:
            errors["email"] = str(e)
        try:
            notes = clean_text(form["notes"], "Notes", 1000, multiline=True)
        except ValueError as e:
            errors["notes"] = str(e)

        if not errors:
            if not rate_limit("book", 10, 600):
                abort(429, description="Too many booking attempts. Please wait a few minutes.")
            try:
                booking = create_booking(db, _biz_id(), service_id, start_local, name, email, notes, now_utc(),
                                         request_token=token)
            except BookingError as e:
                if str(e) == BUSY_MESSAGE:
                    errors["form"] = str(e)
                else:
                    flash(f"{e} Please pick another time.", "error")
                    return redirect(back)
            else:
                _remember("my_bookings", booking["reference"])
                return redirect(url_for("public.confirmation", reference=booking["reference"]))

    return render_template(
        "public/details.html", service=service, start_local=start_local, form=form, errors=errors, back=back,
        token=token, tz_label=_tz_label(start_local.date()),
    )


def _owned_booking(reference, session_key):
    reference = normalize_reference(reference)
    if not reference or reference not in session.get(session_key, []):
        return None
    return get_db().execute("SELECT * FROM bookings WHERE reference = ? AND account_id = ?",
                            (reference, _biz_id())).fetchone()


@bp.route("/booking/<reference>/confirmed")
def confirmation(reference):
    booking = _owned_booking(reference, "my_bookings")
    if booking is None:
        flash("For your privacy, please look up your booking with its reference and email.", "info")
        return redirect(url_for("public.manage_lookup"))
    return render_template("public/confirmation.html", booking=booking)


@bp.route("/manage", methods=["GET", "POST"])
def manage_lookup():
    error = None
    reference_value = request.values.get("reference", "")
    if request.method == "POST":
        if not rate_limit("lookup", 10, 900):
            abort(429, description="Too many lookups. Please wait 15 minutes and try again.")
        reference = normalize_reference(reference_value)
        email = request.form.get("email", "")
        booking = find_customer_booking(get_db(), _biz_id(), reference, email) if reference else None
        if booking is None:
            error = "We couldn't find a booking with that reference and email."
        else:
            _remember("managed", booking["reference"])
            return redirect(url_for("public.manage_booking", reference=booking["reference"]))
    return render_template("public/manage_lookup.html", error=error, reference=reference_value)


@bp.route("/manage/<reference>")
def manage_booking(reference):
    booking = _owned_booking(reference, "managed") or _owned_booking(reference, "my_bookings")
    if booking is None:
        return redirect(url_for("public.manage_lookup", reference=reference))
    return render_template(
        "public/manage_booking.html", booking=booking,
        can_cancel=customer_can_cancel(booking, g.settings, now_utc()),
    )


@bp.route("/manage/<reference>/cancel", methods=["POST"])
def manage_cancel(reference):
    booking = _owned_booking(reference, "managed") or _owned_booking(reference, "my_bookings")
    if booking is None:
        abort(403)
    reason = clean_text((request.form.get("reason") or "").strip()[:300], "Reason", 300)
    try:
        customer_cancel(get_db(), _biz_id(), booking["id"], now_utc(), reason=reason)
        flash("Your booking has been cancelled.", "success")
    except BookingError as e:
        flash(str(e), "error")
    return redirect(url_for("public.manage_booking", reference=booking["reference"]))


@bp.app_template_filter("weekday_name")
def weekday_name(value):
    return calendar.day_name[value]


@bp.app_template_global("is_past_day")
def is_past_day(day: date, today: date):
    return day < today
