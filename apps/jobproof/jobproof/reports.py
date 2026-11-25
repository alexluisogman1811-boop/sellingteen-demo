import json
import logging
import re

from flask import (Blueprint, Response, current_app, flash, g, redirect, render_template, request, url_for)

from . import images
from .ai import AIError, generate_report
from .db import get_db, now_iso
from .models import get_active_share, get_photos, get_report, photos_by_stage, report_status
from .pdf import PDFError, build_pdf
from .profile import logo_path
from .security import get_owned_job, login_required

bp = Blueprint("reports", __name__)
log = logging.getLogger("jobproof.reports")


def photo_disk_path(photo):
    return images.stored_path(current_app.config["UPLOAD_FOLDER"], "photos", photo["file_key"], photo["ext"])


def pdf_filename(job):
    slug = re.sub(r"[^A-Za-z0-9]+", "-", f"{job['title']}-{job['customer_name']}").strip("-")[:80]
    return f"report-{slug or 'job'}.pdf"


def pdf_response(business, job, report, photos, include_customer_contact=True):
    try:
        data = build_pdf(business, job, report, photos, photo_disk_path, logo_path(business),
                         include_customer_contact=include_customer_contact)
    except PDFError as e:
        log.error("PDF generation failed: %s", e)
        return None
    return Response(data, mimetype="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{pdf_filename(job)}"',
        "Cache-Control": "no-store",
    })


@bp.route("/jobs/<int:job_id>/report/generate", methods=["POST"])
@login_required
def generate(job_id):
    job = get_owned_job(job_id)
    photos = get_photos(job_id)
    try:
        content, provider = generate_report(job, g.business, photos, current_app.config)
    except AIError as e:
        flash(e.user_message, "error")
        return redirect(url_for("jobs.detail", job_id=job_id))
    warnings = content.pop("owner_warnings", [])

    db = get_db()
    existing = get_report(job_id)
    if existing:
        db.execute("UPDATE reports SET content_json=?, provider=?, generated_at=?, "
                   "generation_count=generation_count+1 WHERE id=?",
                   (json.dumps(content), provider, now_iso(), existing["id"]))
    else:
        db.execute("INSERT INTO reports (job_id, content_json, provider, generated_at) VALUES (?,?,?,?)",
                   (job_id, json.dumps(content), provider, now_iso()))
    db.commit()
    flash("Report regenerated with your latest changes." if existing else "Report generated. Review it below.",
          "success")
    for warning in warnings:
        flash(warning, "warning")
    return redirect(url_for("reports.preview", job_id=job_id))


@bp.route("/jobs/<int:job_id>/report")
@login_required
def preview(job_id):
    job = get_owned_job(job_id)
    report = get_report(job_id)
    if report is None:
        flash("Generate the report first.", "error")
        return redirect(url_for("jobs.detail", job_id=job_id))
    photos = get_photos(job_id)
    share = get_active_share(report["id"])
    share_url = url_for("share.view", token=share["token"], _external=True) if share else None
    return render_template(
        "reports/preview.html", job=job, business=g.business, report=report, content=report["content"],
        grouped=photos_by_stage(photos), photo_url=lambda p: url_for("photos.file", file_key=p["file_key"]),
        logo_url=url_for("profile.logo") if logo_path(g.business) else None,
        share=share, share_url=share_url, report_status=report_status(report),
        stale=job["updated_at"] > report["generated_at"],
        tips=report_tips(job, g.business, photos, report["content"]),
    )


def report_tips(job, business, photos, content):
    """Owner-only suggestions that make a report more complete. The report never fills these gaps itself;
    the owner adds the real details and regenerates."""
    stages = {p["stage"] for p in photos}
    tips = []
    if not photos:
        tips.append("Add a Before and an After photo - customers trust reports with photos.")
    elif "after" not in stages:
        tips.append("Add an After photo to show the finished work.")
    elif "before" not in stages:
        tips.append("Add a Before photo so the customer can see the difference.")
    if not content.get("work_completed"):
        tips.append("Describe the work you did in the notes (for example “Replaced the shut-off valve”).")
    if not content.get("testing"):
        tips.append("Note how you checked the work (for example “Tested for leaks”) to add a Testing section.")
    if job["status"] != "completed":
        tips.append("Mark the job Completed when the work is finished.")
    missing = [label for label, value in (("job date", job["job_date"]), ("service address", job["address"]),
                                          ("technician", job["technician"])) if not value]
    if missing:
        tips.append("Add the " + ", ".join(missing) + " in Edit job.")
    if not (business["phone"] or business["email"]):
        tips.append("Add your phone or email in Business profile so customers know how to reach you.")
    return tips


@bp.route("/jobs/<int:job_id>/report.pdf")
@login_required
def pdf(job_id):
    job = get_owned_job(job_id)
    report = get_report(job_id)
    if report is None:
        flash("Generate the report before downloading a PDF.", "error")
        return redirect(url_for("jobs.detail", job_id=job_id))
    response = pdf_response(g.business, job, report, get_photos(job_id))
    if response is None:
        flash("We couldn't create the PDF right now. Your report is saved - please try again.", "error")
        return redirect(url_for("reports.preview", job_id=job_id))
    return response


@bp.route("/jobs/<int:job_id>/share", methods=["POST"])
@login_required
def create_share(job_id):
    import secrets
    get_owned_job(job_id)
    report = get_report(job_id)
    if report is None:
        flash("Generate the report before creating a share link.", "error")
        return redirect(url_for("jobs.detail", job_id=job_id))
    if get_active_share(report["id"]) is None:
        db = get_db()
        db.execute("INSERT INTO share_links (report_id, token, created_at) VALUES (?,?,?)",
                   (report["id"], secrets.token_urlsafe(32), now_iso()))
        db.commit()
        flash("Share link created. Copy it and send it to your customer.", "success")
    return redirect(url_for("reports.preview", job_id=job_id) + "#share")


@bp.route("/jobs/<int:job_id>/share/revoke", methods=["POST"])
@login_required
def revoke_share(job_id):
    get_owned_job(job_id)
    report = get_report(job_id)
    if report:
        db = get_db()
        db.execute("UPDATE share_links SET revoked_at=? WHERE report_id=? AND revoked_at IS NULL",
                   (now_iso(), report["id"]))
        db.commit()
        flash("Share link revoked. The old link no longer works.", "success")
    return redirect(url_for("reports.preview", job_id=job_id) + "#share")
