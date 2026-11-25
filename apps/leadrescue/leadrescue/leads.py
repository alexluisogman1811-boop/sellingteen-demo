from datetime import date, datetime, time, timedelta, timezone
from .dates import local_today

from flask import Blueprint, Response, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_

from . import audit
from .csv_io import IMPORT_COLUMNS, CSVImportError, export_leads, parse_import
from .extensions import db
from .forms import ActivityForm, DraftRequestForm, ImportForm, LeadForm, SnoozeForm, StatusForm
from .security import is_safe_redirect, is_same_app_referrer, with_script_root
from .models import (
    CLOSED_STATUSES,
    CONTACT_ACTIVITY_TYPES,
    LEAD_SOURCES,
    LEAD_STATUSES,
    ROLE_ADMIN,
    STATUS_LABELS,
    Activity,
    Lead,
)
from .tenancy import (
    business_members,
    current_business_id,
    get_activity_or_404,
    get_lead_or_404,
    leads_query,
    member_ids,
    require_role,
)

bp = Blueprint("leads", __name__, url_prefix="/leads")

LEAD_FIELDS = [
    "first_name", "last_name", "phone", "email", "address", "city", "state", "zip_code",
    "source", "status", "job_type", "estimated_value", "last_contact_date", "next_follow_up_date", "notes",
]
SORTS = {
    "follow_up": (Lead.next_follow_up_date.is_(None), Lead.next_follow_up_date.asc(), Lead.id.desc()),
    "newest": (Lead.created_at.desc(), Lead.id.desc()),
    "oldest": (Lead.created_at.asc(), Lead.id.asc()),
    "value": (Lead.estimated_value.is_(None), Lead.estimated_value.desc(), Lead.id.desc()),
    "name": (Lead.last_name.asc(), Lead.first_name.asc(), Lead.id.asc()),
    "last_contact": (Lead.last_contact_date.is_(None), Lead.last_contact_date.desc(), Lead.id.desc()),
}
FOLLOW_UP_FILTERS = ("overdue", "today", "week", "none")


def _log(lead: Lead, type_: str, body: str = "", occurred_at: datetime | None = None) -> None:
    db.session.add(Activity(
        business_id=lead.business_id,
        lead_id=lead.id,
        user_id=current_user.id,
        type=type_,
        body=body[:2000],
        occurred_at=occurred_at or datetime.now(timezone.utc),
    ))


def _prepare_lead_form(form: LeadForm) -> None:
    form.assigned_to_id.choices = [(0, "Unassigned")] + [(u.id, u.full_name) for u in business_members()]


def _apply_form(lead: Lead, form: LeadForm) -> None:
    for field in LEAD_FIELDS:
        setattr(lead, field, getattr(form, field).data)
    for field in ("last_name", "phone", "email", "address", "city", "state", "zip_code", "job_type", "notes"):
        if getattr(lead, field) is None:
            setattr(lead, field, "")
    assigned = form.assigned_to_id.data or None
    # Defence in depth: the select choices already restrict this, but re-check membership.
    if assigned is not None and assigned not in member_ids():
        abort(400)
    lead.assigned_to_id = assigned


def filtered_leads_query(args):
    """Build the tenant-scoped, filtered query from request args. Returns (query, filters)."""
    q = (args.get("q") or "").strip()[:100]
    status = args.get("status") or ""
    source = args.get("source") or ""
    follow = args.get("follow_up") or ""
    assigned = args.get("assigned") or ""
    demo = args.get("demo") or ""
    sort = args.get("sort") if args.get("sort") in SORTS else "follow_up"

    query = leads_query()
    if q:
        # Bound parameters only; LIKE wildcards in user input are escaped.
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped.lower()}%"
        digits = "".join(ch for ch in q if ch.isdigit())
        conditions = [
            func.lower(Lead.first_name).like(pattern, escape="\\"),
            func.lower(Lead.last_name).like(pattern, escape="\\"),
            func.lower(Lead.first_name + " " + Lead.last_name).like(pattern, escape="\\"),
            func.lower(Lead.email).like(pattern, escape="\\"),
            func.lower(Lead.address).like(pattern, escape="\\"),
            func.lower(Lead.city).like(pattern, escape="\\"),
            func.lower(Lead.job_type).like(pattern, escape="\\"),
            Lead.phone.like(pattern, escape="\\"),
        ]
        if len(digits) >= 3:
            stripped_phone = func.replace(func.replace(func.replace(func.replace(func.replace(
                Lead.phone, "(", ""), ")", ""), "-", ""), " ", ""), ".", "")
            conditions.append(stripped_phone.like(f"%{digits}%"))
        query = query.where(or_(*conditions))
    if status == "open":
        query = query.where(Lead.status.not_in(CLOSED_STATUSES))
    elif status in STATUS_LABELS:
        query = query.where(Lead.status == status)
    else:
        status = ""
    if source in dict(LEAD_SOURCES):
        query = query.where(Lead.source == source)
    else:
        source = ""
    today = local_today()
    if follow in FOLLOW_UP_FILTERS:
        open_only = Lead.status.not_in(CLOSED_STATUSES)
        if follow == "overdue":
            query = query.where(open_only, Lead.next_follow_up_date < today)
        elif follow == "today":
            query = query.where(open_only, Lead.next_follow_up_date == today)
        elif follow == "week":
            query = query.where(open_only, Lead.next_follow_up_date.between(today, today + timedelta(days=7)))
        else:
            query = query.where(open_only, Lead.next_follow_up_date.is_(None))
    else:
        follow = ""
    if assigned == "me":
        query = query.where(Lead.assigned_to_id == current_user.id)
    elif assigned == "none":
        query = query.where(Lead.assigned_to_id.is_(None))
    elif assigned.isdigit() and int(assigned) in member_ids():
        query = query.where(Lead.assigned_to_id == int(assigned))
    else:
        assigned = ""
    if demo == "hide":
        query = query.where(Lead.is_demo.is_(False))
    elif demo == "only":
        query = query.where(Lead.is_demo.is_(True))
    else:
        demo = ""
    query = query.order_by(*SORTS[sort])
    filters = dict(q=q, status=status, source=source, follow_up=follow, assigned=assigned, demo=demo, sort=sort)
    return query, filters


@bp.route("/")
@login_required
def index():
    query, filters = filtered_leads_query(request.args)
    page = request.args.get("page", "1")
    page = int(page) if page.isdigit() and 0 < int(page) < 100000 else 1
    pagination = db.paginate(query, page=page, per_page=current_app.config["LEADS_PER_PAGE"], error_out=False)
    return render_template(
        "leads/index.html",
        pagination=pagination,
        leads=pagination.items,
        filters=filters,
        members=business_members(),
        statuses=LEAD_STATUSES,
        sources=LEAD_SOURCES,
        sorts=list(SORTS),
        today=local_today(),
        filter_args={k: v for k, v in filters.items() if v},
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
def create():
    form = LeadForm()
    _prepare_lead_form(form)
    if request.method == "GET":
        form.assigned_to_id.data = current_user.id
    if form.validate_on_submit():
        lead = Lead(business_id=current_business_id(), created_by_id=current_user.id)
        _apply_form(lead, form)
        db.session.add(lead)
        db.session.flush()
        _log(lead, "created", f"Lead created ({lead.status_label}).")
        db.session.commit()
        flash("Lead created.", "success")
        return redirect(url_for("leads.detail", lead_id=lead.id))
    return render_template("leads/form.html", form=form, lead=None)


@bp.route("/<int:lead_id>")
@login_required
def detail(lead_id):
    lead = get_lead_or_404(lead_id)
    activity_form = ActivityForm()
    status_form = StatusForm(status=lead.status)
    return render_template(
        "leads/detail.html",
        lead=lead,
        activity_form=activity_form,
        status_form=status_form,
        snooze_form=SnoozeForm(),
        draft_form=DraftRequestForm(channel="text" if lead.phone or not lead.email else "email"),
        today=local_today(),
    )


@bp.route("/<int:lead_id>/edit", methods=["GET", "POST"])
@login_required
def edit(lead_id):
    lead = get_lead_or_404(lead_id)
    form = LeadForm(obj=lead)
    _prepare_lead_form(form)
    if request.method == "GET":
        form.assigned_to_id.data = lead.assigned_to_id or 0
    if form.validate_on_submit():
        old_status = lead.status
        _apply_form(lead, form)
        if old_status != lead.status:
            _log(lead, "status_change", f"{STATUS_LABELS[old_status]} → {lead.status_label}")
        else:
            _log(lead, "updated", "Lead details updated.")
        db.session.commit()
        flash("Lead updated.", "success")
        return redirect(url_for("leads.detail", lead_id=lead.id))
    return render_template("leads/form.html", form=form, lead=lead)


@bp.route("/<int:lead_id>/delete", methods=["POST"])
@login_required
@require_role(ROLE_ADMIN)
def delete(lead_id):
    lead = get_lead_or_404(lead_id)
    audit.record(lead.business_id, "lead_delete", f"Lead #{lead.id} with {len(lead.activities)} timeline entr(ies).")
    db.session.delete(lead)
    db.session.commit()
    flash("Lead deleted.", "info")
    return redirect(url_for("leads.index"))


@bp.route("/<int:lead_id>/status", methods=["POST"])
@login_required
def change_status(lead_id):
    lead = get_lead_or_404(lead_id)
    form = StatusForm()
    if not form.validate_on_submit():
        abort(400)
    if form.status.data != lead.status:
        old = lead.status
        lead.status = form.status.data
        _log(lead, "status_change", f"{STATUS_LABELS[old]} → {lead.status_label}")
        db.session.commit()
        flash(f"Status changed to {lead.status_label}.", "success")
    return redirect(request.referrer if is_same_app_referrer() else url_for("leads.detail", lead_id=lead.id))


@bp.route("/<int:lead_id>/activities", methods=["POST"])
@login_required
def add_activity(lead_id):
    lead = get_lead_or_404(lead_id)
    form = ActivityForm()
    if not form.validate_on_submit():
        for field, errs in form.errors.items():
            flash(f"{getattr(form, field).label.text}: {errs[0]}", "error")
        return redirect(url_for("leads.detail", lead_id=lead.id))
    occurred = datetime.combine(form.occurred_on.data, time(12, 0), tzinfo=timezone.utc)
    _log(lead, form.type.data, form.body.data or "", occurred_at=occurred)
    if form.type.data in CONTACT_ACTIVITY_TYPES:
        if lead.last_contact_date is None or form.occurred_on.data > lead.last_contact_date:
            lead.last_contact_date = form.occurred_on.data
        if lead.status == "new":
            lead.status = "contacted"
        if form.type.data == "estimate" and lead.status in ("new", "contacted", "inspection_scheduled"):
            lead.status = "estimate_sent"
    if form.next_follow_up_date.data:
        lead.next_follow_up_date = form.next_follow_up_date.data
    db.session.commit()
    # No customer name here: flash messages live in the (readable) session cookie.
    flash("Activity logged.", "success")
    next_url = request.form.get("next")
    return redirect(with_script_root(next_url) if is_safe_redirect(next_url) else url_for("leads.detail", lead_id=lead.id))


@bp.route("/<int:lead_id>/activities/<int:activity_id>/delete", methods=["POST"])
@login_required
@require_role(ROLE_ADMIN)
def delete_activity(lead_id, activity_id):
    lead = get_lead_or_404(lead_id)
    activity = get_activity_or_404(lead, activity_id)
    audit.record(lead.business_id, "activity_delete", f"Timeline entry #{activity.id} ({activity.type}) on lead #{lead.id}.")
    db.session.delete(activity)
    db.session.commit()
    flash("Activity removed.", "info")
    return redirect(url_for("leads.detail", lead_id=lead.id))


@bp.route("/<int:lead_id>/snooze", methods=["POST"])
@login_required
def snooze(lead_id):
    lead = get_lead_or_404(lead_id)
    form = SnoozeForm()
    if not form.validate_on_submit():
        abort(400)
    lead.next_follow_up_date = local_today() + timedelta(days=form.days.data)
    _log(lead, "updated", f"Follow-up moved to {lead.next_follow_up_date.isoformat()}.")
    db.session.commit()
    flash(f"Follow-up set for {lead.next_follow_up_date.strftime('%b %d')}.", "success")
    return redirect(request.referrer if is_same_app_referrer() else url_for("main.queue"))


@bp.route("/export.csv")
@login_required
@require_role(ROLE_ADMIN)
def export():
    query, _ = filtered_leads_query(request.args)
    leads = db.session.execute(query).scalars().all()
    body = export_leads(leads)
    audit.record(current_business_id(), "csv_export", f"{len(leads)} lead(s).")
    db.session.commit()
    filename = f"leadrescue-leads-{local_today().isoformat()}.csv"
    return Response(
        body,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.route("/import-template.csv")
@login_required
def import_template():
    example = (
        ",".join(IMPORT_COLUMNS) + "\r\n"
        "Pat,Homeowner,(555) 555-0150,pat@example.com,12 Example Rd,Springfield,OH,45501,referral,estimate_sent,"
        "Full reroof,12500,2026-01-15,2026-01-20,Example row - replace with your leads\r\n"
    )
    return Response(example, mimetype="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="leadrescue-import-template.csv"'})


@bp.route("/import", methods=["GET", "POST"])
@login_required
@require_role(ROLE_ADMIN)
def import_csv():
    form = ImportForm()
    errors = []
    if form.validate_on_submit():
        upload = form.file.data
        raw = upload.read(current_app.config["MAX_CONTENT_LENGTH"] + 1)
        if len(raw) > current_app.config["MAX_CONTENT_LENGTH"]:
            abort(413)
        try:
            rows, errors = parse_import(raw, current_app.config["CSV_MAX_ROWS"])
        except CSVImportError as exc:
            flash(str(exc), "error")
            return render_template("leads/import.html", form=form, errors=[])
        if errors:
            flash(f"Nothing was imported: {len(errors)} row(s) need fixing.", "error")
        elif not rows:
            flash("No lead rows found in the file.", "error")
        else:
            business_id = current_business_id()
            for row in rows:
                lead = Lead(business_id=business_id, created_by_id=current_user.id, **row)
                for field in ("last_name", "phone", "email", "address", "city", "state", "zip_code", "job_type", "notes"):
                    if getattr(lead, field) is None:
                        setattr(lead, field, "")
                db.session.add(lead)
                db.session.flush()
                _log(lead, "imported", "Imported from CSV.")
            audit.record(business_id, "csv_import", f"{len(rows)} lead(s).")
            db.session.commit()
            flash(f"Imported {len(rows)} lead(s).", "success")
            return redirect(url_for("leads.index"))
    elif request.method == "POST":
        for field, errs in form.errors.items():
            flash(errs[0], "error")
    return render_template("leads/import.html", form=form, errors=errors[:50], error_count=len(errors))
