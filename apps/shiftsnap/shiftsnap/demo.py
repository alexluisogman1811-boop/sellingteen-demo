"""Clearly labeled sample data, generated relative to the current week."""
from datetime import timedelta

from . import scheduling as sch
from . import services

WEEKDAYS_9_TO_5 = {wd: ("08:00", "18:00") for wd in range(5)}

SAMPLE_EMPLOYEES = [
    # name, role, track availability, availability
    ("Sample: Ana Lopez", "Manager", True, WEEKDAYS_9_TO_5),
    ("Sample: Ben Okafor", "Barista", True, {wd: ("06:00", "14:00") for wd in range(7)}),
    ("Sample: Chloe Martin", "Barista", True, {0: ("12:00", "22:00"), 2: ("12:00", "22:00"),
                                                4: ("12:00", "22:00"), 5: ("09:00", "22:00")}),
    ("Sample: Dev Patel", "Cook", False, {}),
    ("Sample: Emma Novak", "Cashier", True, {wd: ("10:00", "20:00") for wd in (1, 3, 5, 6)}),
    ("Sample: Felix Wong", "Cleaner", True, {wd: ("20:00", "08:00") for wd in range(7)}),
]

# (employee index or None for unfilled, day offset from Monday, start, end, role, notes)
SAMPLE_SHIFTS = [
    (0, 0, "09:00", "17:00", "Manager", "Weekly stock check"),
    (0, 2, "09:00", "17:00", "Manager", ""),
    (0, 4, "09:00", "17:00", "Manager", ""),
    (1, 0, "06:00", "14:00", "Barista", "Opening"),
    (1, 1, "06:00", "14:00", "Barista", "Opening"),
    (1, 3, "06:00", "14:00", "Barista", "Opening"),
    (1, 5, "07:00", "13:00", "Barista", ""),
    (2, 0, "14:00", "22:00", "Barista", "Closing"),
    (2, 2, "14:00", "22:00", "Barista", "Closing"),
    (2, 3, "14:00", "20:00", "Barista", "Outside availability on purpose (demo warning)"),
    (2, 5, "12:00", "20:00", "Barista", ""),
    (3, 0, "10:00", "18:00", "Cook", ""),
    (3, 1, "10:00", "18:00", "Cook", ""),
    (3, 4, "10:00", "18:00", "Cook", ""),
    (3, 5, "10:00", "16:00", "Cook", ""),
    (4, 1, "11:00", "19:00", "Cashier", ""),
    (4, 3, "11:00", "19:00", "Cashier", ""),
    (4, 5, "10:00", "18:00", "Cashier", "Weekend rush"),
    (5, 1, "22:00", "06:00", "Cleaner", "Overnight deep clean"),
    (5, 4, "22:00", "06:00", "Cleaner", "Overnight deep clean"),
    (None, 2, "11:00", "19:00", "Cashier", "Needs cover"),
    (None, 6, "08:00", "14:00", "Barista", "Sunday brunch - needs cover"),
    (None, 9, "17:00", "22:00", "Cook", "Private event - needs cover"),
    (1, 7, "06:00", "14:00", "Barista", "Opening"),
    (0, 7, "09:00", "17:00", "Manager", ""),
    (3, 8, "10:00", "18:00", "Cook", ""),
]


def load_demo_data() -> None:
    """Replace ALL data with the sample set and switch demo mode on."""
    services.clear_all_data()
    ids = []
    for name, role, track, avail in SAMPLE_EMPLOYEES:
        slug = name.split(": ")[1].split()[0].lower()
        ids.append(services.create_employee({
            "name": name, "email": f"{slug}@example.com", "phone": "555-0100",
            "role": role, "notes": "Demo data - not a real person.",
            "track_availability": track, "availability": avail,
        }))
    monday = sch.week_start(services.today())
    for emp_idx, offset, start, end, role, notes in SAMPLE_SHIFTS:
        services.create_shift({
            "employee_id": ids[emp_idx] if emp_idx is not None else None,
            "shift_date": (monday + timedelta(days=offset)).isoformat(),
            "start_time": start, "end_time": end, "role": role,
            "notes": ("[Sample] " + notes).strip(),
        })
    services.set_setting("demo_mode", "1")
