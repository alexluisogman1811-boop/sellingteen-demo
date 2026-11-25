"""Booking service layer: everything that writes bookings lives here and runs
inside a ``BEGIN IMMEDIATE`` transaction with availability re-validated."""
import secrets
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta

from .availability import Schedule, validate_booking_time
from .db import get_settings, transaction
from .timeutil import (
    from_iso, get_tz, local_to_utc, local_to_utc_lenient, parse_time, to_iso,
)
from .validation import REF_ALPHABET

ACTIVE_STATUSES = ("pending", "confirmed")
STATUSES = ("pending", "confirmed", "cancelled", "completed")
TRANSITIONS = {
    "pending": {"confirmed", "cancelled"},
    "confirmed": {"completed", "cancelled"},
    "cancelled": set(),
    "completed": set(),
}
BUSY_MESSAGE = "The booking system is busy right now. Nothing was saved - please try again in a moment."


class BookingError(Exception):
    pass


@contextmanager
def locked_write(db):
    """Write transaction that turns SQLite lock timeouts and constraint
    violations into user-facing BookingErrors instead of 500 errors."""
    try:
        with transaction(db):
            yield db
    except sqlite3.OperationalError as e:
        if "locked" in str(e) or "busy" in str(e):
            raise BookingError(BUSY_MESSAGE) from None
        raise
    except sqlite3.IntegrityError:
        # Only reachable if the unique start-time backstop fires.
        raise BookingError("That time has already been booked.") from None


def int_setting(settings, key, default=0):
    try:
        return int(settings.get(key, default))
    except (TypeError, ValueError):
        return default


def load_schedule(db, account_id, now, settings=None, exclude_booking_id=None):
    settings = settings or get_settings(db, account_id)
    tz = get_tz(settings["timezone"])

    opening = {}
    for row in db.execute("SELECT * FROM opening_hours WHERE account_id = ?", (account_id,)):
        o, c = parse_time(row["open_time"] or ""), parse_time(row["close_time"] or "")
        opening[row["weekday"]] = (o, c) if row["is_open"] and o and c and o < c else None

    windows = defaultdict(list)
    for row in db.execute("SELECT * FROM availability_rules WHERE account_id = ?", (account_id,)):
        s, e = parse_time(row["start_time"]), parse_time(row["end_time"])
        if s and e and s < e:
            windows[row["weekday"]].append((s, e))

    blocks = [
        (local_to_utc_lenient(datetime.fromisoformat(r["start_local"]), tz),
         local_to_utc_lenient(datetime.fromisoformat(r["end_local"]), tz))
        for r in db.execute("SELECT start_local, end_local FROM blocked_periods WHERE account_id = ?", (account_id,))
    ]

    since = to_iso(now - timedelta(days=2))
    busy = [
        (from_iso(r["start_utc"]), from_iso(r["end_utc"]))
        for r in db.execute(
            "SELECT start_utc, end_utc FROM bookings "
            "WHERE account_id = ? AND status != 'cancelled' AND end_utc >= ? AND id != ?",
            (account_id, since, exclude_booking_id or -1),
        )
    ]

    return Schedule(
        tz=tz,
        opening=opening,
        windows=dict(windows),
        blocks=blocks,
        busy=busy,
        slot_interval=max(5, int_setting(settings, "slot_interval", 15)),
        buffer_minutes=int_setting(settings, "buffer_minutes", 0),
        min_notice_minutes=int_setting(settings, "min_notice_minutes", 0),
        max_advance_days=int_setting(settings, "max_advance_days", 60),
    )


def generate_reference():
    body = "".join(secrets.choice(REF_ALPHABET) for _ in range(10))
    return f"SS-{body[:5]}-{body[5:]}"


def log_event(db, account_id, booking_id, event, detail, actor, now):
    db.execute(
        "INSERT INTO booking_events(account_id, booking_id, event, detail, actor, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (account_id, booking_id, event, detail, actor, to_iso(now)),
    )


def get_booking(db, account_id, booking_id):
    """A booking only if it belongs to ``account_id``: ids from another business are 'not found'."""
    return db.execute("SELECT * FROM bookings WHERE id = ? AND account_id = ?",
                      (booking_id, account_id)).fetchone()


def create_booking(db, account_id, service_id, start_local, name, email, notes, now, actor="customer",
                   is_demo=False, request_token=None, enforce_notice=True, status=None):
    """Validate and insert a booking atomically. Returns the booking row.

    ``request_token`` identifies one submitted booking form. If a booking with
    that token already exists (double-click, network retry, back+resubmit) it
    is returned instead of failing with "already booked".

    Staff-created bookings pass ``enforce_notice=False`` (minimum notice and
    booking-window limits are for customers) and may set ``status``; every
    other rule - hours, blocks, buffers, overlaps, no past times - still applies."""
    if status not in (None, *ACTIVE_STATUSES):
        raise BookingError("Unknown status.")
    with locked_write(db):
        if request_token:
            existing = db.execute(
                "SELECT * FROM bookings WHERE request_token = ?", (request_token,)
            ).fetchone()
            if existing is not None:
                if existing["account_id"] != account_id:
                    raise BookingError("This booking form has expired. Please start again.")
                return existing

        service = db.execute(
            "SELECT * FROM services WHERE id = ? AND account_id = ? AND active = 1", (service_id, account_id)
        ).fetchone()
        if service is None:
            raise BookingError("This service is not available for booking.")
        settings = get_settings(db, account_id)
        # Reload rules and bookings inside the write lock: this is the
        # authoritative availability check, whatever the browser showed.
        schedule = load_schedule(db, account_id, now, settings)
        reason = validate_booking_time(schedule, service["duration_minutes"], start_local, now, enforce_notice)
        if reason:
            raise BookingError(reason)

        start_utc = local_to_utc(start_local, schedule.tz)
        end_utc = start_utc + timedelta(minutes=service["duration_minutes"])
        if status is None:
            status = "confirmed" if settings.get("auto_confirm") == "1" else "pending"

        reference = generate_reference()
        while db.execute("SELECT 1 FROM bookings WHERE reference = ?", (reference,)).fetchone():
            reference = generate_reference()

        cur = db.execute(
            "INSERT INTO bookings(account_id, reference, service_id, service_name, duration_minutes, price_cents, "
            "customer_name, customer_email, notes, start_utc, end_utc, status, is_demo, request_token, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (account_id, reference, service["id"], service["name"], service["duration_minutes"], service["price_cents"],
             name, email, notes, to_iso(start_utc), to_iso(end_utc), status, int(is_demo), request_token,
             to_iso(now), to_iso(now)),
        )
        log_event(db, account_id, cur.lastrowid, "created", f"Booked as {status}", actor, now)
        return get_booking(db, account_id, cur.lastrowid)


def reschedule_booking(db, account_id, booking_id, new_start_local, now, actor="admin", enforce_notice=False):
    with locked_write(db):
        booking = get_booking(db, account_id, booking_id)
        if booking is None:
            raise BookingError("Booking not found.")
        if booking["status"] not in ACTIVE_STATUSES:
            raise BookingError(f"A {booking['status']} booking cannot be rescheduled.")
        settings = get_settings(db, account_id)
        schedule = load_schedule(db, account_id, now, settings, exclude_booking_id=booking_id)
        # The booking keeps its own duration snapshot even if the service changed.
        reason = validate_booking_time(
            schedule, booking["duration_minutes"], new_start_local, now, enforce_notice
        )
        if reason:
            raise BookingError(reason)
        start_utc = local_to_utc(new_start_local, schedule.tz)
        end_utc = start_utc + timedelta(minutes=booking["duration_minutes"])
        old_local = from_iso(booking["start_utc"]).astimezone(schedule.tz)
        db.execute(
            "UPDATE bookings SET start_utc = ?, end_utc = ?, updated_at = ? WHERE id = ? AND account_id = ?",
            (to_iso(start_utc), to_iso(end_utc), to_iso(now), booking_id, account_id),
        )
        log_event(
            db, account_id, booking_id, "rescheduled",
            f"From {old_local:%Y-%m-%d %H:%M} to {new_start_local:%Y-%m-%d %H:%M} ({settings['timezone']})",
            actor, now,
        )
        return get_booking(db, account_id, booking_id)


def update_booking_details(db, account_id, booking_id, name, email, notes, now, actor="admin"):
    """Correct customer contact details / notes. Time changes go through
    reschedule_booking() so availability is always re-checked."""
    with locked_write(db):
        booking = get_booking(db, account_id, booking_id)
        if booking is None:
            raise BookingError("Booking not found.")
        changes = [label for label, old, new in (
            ("name", booking["customer_name"], name),
            ("email", booking["customer_email"], email),
            ("notes", booking["notes"], notes),
        ) if old != new]
        if not changes:
            return booking
        db.execute(
            "UPDATE bookings SET customer_name = ?, customer_email = ?, notes = ?, updated_at = ? "
            "WHERE id = ? AND account_id = ?",
            (name, email, notes, to_iso(now), booking_id, account_id),
        )
        log_event(db, account_id, booking_id, "edited", "Changed " + ", ".join(changes), actor, now)
        return get_booking(db, account_id, booking_id)


def _apply_status(db, booking, new_status, now, actor, reason):
    """Must be called inside a write transaction."""
    if new_status not in TRANSITIONS[booking["status"]]:
        raise BookingError(f"Cannot change a {booking['status']} booking to {new_status}.")
    if new_status == "completed" and from_iso(booking["start_utc"]) > now:
        raise BookingError("An appointment can only be completed after it has started.")
    db.execute(
        "UPDATE bookings SET status = ?, cancel_reason = ?, updated_at = ? WHERE id = ? AND account_id = ?",
        (new_status, reason if new_status == "cancelled" else booking["cancel_reason"],
         to_iso(now), booking["id"], booking["account_id"]),
    )
    log_event(db, booking["account_id"], booking["id"], new_status, reason, actor, now)
    return get_booking(db, booking["account_id"], booking["id"])


def change_status(db, account_id, booking_id, new_status, now, actor="admin", reason=""):
    if new_status not in STATUSES:
        raise BookingError("Unknown status.")
    with locked_write(db):
        booking = get_booking(db, account_id, booking_id)
        if booking is None:
            raise BookingError("Booking not found.")
        return _apply_status(db, booking, new_status, now, actor, reason)


def find_customer_booking(db, account_id, reference, email):
    """Reference AND email must match, within one business only."""
    if not reference or not email:
        return None
    booking = db.execute("SELECT * FROM bookings WHERE reference = ? AND account_id = ?",
                         (reference, account_id)).fetchone()
    if booking is None or not secrets.compare_digest(
        booking["customer_email"].lower().encode(), email.strip().lower().encode()
    ):
        return None
    return booking


def customer_can_cancel(booking, settings, now):
    if booking["status"] not in ACTIVE_STATUSES:
        return False
    cutoff = timedelta(hours=int_setting(settings, "customer_cancel_hours", 24))
    return from_iso(booking["start_utc"]) - now >= cutoff


def customer_cancel(db, account_id, booking_id, now, reason=""):
    # Eligibility is checked inside the same lock as the update, so a
    # concurrent staff action can't slip in between check and write.
    with locked_write(db):
        booking = get_booking(db, account_id, booking_id)
        if booking is None or not customer_can_cancel(booking, get_settings(db, account_id), now):
            raise BookingError("This booking can no longer be cancelled online. Please contact us.")
        return _apply_status(db, booking, "cancelled", now, "customer", reason or "Cancelled by customer")
