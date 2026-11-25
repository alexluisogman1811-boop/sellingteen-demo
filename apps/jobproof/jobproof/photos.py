import os

from flask import Blueprint, abort, current_app, flash, redirect, request, send_file, url_for

from . import images
from .db import get_db, now_iso
from .models import PHOTO_STAGES, get_photos
from .security import get_owned_job, get_owned_photo, login_required

bp = Blueprint("photos", __name__)


def _back(job_id):
    return redirect(url_for("jobs.detail", job_id=job_id) + "#photos")


@bp.route("/jobs/<int:job_id>/photos", methods=["POST"])
@login_required
def upload(job_id):
    get_owned_job(job_id)
    stage = request.form.get("stage", "before")
    if stage not in PHOTO_STAGES:
        flash("Choose Before, During or After for the photos.", "photo-error")
        return _back(job_id)
    files = [f for f in request.files.getlist("photos") if f and f.filename]
    if not files:
        flash("Choose at least one photo to upload.", "photo-error")
        return _back(job_id)

    db = get_db()
    existing = get_photos(job_id)
    limit = current_app.config["MAX_PHOTOS_PER_JOB"]
    position = max([p["position"] for p in existing], default=0)
    saved, problems = 0, []
    for f in files:
        if len(existing) + saved >= limit:
            problems.append(f"A job can have up to {limit} photos.")
            break
        try:
            data, ext, w, h = images.process_upload(f, current_app.config["MAX_PHOTO_BYTES"])
        except images.ImageValidationError as e:
            problems.append(str(e))
            continue
        key = images.new_key()
        try:
            images.save_bytes(current_app.config["UPLOAD_FOLDER"], "photos", key, ext, data)
        except OSError as e:
            current_app.logger.error("Photo storage failed: %s", type(e).__name__)
            problems.append("We couldn't save one of your photos. Please try again.")
            continue
        position += 1
        db.execute(
            "INSERT INTO photos (job_id, file_key, ext, stage, position, width, height, size_bytes, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (job_id, key, ext, stage, position, w, h, len(data), now_iso()),
        )
        saved += 1
    db.commit()
    if saved:
        flash(f"Uploaded {saved} {PHOTO_STAGES[stage].lower()} photo{'s' if saved != 1 else ''}.", "photo-success")
    for p in problems:
        flash(p, "photo-error")
    return _back(job_id)


@bp.route("/photos/<file_key>")
@login_required
def file(file_key):
    photo = get_owned_photo(file_key)
    path = images.stored_path(current_app.config["UPLOAD_FOLDER"], "photos", photo["file_key"], photo["ext"])
    if not os.path.exists(path):
        abort(404)  # record exists but the file is gone: show a broken image, not an error page
    return send_file(path, max_age=0)


@bp.route("/photos/<file_key>/delete", methods=["POST"])
@login_required
def delete(file_key):
    photo = get_owned_photo(file_key)
    db = get_db()
    db.execute("DELETE FROM photos WHERE id=?", (photo["id"],))
    db.commit()
    images.remove_file(current_app.config["UPLOAD_FOLDER"], "photos", photo["file_key"], photo["ext"])
    flash("Photo deleted.", "photo-success")
    return _back(photo["job_id"])


@bp.route("/photos/<file_key>/stage", methods=["POST"])
@login_required
def set_stage(file_key):
    photo = get_owned_photo(file_key)
    stage = request.form.get("stage", "")
    if stage not in PHOTO_STAGES:
        flash("Choose Before, During or After.", "photo-error")
    else:
        db = get_db()
        db.execute("UPDATE photos SET stage=? WHERE id=?", (stage, photo["id"]))
        db.commit()
    return _back(photo["job_id"])


@bp.route("/photos/<file_key>/move", methods=["POST"])
@login_required
def move(file_key):
    """Move a photo one place earlier/later among the photos of the same stage."""
    photo = get_owned_photo(file_key)
    direction = request.form.get("direction")
    siblings = [p for p in get_photos(photo["job_id"]) if p["stage"] == photo["stage"]]
    idx = next(i for i, p in enumerate(siblings) if p["id"] == photo["id"])
    swap_idx = idx - 1 if direction == "up" else idx + 1 if direction == "down" else None
    if swap_idx is not None and 0 <= swap_idx < len(siblings):
        order = [p["id"] for p in get_photos(photo["job_id"])]
        a, b = siblings[idx]["id"], siblings[swap_idx]["id"]
        ia, ib = order.index(a), order.index(b)
        order[ia], order[ib] = order[ib], order[ia]
        db = get_db()
        for pos, pid in enumerate(order, start=1):
            db.execute("UPDATE photos SET position=? WHERE id=?", (pos, pid))
        db.commit()
    return _back(photo["job_id"])
