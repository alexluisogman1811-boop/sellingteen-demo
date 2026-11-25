from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .ai import ALLOWED_PLACEHOLDERS, unknown_placeholders
from .db import get_db
from .security import MAX_ID, login_required

bp = Blueprint("message_templates", __name__, url_prefix="/templates")


def validate_template(form):
    data = {"name": form.get("name", "").strip(), "body": form.get("body", "").strip()}
    errors = {}
    if not data["name"]:
        errors["name"] = "Give the template a name, like \"Friendly thank-you\"."
    elif len(data["name"]) > 80:
        errors["name"] = "Keep the name under 80 characters."
    if not data["body"]:
        errors["body"] = "Write the message text."
    elif len(data["body"]) > 1000:
        errors["body"] = "Keep the message under 1000 characters."
    elif bad := unknown_placeholders(data["body"]):
        errors["body"] = ("We don't recognize " + ", ".join("{%s}" % p for p in bad)
                          + ". Use only the placeholders listed below.")
    elif "{review_link}" not in data["body"]:
        errors["body"] = "Add {review_link} so the customer knows where to leave the review."
    return data, errors


def get_template_or_404(template_id):
    if template_id > MAX_ID:
        abort(404, description="Template not found.")
    row = get_db().execute(
        "SELECT * FROM message_templates WHERE id = ? AND business_id = ?",
        (template_id, g.business["id"]),
    ).fetchone()
    if row is None:
        abort(404, description="Template not found.")
    return row


@bp.route("/")
@login_required
def list_templates():
    rows = get_db().execute(
        "SELECT * FROM message_templates WHERE business_id = ? ORDER BY name", (g.business["id"],)
    ).fetchall()
    return render_template("message_templates/list.html", templates=rows)


@bp.route("/new", methods=["GET", "POST"])
@login_required
def create_template():
    data, errors = {}, {}
    if request.method == "POST":
        data, errors = validate_template(request.form)
        if not errors:
            db = get_db()
            db.execute("INSERT INTO message_templates (business_id, name, body) VALUES (?, ?, ?)",
                       (g.business["id"], data["name"], data["body"]))
            db.commit()
            flash("Template saved.", "success")
            return redirect(url_for("message_templates.list_templates"))
    return render_template("message_templates/form.html", data=data, errors=errors, template=None,
                           placeholders=ALLOWED_PLACEHOLDERS), (400 if errors else 200)


@bp.route("/<int:template_id>/edit", methods=["GET", "POST"])
@login_required
def edit_template(template_id):
    template = get_template_or_404(template_id)
    data, errors = dict(template), {}
    if request.method == "POST":
        data, errors = validate_template(request.form)
        if not errors:
            db = get_db()
            db.execute("UPDATE message_templates SET name = ?, body = ? WHERE id = ? AND business_id = ?",
                       (data["name"], data["body"], template_id, g.business["id"]))
            db.commit()
            flash("Template updated.", "success")
            return redirect(url_for("message_templates.list_templates"))
    return render_template("message_templates/form.html", data=data, errors=errors, template=template,
                           placeholders=ALLOWED_PLACEHOLDERS), (400 if errors else 200)


@bp.route("/<int:template_id>/delete", methods=["POST"])
@login_required
def delete_template(template_id):
    get_template_or_404(template_id)
    db = get_db()
    db.execute("DELETE FROM message_templates WHERE id = ? AND business_id = ?",
               (template_id, g.business["id"]))
    db.commit()
    flash("Template deleted.", "info")
    return redirect(url_for("message_templates.list_templates"))
