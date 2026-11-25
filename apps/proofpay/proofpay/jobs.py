"""Owner dashboard and job management. Every query is scoped to the logged-in business."""
from datetime import date

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required
from sqlalchemy import func
from sqlalchemy.orm import selectinload

from . import ai, billing, payments
from .auth import EMAIL_RE
from .helpers import current_business, get_job_or_404, parse_money
from .models import Customer, Invoice, InvoiceStatus, Job, JobStatus, db, utcnow
from .storage import delete_photo_files

bp = Blueprint("jobs", __name__)

LIMITS = {"customer_name": 120, "customer_email": 255, "customer_phone": 40, "title": 160,
          "description": 4000, "address": 255, "work_notes": 4000}


@bp.route("/dashboard")
@login_required
def dashboard():
    business = current_business()
    jobs = db.session.scalars(
        db.select(Job).filter_by(business_id=business.id)
        .options(selectinload(Job.customer), selectinload(Job.invoice))
        .order_by(Job.created_at.desc()).limit(50)).all()

    def count(*conds):
        return db.session.scalar(
            db.select(func.count(Job.id)).outerjoin(Invoice)
            .where(Job.business_id == business.id, *conds)) or 0

    unpaid_cond = (Job.status == JobStatus.COMPLETED, Job.price_cents > 0,
                   (Invoice.status != InvoiceStatus.PAID) | (Invoice.id.is_(None)))
    stats = {
        "total": count(),
        "completed": count(Job.status == JobStatus.COMPLETED),
        "unpaid": count(*unpaid_cond),
        "paid": count(Invoice.status == InvoiceStatus.PAID),
        "unpaid_cents": db.session.scalar(
            db.select(func.coalesce(func.sum(Job.price_cents), 0)).outerjoin(Invoice)
            .where(Job.business_id == business.id, *unpaid_cond)) or 0,
    }
    return render_template("jobs/dashboard.html", jobs=jobs, stats=stats, business=business)


def _read_job_form(job: Job | None):
    f = {k: request.form.get(k, "").strip() for k in
         ("customer_ref", "customer_name", "customer_email", "customer_phone", "title",
          "description", "address", "price", "due_date", "status", "work_notes")}
    errors = {}
    for key, limit in LIMITS.items():
        if len(f[key]) > limit:
            errors[key] = f"Keep this under {limit} characters."

    business = current_business()
    existing_customer = None
    if job is None and f["customer_ref"]:
        existing_customer = db.session.scalar(
            db.select(Customer).filter_by(ref=f["customer_ref"], business_id=business.id))
        if existing_customer is None:
            errors["customer_ref"] = "Choose a customer from the list."
    else:
        if not f["customer_name"]:
            errors["customer_name"] = "Enter the customer's name."
        if f["customer_email"] and not EMAIL_RE.match(f["customer_email"]):
            errors["customer_email"] = "Enter a valid email address."

    if not f["title"]:
        errors["title"] = "Give the job a short title."

    price_cents = parse_money(f["price"])
    if not f["price"]:
        errors["price"] = "Enter the price (use 0 if there's no charge)."
    elif price_cents is None:
        errors["price"] = "Enter a price like 1250 or 1250.50 (max $1,000,000)."
    elif job is not None and job.is_paid and price_cents != job.price_cents:
        errors["price"] = "This job is marked paid. Mark it unpaid before changing the price."

    due = None
    if f["due_date"]:
        try:
            due = date.fromisoformat(f["due_date"])
        except ValueError:
            errors["due_date"] = "Enter a valid date."

    if f["status"] not in (JobStatus.DRAFT, JobStatus.IN_PROGRESS, JobStatus.COMPLETED, ""):
        errors["status"] = "Choose a status."

    return f, errors, existing_customer, price_cents, due


def _apply(job: Job, f: dict, price_cents: int, due) -> None:
    job.title = f["title"]
    job.description = f["description"] or None
    job.address = f["address"] or None
    job.price_cents = price_cents
    job.due_date = due
    job.work_notes = f["work_notes"] or None
    if f["status"] and f["status"] != job.status:
        _set_status(job, f["status"])
    payments.ensure_invoice(job)


def _reopens_over_free_limit(job: Job, status: str) -> bool:
    """Free plan: reopening a completed job must not go over the active-job limit."""
    if not job.is_completed or status in ("", JobStatus.COMPLETED):
        return False
    return not billing.can_add_active_job(job.business, exclude_job=job)


def _set_status(job: Job, status: str) -> None:
    job.status = status
    job.completed_at = utcnow() if status == JobStatus.COMPLETED else None


@bp.route("/jobs/new", methods=["GET", "POST"])
@login_required
def new_job():
    business = current_business()
    if not billing.can_add_active_job(business):
        flash(billing.free_limit_message(), "error")
        return redirect(url_for("billing.subscribe"))
    customers = db.session.scalars(
        db.select(Customer).filter_by(business_id=business.id).order_by(Customer.name)).all()
    form, errors = {"status": JobStatus.IN_PROGRESS,
                    "customer_ref": request.args.get("customer", "")}, {}
    if request.method == "POST":
        form, errors, customer, price_cents, due = _read_job_form(None)
        if not errors:
            if customer is None:
                email = form["customer_email"].lower() or None
                if email:  # reuse the same customer record for a repeat client
                    customer = db.session.scalar(
                        db.select(Customer).filter_by(business_id=business.id, email=email))
                if customer is None:
                    customer = Customer(business_id=business.id, name=form["customer_name"],
                                        email=email, phone=form["customer_phone"] or None)
                    db.session.add(customer)
            job = Job(business_id=business.id, customer=customer, status=JobStatus.DRAFT)
            db.session.add(job)
            _apply(job, form, price_cents, due)
            db.session.commit()
            flash("Job created. Add photos and notes, then share it with your customer.", "success")
            return redirect(url_for("jobs.job_detail", ref=job.ref))
    return render_template("jobs/form.html", job=None, form=form, errors=errors,
                           customers=customers), 400 if errors else 200


@bp.route("/jobs/<ref>")
@login_required
def job_detail(ref):
    job = get_job_or_404(ref)
    return render_template("jobs/detail.html", job=job, draft_summary=None)


@bp.route("/jobs/<ref>/edit", methods=["GET", "POST"])
@login_required
def edit_job(ref):
    job = get_job_or_404(ref)
    errors = {}
    if request.method == "POST":
        form, errors, _, price_cents, due = _read_job_form(job)
        if _reopens_over_free_limit(job, form["status"]):
            errors["status"] = billing.free_limit_message()
        if not errors:
            job.customer.name = form["customer_name"]
            job.customer.email = form["customer_email"].lower() or None
            job.customer.phone = form["customer_phone"] or None
            _apply(job, form, price_cents, due)
            db.session.commit()
            flash("Job updated.", "success")
            return redirect(url_for("jobs.job_detail", ref=job.ref))
    else:
        form = {
            "customer_name": job.customer.name, "customer_email": job.customer.email or "",
            "customer_phone": job.customer.phone or "", "title": job.title,
            "description": job.description or "", "address": job.address or "",
            "price": f"{job.price:.2f}", "due_date": job.due_date.isoformat() if job.due_date else "",
            "status": job.status, "work_notes": job.work_notes or "",
        }
    return render_template("jobs/form.html", job=job, form=form, errors=errors,
                           customers=[]), 400 if errors else 200


@bp.route("/jobs/<ref>/delete", methods=["POST"])
@login_required
def delete_job(ref):
    job = get_job_or_404(ref)
    business_ref = job.business.ref
    files = [n for p in job.photos for n in (p.stored_name, p.thumb_name)]
    db.session.delete(job)
    db.session.commit()
    delete_photo_files(business_ref, *files)
    flash("Job deleted.", "info")
    return redirect(url_for("jobs.dashboard"))


@bp.route("/jobs/<ref>/status", methods=["POST"])
@login_required
def set_status(ref):
    job = get_job_or_404(ref)
    status = request.form.get("status", "")
    if status not in JobStatus.ALL:
        flash("Unknown status.", "error")
    elif _reopens_over_free_limit(job, status):
        flash(billing.free_limit_message(), "error")
    else:
        _set_status(job, status)
        payments.ensure_invoice(job)
        db.session.commit()
        flash("Job marked as completed." if status == JobStatus.COMPLETED
              else "Job status updated.", "success")
    return redirect(url_for("jobs.job_detail", ref=job.ref))


@bp.route("/jobs/<ref>/paid", methods=["POST"])
@login_required
def mark_paid(ref):
    job = get_job_or_404(ref)
    invoice = payments.ensure_invoice(job)
    if job.price_cents <= 0:
        flash("Set a price before marking this job paid.", "error")
    elif invoice.is_paid:
        flash("This invoice is already marked paid.", "info")
    else:
        payments.record_payment(invoice, via="manual")
        flash("Marked as paid. You confirmed receiving this payment.", "success")
    return redirect(url_for("jobs.job_detail", ref=job.ref))


@bp.route("/jobs/<ref>/unpaid", methods=["POST"])
@login_required
def mark_unpaid(ref):
    job = get_job_or_404(ref)
    invoice = payments.ensure_invoice(job)
    payments.mark_unpaid(invoice)
    flash("Marked as unpaid.", "info")
    return redirect(url_for("jobs.job_detail", ref=job.ref))


@bp.route("/jobs/<ref>/summary/generate", methods=["POST"])
@login_required
def generate_summary(ref):
    """Show an AI draft for review. Nothing is saved until the owner clicks Save."""
    job = get_job_or_404(ref)
    if not billing.has_pro(job.business):
        flash("AI job summaries are part of Pro. You can still write the summary yourself.", "error")
        return redirect(url_for("jobs.job_detail", ref=job.ref) + "#summary")
    notes = request.form.get("rough_notes", "").strip() or (job.work_notes or "")
    try:
        result = ai.generate_summary(notes, job.title)
    except ai.AIError as e:
        flash(str(e), "error")
        return redirect(url_for("jobs.job_detail", ref=job.ref) + "#summary")
    return render_template("jobs/detail.html", job=job, draft_summary=result.text,
                           omitted_notes=result.omitted, rough_notes=notes)


@bp.route("/jobs/<ref>/summary", methods=["POST"])
@login_required
def save_summary(ref):
    job = get_job_or_404(ref)
    text = request.form.get("summary", "").strip()
    if len(text) > 2000:
        flash("Keep the summary under 2000 characters.", "error")
    else:
        job.summary = text or None
        db.session.commit()
        flash("Summary saved. It now appears on the customer page." if text
              else "Summary removed.", "success")
    return redirect(url_for("jobs.job_detail", ref=job.ref) + "#summary")
