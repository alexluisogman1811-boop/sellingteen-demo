import os

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, send_file, url_for

from . import images
from .db import get_db, now_iso
from .security import login_required

bp = Blueprint("profile", __name__, url_prefix="/profile")

FIELDS = {
    "name": ("Business name", 120),
    "contact_name": ("Owner / contact name", 120),
    "phone": ("Phone", 40),
    "email": ("Email", 254),
    "address": ("Business address", 300),
}


def logo_path(business):
    """Absolute path of a business logo, or None."""
    ref = business["logo_file"] if business else None
    if not ref or "." not in ref:
        return None
    key, ext = ref.split(".", 1)
    try:
        path = images.stored_path(current_app.config["UPLOAD_FOLDER"], "logos", key, ext)
    except ValueError:
        return None
    return path if os.path.exists(path) else None


@bp.route("", methods=["GET", "POST"])
@login_required
def edit():
    business = g.business
    if request.method == "POST":
        values = {k: request.form.get(k, "").strip() for k in FIELDS}
        errors = []
        if not values["name"]:
            errors.append("Business name is required - it appears at the top of every report.")
        for key, (label, limit) in FIELDS.items():
            if len(values[key]) > limit:
                errors.append(f"{label} must be {limit} characters or fewer.")
        if values["email"] and "@" not in values["email"]:
            errors.append("Enter a valid business email address.")

        logo_ref = business["logo_file"]
        upload = request.files.get("logo")
        new_logo = None
        if not errors and upload and upload.filename:
            try:
                data, ext, _w, _h = images.process_upload(
                    upload, current_app.config["MAX_LOGO_BYTES"], max_dimension=600)
            except images.ImageValidationError as e:
                errors.append(str(e))
            else:
                key = images.new_key()
                try:
                    images.save_bytes(current_app.config["UPLOAD_FOLDER"], "logos", key, ext, data)
                    new_logo = f"{key}.{ext}"
                except OSError as e:
                    current_app.logger.error("Logo storage failed: %s", type(e).__name__)
                    errors.append("We couldn't save your logo. Please try again.")
        if request.form.get("remove_logo") == "1" and not new_logo:
            new_logo = ""

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("profile.html", business=values | {"logo_file": logo_ref},
                                   welcome=bool(request.args.get("welcome"))), 400

        if new_logo is not None and logo_ref:
            old_key, old_ext = logo_ref.split(".", 1)
            images.remove_file(current_app.config["UPLOAD_FOLDER"], "logos", old_key, old_ext)
            logo_ref = new_logo or None
        elif new_logo:
            logo_ref = new_logo

        db = get_db()
        db.execute(
            "UPDATE businesses SET name=?, contact_name=?, phone=?, email=?, address=?, logo_file=?, updated_at=? "
            "WHERE id=?",
            (values["name"], values["contact_name"], values["phone"], values["email"], values["address"],
             logo_ref, now_iso(), business["id"]),
        )
        db.commit()
        flash("Business profile saved.", "success")
        if request.args.get("welcome"):
            return redirect(url_for("jobs.new"))
        return redirect(url_for("profile.edit"))
    return render_template("profile.html", business=business, welcome=bool(request.args.get("welcome")))


@bp.route("/logo")
@login_required
def logo():
    path = logo_path(g.business)
    if not path:
        abort(404)
    return send_file(path, max_age=0)
