from flask import Blueprint, flash, g, redirect, render_template, url_for

from .db import get_db
from .models import REPORT_STATUS_SQL
from .security import login_required

bp = Blueprint("dashboard", __name__)


@bp.route("/")
def landing():
    if g.user:
        return redirect(url_for("dashboard.index"))
    return render_template("landing.html")


@bp.route("/dashboard")
@login_required
def index():
    db = get_db()
    bid = g.business["id"]
    stats = db.execute(
        f"""
        SELECT COUNT(*) AS total,
               COALESCE(SUM(j.status = 'completed'), 0) AS completed,
               COALESCE(SUM(j.status = 'draft'), 0) AS draft,
               COALESCE(SUM(j.status = 'in_progress'), 0) AS in_progress,
               COALESCE(SUM(({REPORT_STATUS_SQL}) = 'shared'), 0) AS shared,
               COALESCE(SUM(({REPORT_STATUS_SQL}) = 'generated'), 0) AS generated
        FROM jobs j LEFT JOIN reports r ON r.job_id = j.id
        WHERE j.business_id = ?
        """,
        (bid,),
    ).fetchone()
    recent = db.execute(
        f"""
        SELECT j.*, ({REPORT_STATUS_SQL}) AS report_status
        FROM jobs j LEFT JOIN reports r ON r.job_id = j.id
        WHERE j.business_id = ?
        ORDER BY j.created_at DESC, j.id DESC LIMIT 6
        """,
        (bid,),
    ).fetchall()
    return render_template("dashboard.html", stats=stats, recent=recent)


@bp.route("/demo", methods=["POST"])
@login_required
def load_demo():
    from .demo import create_demo_job
    job_id = create_demo_job(g.business["id"])
    flash("Demo job added. Try clicking “Generate Report”.", "success")
    return redirect(url_for("jobs.detail", job_id=job_id))
