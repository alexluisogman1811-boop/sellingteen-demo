"""First-party product analytics: a few named events in our own database.

No third-party trackers, no cookies, nothing sent off the server. Events are
viewable by admins (ADMIN_EMAILS) at /admin/analytics.
"""
import logging

from . import db as store
from . import settings

log = logging.getLogger(__name__)

EVENTS = {
    "signup": "Sign-ups",
    "quote_generated": "Quotes generated",
    "free_limit_reached": "Free limit reached",
    "upgrade": "Upgrades to Pro",
}


def track(name, user_id=None):
    """Record an event. Never lets an analytics problem break the request."""
    if name not in EVENTS:
        raise ValueError(f"Unknown analytics event: {name}")
    try:
        db = store.get_db()
        db.execute("INSERT INTO events (name, user_id) VALUES (?, ?)", (name, user_id))
        db.commit()
    except Exception:
        log.exception("Could not record analytics event %s", name)


def is_admin(user):
    admins = {e.strip().lower() for e in (settings.get("ADMIN_EMAILS") or "").split(",") if e.strip()}
    return user is not None and user["email"].lower() in admins


def report():
    """Counts of each event (and distinct businesses) for a few time windows."""
    db = store.get_db()
    windows = [("Last 7 days", "-7 days"), ("Last 30 days", "-30 days"), ("All time", None)]
    rows = []
    for name, label in EVENTS.items():
        row = {"label": label, "cells": []}
        for _, delta in windows:
            where, args = "name = ?", [name]
            if delta:
                where += " AND created_at >= datetime('now', ?)"
                args.append(delta)
            total, users = db.execute(
                f"SELECT COUNT(*), COUNT(DISTINCT user_id) FROM events WHERE {where}", args
            ).fetchone()
            row["cells"].append({"total": total, "users": users})
        rows.append(row)
    totals = db.execute(
        "SELECT (SELECT COUNT(*) FROM users), "
        "(SELECT COUNT(*) FROM users WHERE subscription_status IN ('active', 'trialing', 'past_due'))"
    ).fetchone()
    return {"windows": [w[0] for w in windows], "rows": rows,
            "total_users": totals[0], "pro_users": totals[1]}
