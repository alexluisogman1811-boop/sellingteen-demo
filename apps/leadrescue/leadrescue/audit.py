"""Audit log helper. Records go into the caller's transaction (commit as usual)."""
from flask_login import current_user

from .extensions import db
from .models import AuditEvent


def record(business_id: int, action: str, detail: str = "") -> None:
    """Add an audit event. `detail` must not contain customer names or contact details."""
    user = current_user if getattr(current_user, "is_authenticated", False) else None
    db.session.add(AuditEvent(
        business_id=business_id,
        user_id=user.id if user else None,
        user_name=(user.full_name if user else "support (command line)")[:120],
        action=action,
        detail=detail[:300],
    ))
