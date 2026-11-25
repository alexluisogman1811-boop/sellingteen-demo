"""Photo upload, delete and owner-only serving."""
import logging

from flask import Blueprint, abort, current_app, flash, redirect, request, send_file, url_for
from flask_login import login_required

from .helpers import get_job_or_404, get_photo_or_404
from .models import JobPhoto, PhotoKind, db
from .storage import PhotoError, delete_photo_files, photo_path, save_photo

bp = Blueprint("photos", __name__)
log = logging.getLogger(__name__)


@bp.route("/jobs/<ref>/photos", methods=["POST"])
@login_required
def upload(ref):
    job = get_job_or_404(ref)
    kind = request.form.get("kind", PhotoKind.GENERAL)
    if kind not in PhotoKind.ALL:
        kind = PhotoKind.GENERAL
    files = [f for f in request.files.getlist("photos") if f and f.filename]
    if not files:
        flash("Choose at least one photo to upload.", "error")
        return redirect(url_for("jobs.job_detail", ref=job.ref) + "#photos")

    room = current_app.config["MAX_PHOTOS_PER_JOB"] - len(job.photos)
    if len(files) > room:
        flash(f"A job can have up to {current_app.config['MAX_PHOTOS_PER_JOB']} photos.", "error")
        return redirect(url_for("jobs.job_detail", ref=job.ref) + "#photos")

    saved, problems = 0, []
    for f in files:
        try:
            meta = save_photo(f, job.business.ref)
        except PhotoError as e:
            problems.append(str(e))
            continue
        db.session.add(JobPhoto(business_id=job.business_id, job_id=job.id, kind=kind, **meta))
        saved += 1
    db.session.commit()

    if saved:
        flash(f"Uploaded {saved} photo{'s' if saved != 1 else ''}.", "success")
    for p in problems:
        flash(p, "error")
    return redirect(url_for("jobs.job_detail", ref=job.ref) + "#photos")


@bp.route("/photos/<ref>/delete", methods=["POST"])
@login_required
def delete(ref):
    photo = get_photo_or_404(ref)
    job_ref, business_ref = photo.job.ref, photo.job.business.ref
    names = (photo.stored_name, photo.thumb_name)
    db.session.delete(photo)
    db.session.commit()
    delete_photo_files(business_ref, *names)
    flash("Photo deleted.", "info")
    return redirect(url_for("jobs.job_detail", ref=job_ref) + "#photos")


@bp.route("/photos/<ref>")
@login_required
def view(ref):
    photo = get_photo_or_404(ref)
    return send_photo(photo, thumb=request.args.get("thumb") == "1")


def send_photo(photo: JobPhoto, thumb: bool):
    name = photo.thumb_name if thumb else photo.stored_name
    try:
        path = photo_path(photo.job.business.ref, name)
        resp = send_file(path, mimetype=photo.content_type, max_age=0)
    except (PhotoError, FileNotFoundError):
        log.error("Photo file missing for photo id=%s", photo.id)
        abort(404)
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp
