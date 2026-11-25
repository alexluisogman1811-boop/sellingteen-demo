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


def register_filters(app):
    app.jinja_env.filters["money"] = money
    app.jinja_env.filters["nice_date"] = nice_date
    app.jinja_env.filters["days"] = plural_days
