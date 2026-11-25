from datetime import date

from flask import Blueprint, g, redirect, render_template, url_for

from .db import get_db
from .invoices import query_invoices, status_clause
from .util import login_required, today

bp = Blueprint("dashboard", __name__)


@bp.get("/")
def home():
    if g.user:
        return redirect(url_for("dashboard.index"))
    return render_template("landing.html")


def compute_stats():
    """All dashboard numbers in one query, using the same status rules as the invoice filters."""
    uid = g.user["id"]
    overdue_sql, p1 = status_clause("overdue")
    soon_sql, p2 = status_clause("due_soon")
    month_start = date(today().year, today().month, 1).isoformat()
    row = get_db().execute(
        f"""
        SELECT
          COUNT(*) AS invoice_count,
          COALESCE(SUM(CASE WHEN i.paid_at IS NULL THEN i.amount_cents END), 0) AS outstanding,
          COUNT(CASE WHEN i.paid_at IS NULL THEN 1 END) AS unpaid_count,
          COALESCE(SUM(CASE WHEN {overdue_sql} THEN i.amount_cents END), 0) AS overdue,
          COUNT(CASE WHEN {overdue_sql} THEN 1 END) AS overdue_count,
          COALESCE(SUM(CASE WHEN {soon_sql} THEN i.amount_cents END), 0) AS due_soon,
          COUNT(CASE WHEN {soon_sql} THEN 1 END) AS due_soon_count,
          COUNT(CASE WHEN i.paid_at IS NOT NULL THEN 1 END) AS paid_count,
          COALESCE(SUM(CASE WHEN i.paid_at >= :month_start THEN i.amount_cents END), 0) AS paid_this_month,
          COUNT(CASE WHEN i.paid_at >= :month_start THEN 1 END) AS paid_this_month_count
        FROM invoices i WHERE i.user_id = :uid
        """,
        {**p1, **p2, "uid": uid, "month_start": month_start},
    ).fetchone()
    return dict(row)


@bp.get("/dashboard")
@login_required
def index():
    db = get_db()
    uid = g.user["id"]
    stats = compute_stats()
    counts = db.execute(
        "SELECT (SELECT COUNT(*) FROM customers WHERE user_id = :u),"
        " (SELECT COUNT(*) FROM followups WHERE user_id = :u)", {"u": uid},
    ).fetchone()
    overdue = sorted(query_invoices("overdue"), key=lambda i: (-i["days_overdue"], i["id"]))
    due_soon = query_invoices("due_soon", limit=10)
    recent = query_invoices(limit=5, order="recent")
    onboarding = [
        {"done": counts[0] > 0, "label": "Add a customer", "href": url_for("customers.new", then="invoice")},
        {"done": stats["invoice_count"] > 0, "label": "Record an invoice you've sent", "href": url_for("invoices.new")},
        {"done": counts[1] > 0, "label": "Send your first follow-up",
         "href": url_for("invoices.index", status="unpaid")},
    ]
    return render_template(
        "dashboard.html", stats=stats, overdue=overdue[:10], overdue_more=max(0, len(overdue) - 10),
        due_soon=due_soon, recent=recent, customer_count=counts[0],
        onboarding=onboarding, onboarding_done=all(s["done"] for s in onboarding),
    )
