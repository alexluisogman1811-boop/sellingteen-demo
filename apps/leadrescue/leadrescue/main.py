from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from flask import Blueprint, current_app, flash, g, redirect, render_template, url_for
from flask_login import current_user, login_required
from sqlalchemy import func

from .dates import app_tz, local_today
from .extensions import db
from .models import CLOSED_STATUSES, LEAD_SOURCES, LEAD_STATUSES, Activity, Lead
from .tenancy import current_business_id, leads_query

bp = Blueprint("main", __name__)


@bp.route("/")
def home():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    return render_template("home.html")


@bp.route("/healthz")
def healthz():
    """Health check for the hosting platform. Reveals nothing except up/down."""
    try:
        db.session.execute(db.text("SELECT 1"))
    except Exception:
        db.session.rollback()
        return {"status": "database unavailable"}, 503
    return {"status": "ok"}


LEGAL_UPDATED = "November 24, 2026"


def _legal_ctx():
    c = current_app.config
    return {"price": c["PRICE_TEXT"], "trial_days": c["TRIAL_DAYS"], "updated": LEGAL_UPDATED,
            "billing_enabled": c.get("BILLING_ENABLED")}


@bp.route("/privacy")
def privacy():
    return render_template("legal/privacy.html", **_legal_ctx())


@bp.route("/terms")
def terms():
    return render_template("legal/terms.html", **_legal_ctx())


@bp.route("/refunds")
def refunds():
    return render_template("legal/refunds.html", **_legal_ctx())


def dashboard_metrics(business_id: int, today: date) -> dict:
    base = db.select(Lead).where(Lead.business_id == business_id).subquery()
    open_cond = base.c.status.not_in(CLOSED_STATUSES)

    def scalar(stmt):
        return db.session.execute(stmt).scalar() or 0

    # Start of the month in the business's time zone, compared against UTC timestamps.
    month_start_dt = datetime(today.year, today.month, 1, tzinfo=app_tz()).astimezone(timezone.utc)

    total = scalar(db.select(func.count()).select_from(base))
    open_count = scalar(db.select(func.count()).select_from(base).where(open_cond))
    pipeline = scalar(db.select(func.sum(base.c.estimated_value)).where(open_cond))
    overdue = scalar(db.select(func.count()).select_from(base).where(open_cond, base.c.next_follow_up_date < today))
    due_today = scalar(db.select(func.count()).select_from(base).where(open_cond, base.c.next_follow_up_date == today))
    no_follow = scalar(db.select(func.count()).select_from(base).where(open_cond, base.c.next_follow_up_date.is_(None)))
    won = scalar(db.select(func.count()).select_from(base).where(base.c.status == "won"))
    lost = scalar(db.select(func.count()).select_from(base).where(base.c.status == "lost"))
    won_value = scalar(db.select(func.sum(base.c.estimated_value)).where(base.c.status == "won"))
    estimates_out = scalar(db.select(func.count()).select_from(base).where(base.c.status.in_(("estimate_sent", "negotiating"))))
    new_this_month = scalar(db.select(func.count()).select_from(base).where(base.c.created_at >= month_start_dt))
    stale_cutoff = today - timedelta(days=14)
    stale = scalar(db.select(func.count()).select_from(base).where(
        open_cond, (base.c.last_contact_date.is_(None)) | (base.c.last_contact_date < stale_cutoff)))

    by_status_rows = dict(db.session.execute(
        db.select(base.c.status, func.count()).group_by(base.c.status)).all())
    by_source_rows = db.session.execute(
        db.select(base.c.source, func.count(), func.sum(base.c.estimated_value)).group_by(base.c.source)).all()
    source_labels = dict(LEAD_SOURCES)

    return dict(
        total=total,
        open_count=open_count,
        pipeline=Decimal(pipeline or 0),
        overdue=overdue,
        due_today=due_today,
        no_follow=no_follow,
        won=won,
        lost=lost,
        won_value=Decimal(won_value or 0),
        win_rate=round(100 * won / (won + lost)) if (won + lost) else None,
        estimates_out=estimates_out,
        new_this_month=new_this_month,
        stale=stale,
        by_status=[(label, by_status_rows.get(key, 0), key) for key, label in LEAD_STATUSES],
        by_source=sorted(
            [(source_labels.get(s, s), c, Decimal(v or 0), s) for s, c, v in by_source_rows],
            key=lambda r: -r[1],
        ),
    )


@bp.route("/dashboard")
@login_required
def dashboard():
    if g.membership is None:
        flash("You're not in any workspace. Ask an owner for an invite link, or delete your account here.", "info")
        return redirect(url_for("data.account"))
    today = local_today()
    metrics = dashboard_metrics(current_business_id(), today)
    recent = db.session.execute(
        db.select(Activity)
        .where(Activity.business_id == current_business_id())
        .order_by(Activity.occurred_at.desc(), Activity.id.desc())
        .limit(8)
    ).scalars().all()
    return render_template("dashboard.html", m=metrics, recent=recent, today=today)


@bp.route("/queue")
@login_required
def queue():
    today = local_today()
    open_q = leads_query().where(Lead.status.not_in(CLOSED_STATUSES))
    overdue = db.session.execute(
        open_q.where(Lead.next_follow_up_date < today).order_by(Lead.next_follow_up_date, Lead.id)
    ).scalars().all()
    due_today = db.session.execute(
        open_q.where(Lead.next_follow_up_date == today).order_by(Lead.estimated_value.desc(), Lead.id)
    ).scalars().all()
    unscheduled = db.session.execute(
        open_q.where(Lead.next_follow_up_date.is_(None)).order_by(Lead.created_at.desc()).limit(25)
    ).scalars().all()
    upcoming = db.session.execute(
        open_q.where(Lead.next_follow_up_date.between(today + timedelta(days=1), today + timedelta(days=7)))
        .order_by(Lead.next_follow_up_date, Lead.id)
    ).scalars().all()
    return render_template(
        "queue.html",
        overdue=overdue,
        due_today=due_today,
        unscheduled=unscheduled,
        upcoming=upcoming,
        today=today,
        default_next=today + timedelta(days=3),
    )
