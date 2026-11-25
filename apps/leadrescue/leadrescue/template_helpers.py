from datetime import date
from decimal import Decimal

from flask import Flask, g

from .dates import to_local
from .models import ROLE_ADMIN, ROLE_OWNER
from .tenancy import has_role


def money(value) -> str:
    if value is None or value == "":
        return "—"
    return f"${Decimal(value):,.0f}"


def nice_date(value) -> str:
    if not value:
        return "—"
    if isinstance(value, date):
        return value.strftime("%b %d, %Y")
    return str(value)


def localfmt(value, fmt: str = "%b %d, %Y") -> str:
    """Format a stored UTC timestamp in the business's time zone."""
    local = to_local(value)
    return local.strftime(fmt) if local else "—"


def follow_up_state(lead, today: date) -> str:
    if not lead.is_open:
        return "closed"
    d = lead.next_follow_up_date
    if d is None:
        return "none"
    if d < today:
        return "overdue"
    if d == today:
        return "today"
    return "upcoming"


def register_template_helpers(app: Flask) -> None:
    app.jinja_env.filters["money"] = money
    app.jinja_env.filters["nice_date"] = nice_date
    app.jinja_env.filters["localfmt"] = localfmt
    app.jinja_env.globals["follow_up_state"] = follow_up_state

    @app.context_processor
    def inject():
        return dict(
            is_admin=has_role(ROLE_ADMIN),
            is_owner=has_role(ROLE_OWNER),
            active_business=getattr(g, "business", None),
            active_membership=getattr(g, "membership", None),
            all_memberships=getattr(g, "memberships", []),
            legal_operator=app.config.get("LEGAL_ENTITY_NAME") or "[OPERATOR LEGAL NAME — set LEGAL_ENTITY_NAME]",
            legal_contact=app.config.get("SUPPORT_EMAIL") or "[SUPPORT EMAIL — set SUPPORT_EMAIL]",
        )
