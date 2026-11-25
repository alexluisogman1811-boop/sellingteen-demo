"""Demo mode: fictional sample data that can be reset at any time.

Demo mode adds ONE demo business account (demo@example.com / demo1234, booking
page /book/demo). Reset and clear only ever touch that account's rows. Sample
customers use the reserved example.com domain and every sample booking is
flagged ``is_demo = 1``.
"""
import random
from datetime import datetime, time, timedelta

from .availability import slots_for_date
from .bookings import generate_reference, load_schedule
from .db import DEFAULT_SETTINGS, set_settings, transaction
from .timeutil import get_tz, local_to_utc, now_utc, to_iso

DEMO_EMAIL = "demo@example.com"
DEMO_PASSWORD = "demo1234"
DEMO_SLUG = "demo"
DEMO_TZ = "America/New_York"

SERVICES = [
    ("Initial Consultation", "A relaxed first meeting to understand your goals and plan next steps.", 0, 30),
    ("Standard Session", "Our most popular one-to-one appointment.", 6500, 60),
    ("Express Session", "A short, focused appointment for quick questions.", 3500, 30),
    ("Extended Session", "Extra time for in-depth work.", 11000, 90),
    ("Group Workshop", "Currently paused - shown as an example of an inactive service.", 2500, 120),
]

CUSTOMERS = [
    "Avery Brooks", "Jordan Lee", "Sam Patel", "Riley Chen", "Morgan Diaz", "Casey Kim",
    "Taylor Nguyen", "Jamie Okafor", "Robin Silva", "Quinn Murphy", "Drew Ivanova", "Alex Romero",
]


def _wipe(db, account_id):
    for table in ("booking_events", "bookings", "blocked_periods", "availability_rules", "services"):
        # Table names are constants above; rows are limited to this one account.
        db.execute(f"DELETE FROM {table} WHERE account_id = ?", (account_id,))


def clear_data(db, account_id):
    """Remove this account's bookings, services, rules and blocks (settings/login kept)."""
    with transaction(db):
        _wipe(db, account_id)


def ensure_demo_account(db):
    """Create the demo account with sample data if it doesn't exist. Returns the account row."""
    from .accounts import create_account, get_account_by_email
    account = get_account_by_email(db, DEMO_EMAIL)
    if account is None:
        account = create_account(db, "Harbor Lane Studio (Demo)", DEMO_EMAIL, DEMO_PASSWORD, now_utc(),
                                 is_demo=True, slug=DEMO_SLUG)
        reset_demo(db, account["id"])
    return account


def reset_demo(db, account_id, now=None):
    now = now or now_utc()
    tz = get_tz(DEMO_TZ)
    with transaction(db):
        _wipe(db, account_id)
        settings = dict(DEFAULT_SETTINGS)
        settings.update({
            "business_name": "Harbor Lane Studio (Demo)",
            "timezone": DEMO_TZ,
            "contact_email": "hello@example.com",
            "contact_phone": "+1 555 0100",
            "address": "12 Harbor Lane, Example City",
            "about": "A fictional business used to demonstrate SlotSimple. All data here is sample data.",
            "default_duration": "60",
            "slot_interval": "30",
            "buffer_minutes": "15",
            "min_notice_minutes": "120",
            "max_advance_days": "45",
            "auto_confirm": "0",
            "customer_cancel_hours": "24",
        })
        set_settings(db, account_id, settings)

        for weekday in range(7):
            if weekday < 5:
                hours = (1, "09:00", "17:30")
            elif weekday == 5:
                hours = (1, "10:00", "14:00")
            else:
                hours = (0, "09:00", "17:00")
            db.execute(
                "INSERT INTO opening_hours(account_id, weekday, is_open, open_time, close_time) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(account_id, weekday) DO UPDATE SET is_open = excluded.is_open, "
                "open_time = excluded.open_time, close_time = excluded.close_time",
                (account_id, weekday, *hours),
            )
        for weekday in range(5):
            db.execute("INSERT INTO availability_rules(account_id, weekday, start_time, end_time) "
                       "VALUES (?, ?, '09:00', '12:30')", (account_id, weekday))
            db.execute("INSERT INTO availability_rules(account_id, weekday, start_time, end_time) "
                       "VALUES (?, ?, '13:30', '17:30')", (account_id, weekday))
        db.execute("INSERT INTO availability_rules(account_id, weekday, start_time, end_time) "
                   "VALUES (?, 5, '10:00', '14:00')", (account_id,))

        stamp = to_iso(now)
        for i, (name, desc, price, duration) in enumerate(SERVICES):
            db.execute(
                "INSERT INTO services(account_id, name, description, price_cents, duration_minutes, active, "
                "sort_order, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (account_id, name, desc, price, duration, 0 if name == "Group Workshop" else 1, i, stamp, stamp),
            )

        today = now.astimezone(tz).date()
        # An all-day block ~10 days out and a partial block later in the week.
        block_day = today + timedelta(days=10)
        db.execute(
            "INSERT INTO blocked_periods(account_id, start_local, end_local, reason, created_at) VALUES (?, ?, ?, ?, ?)",
            (account_id, f"{block_day}T00:00", f"{block_day + timedelta(days=1)}T00:00", "Staff training day", stamp),
        )
        partial = today + timedelta(days=3)
        db.execute(
            "INSERT INTO blocked_periods(account_id, start_local, end_local, reason, created_at) VALUES (?, ?, ?, ?, ?)",
            (account_id, f"{partial}T13:30", f"{partial}T15:30", "Supplier visit", stamp),
        )

        _seed_bookings(db, account_id, now, tz, today)


def _insert(db, account_id, rng, service, start_local, tz, status, now, customer):
    start = local_to_utc(start_local, tz)
    if start is None:
        return
    end = start + timedelta(minutes=service["duration_minutes"])
    email = customer.lower().replace(" ", ".") + "@example.com"
    notes = rng.choice(["", "", "First visit.", "Please call if running late.", "Prefers the quiet room."])
    cur = db.execute(
        "INSERT INTO bookings(account_id, reference, service_id, service_name, duration_minutes, price_cents, "
        "customer_name, customer_email, notes, start_utc, end_utc, status, cancel_reason, is_demo, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
        (account_id, generate_reference(), service["id"], service["name"], service["duration_minutes"],
         service["price_cents"], customer, email, notes, to_iso(start), to_iso(end), status,
         "Customer asked to cancel" if status == "cancelled" else "", to_iso(now), to_iso(now)),
    )
    db.execute(
        "INSERT INTO booking_events(account_id, booking_id, event, detail, actor, created_at) "
        "VALUES (?, ?, 'created', ?, 'demo', ?)",
        (account_id, cur.lastrowid, f"Sample booking ({status})", to_iso(now)),
    )
    return start, end


def _seed_bookings(db, account_id, now, tz, today):
    rng = random.Random(today.toordinal())
    services = db.execute("SELECT * FROM services WHERE account_id = ? AND active = 1 ORDER BY sort_order",
                          (account_id,)).fetchall()
    customers = iter(CUSTOMERS * 3)

    for offset in range(-6, 0):
        day = today + timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        for hour, status in ((10, "completed"), (14, rng.choice(["completed", "cancelled"]))):
            _insert(db, account_id, rng, rng.choice(services), datetime.combine(day, time(hour)), tz, status, now,
                    next(customers))

    schedule = load_schedule(db, account_id, now)
    statuses = ["confirmed", "confirmed", "pending", "confirmed", "pending", "cancelled"]
    for offset in range(0, 15):
        day = today + timedelta(days=offset)
        picks = 2 if offset < 5 else 1
        for _ in range(picks):
            service = rng.choice(services)
            slots = slots_for_date(schedule, service["duration_minutes"], day, now)
            if not slots:
                continue
            status = rng.choice(statuses)
            span = _insert(db, account_id, rng, service, rng.choice(slots), tz, status, now, next(customers))
            if span and status != "cancelled":
                schedule.busy.append(span)
