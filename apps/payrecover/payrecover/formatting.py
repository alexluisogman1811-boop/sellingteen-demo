from datetime import date


def money(cents):
    return f"${(cents or 0) / 100:,.2f}"


def nice_date(value):
    if not value:
        return ""
    d = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    return f"{d:%b} {d.day}, {d.year}"


def plural_days(n):
    return f"{n} day" if n == 1 else f"{n} days"


THAI_MONTHS = ("ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
               "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค.")


def thai_date(value):
    """Due date with a short Thai month. Same year number as the invoice (2026, not 2569), matching the
    other sellingteen apps and the invoice the customer already has."""
    if not value:
        return ""
    d = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    return f"{d.day} {THAI_MONTHS[d.month - 1]} {d.year}"


def thai_days(n):
    return f"{n} วัน"


def register_filters(app):
    app.jinja_env.filters["money"] = money
    app.jinja_env.filters["nice_date"] = nice_date
    app.jinja_env.filters["days"] = plural_days
