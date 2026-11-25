import re
from datetime import date

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, url_for

from . import images
from .db import get_db, now_iso
from .models import (JOB_STATUSES, PHOTO_STAGES, REPORT_STATUS_SQL, SERVICE_TYPES, get_active_share,
                     get_photos, get_report, report_status)
from .security import get_owned_job, login_required

bp = Blueprint("jobs", __name__, url_prefix="/jobs")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
LIMITS = {
    "customer_name": 120, "customer_phone": 40, "customer_email": 254, "title": 160,
    "address": 300, "service_type": 60, "technician": 120, "notes": 10000, "materials": 2000,
}
FIELDS = list(LIMITS) + ["job_date", "status"]


def read_job_form():
    values = {k: request.form.get(k, "").strip() for k in FIELDS}
    values["status"] = values["status"] or "draft"
    errors = []
    if not values["customer_name"]:
        errors.append("Customer name is required.")
    if not values["title"]:
        errors.append("Job title is required (for example “Kitchen Sink Repair”).")
    if values["customer_email"] and not EMAIL_RE.match(values["customer_email"]):
        errors.append("Customer email doesn't look valid.")
    if values["job_date"]:
        try:
            date.fromisoformat(values["job_date"])
        except ValueError:
            errors.append("Job date must be a valid date.")
    if values["status"] not in JOB_STATUSES:
        errors.append("Choose a valid job status.")
    for key, limit in LIMITS.items():
        if len(values[key]) > limit:
            errors.append(f"{key.replace('_', ' ').capitalize()} must be {limit} characters or fewer.")
    return values, errors


@bp.route("")
@login_required
def index():
    q = request.args.get("q", "").strip()[:100]
    search_in = request.args.get("in", "all")
    status = request.args.get("status", "")
    sort = request.args.get("sort", "newest")

    sql = [f"SELECT j.*, ({REPORT_STATUS_SQL}) AS report_status FROM jobs j "
           "LEFT JOIN reports r ON r.job_id = j.id WHERE j.business_id = ?"]
    params = [g.business["id"]]
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        if search_in == "customer":
            sql.append("AND j.customer_name LIKE ? ESCAPE '\\'")
            params.append(like)
        elif search_in == "job":
            sql.append("AND j.title LIKE ? ESCAPE '\\'")
            params.append(like)
        else:
            sql.append("AND (j.customer_name LIKE ? ESCAPE '\\' OR j.title LIKE ? ESCAPE '\\')")
            params += [like, like]
    if status in JOB_STATUSES:
        sql.append("AND j.status = ?")
        params.append(status)
    sql.append("ORDER BY j.created_at ASC, j.id ASC" if sort == "oldest" else "ORDER BY j.created_at DESC, j.id DESC")
    rows = get_db().execute(" ".join(sql), params).fetchall()
    return render_template("jobs/list.html", jobs=rows, q=q, search_in=search_in, status=status, sort=sort)


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    if request.method == "POST":
        values, errors = read_job_form()
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("jobs/form.html", job=values, mode="new",
                                   service_types=SERVICE_TYPES), 400
        ts = now_iso()
        db = get_db()
        cur = db.execute(
            "INSERT INTO jobs (business_id, customer_name, customer_phone, customer_email, title, address, "
            "service_type, job_date, technician, notes, materials, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (g.business["id"], values["customer_name"], values["customer_phone"], values["customer_email"],
             values["title"], values["address"], values["service_type"], values["job_date"],
             values["technician"], values["notes"], values["materials"], values["status"], ts, ts),
        )
        db.commit()
        flash("Job created. Next, add photos and generate the report.", "success")
        return redirect(url_for("jobs.detail", job_id=cur.lastrowid))
    defaults = {k: "" for k in FIELDS}
    defaults["status"] = "draft"
    defaults["job_date"] = date.today().isoformat()
    defaults["technician"] = g.business["contact_name"] if g.business else ""
    return render_template("jobs/form.html", job=defaults, mode="new", service_types=SERVICE_TYPES)


@bp.route("/<int:job_id>")
@login_required
def detail(job_id):
    job = get_owned_job(job_id)
    photos = get_photos(job_id)
    report = get_report(job_id)
    share = get_active_share(report["id"]) if report else None
    stale = bool(report and job["updated_at"] > report["generated_at"])
    return render_template("jobs/detail.html", job=job, photos=photos, report=report, share=share,
                           report_status=report_status(report), stale=stale, stages=PHOTO_STAGES)


@bp.route("/<int:job_id>/edit", methods=["GET", "POST"])
@login_required
def edit(job_id):
    job = get_owned_job(job_id)
    if request.method == "POST":
        values, errors = read_job_form()
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("jobs/form.html", job=values | {"id": job_id}, mode="edit",
                                   service_types=SERVICE_TYPES), 400
        db = get_db()
        db.execute(
            "UPDATE jobs SET customer_name=?, customer_phone=?, customer_email=?, title=?, address=?, "
            "service_type=?, job_date=?, technician=?, notes=?, materials=?, status=?, updated_at=? "
            "WHERE id=? AND business_id=?",
            (values["customer_name"], values["customer_phone"], values["customer_email"], values["title"],
             values["address"], values["service_type"], values["job_date"], values["technician"],
             values["notes"], values["materials"], values["status"], now_iso(), job_id, g.business["id"]),
        )
        db.commit()
        msg = "Job updated."
        if get_report(job_id):
            msg += " Regenerate the report to include your changes."
        flash(msg, "success")
        return redirect(url_for("jobs.detail", job_id=job_id))
    return render_template("jobs/form.html", job=job, mode="edit", service_types=SERVICE_TYPES)


@bp.route("/<int:job_id>/status", methods=["POST"])
@login_required
def set_status(job_id):
    get_owned_job(job_id)
    status = request.form.get("status", "")
    if status not in JOB_STATUSES:
        flash("Choose a valid job status.", "error")
    else:
        db = get_db()
        db.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=? AND business_id=?",
                   (status, now_iso(), job_id, g.business["id"]))
        db.commit()
        flash(f"Job marked as {JOB_STATUSES[status]}.", "success")
    return redirect(url_for("jobs.detail", job_id=job_id))


@bp.route("/<int:job_id>/delete", methods=["POST"])
@login_required
def delete(job_id):
    job = get_owned_job(job_id)
    photos = get_photos(job_id)
    db = get_db()
    db.execute("DELETE FROM jobs WHERE id=? AND business_id=?", (job_id, g.business["id"]))
    db.commit()
    # Remove files only after the database delete succeeded, so a failure never leaves broken photos.
    root = current_app.config["UPLOAD_FOLDER"]
    for photo in photos:
        images.remove_file(root, "photos", photo["file_key"], photo["ext"])
    flash(f"Deleted job “{job['title']}”.", "success")
    return redirect(url_for("jobs.index"))
