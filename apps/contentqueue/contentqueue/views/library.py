"""Campaigns, categories, caption templates and the idea bank."""
import sqlite3

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from ..auth import current_account_id
from ..captions import fill_template, find_placeholders, label_for
from ..constants import MAX_NAME, MAX_NOTES, MAX_TEMPLATE, MAX_TITLE, PLATFORMS
from ..db import get_db, now_iso
from ..validation import clean_text, normalize_hashtags, validate_named_item
from .main import filter_choices
from .posts import FORM_FIELDS

bp = Blueprint("library", __name__)


def _get_or_404(table, item_id):
    """Row from a fixed table name, only if it belongs to the signed-in account (else 404)."""
    if item_id > 2**63 - 1:
        abort(404)
    row = get_db().execute(f"SELECT * FROM {table} WHERE id = ? AND account_id = ?",
                           (item_id, current_account_id())).fetchone()
    if row is None:
        abort(404)
    return row


# ---------------------------------------------------------------- campaigns

@bp.route("/campaigns", methods=["GET", "POST"])
def campaigns():
    conn = get_db()
    errors, form = {}, {}
    if request.method == "POST":
        data, errors = validate_named_item(request.form, with_color=True)
        form = request.form
        if not errors:
            try:
                conn.execute(
                    "INSERT INTO campaigns (account_id, name, description, color, start_date, end_date, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (current_account_id(), data["name"], data["description"], data["color"], data["start_date"], data["end_date"], now_iso()),
                )
                conn.commit()
                flash(f"Campaign “{data['name']}” created.", "success")
                return redirect(url_for("library.campaigns"))
            except sqlite3.IntegrityError:
                errors["name"] = "A campaign with that name already exists."
        flash("Please fix the highlighted fields.", "error")
    rows = conn.execute(
        """SELECT c.*, COUNT(p.id) AS post_count,
                  SUM(CASE WHEN p.status = 'published' THEN 1 ELSE 0 END) AS published_count
           FROM campaigns c LEFT JOIN posts p ON p.campaign_id = c.id AND p.account_id = c.account_id
           WHERE c.account_id = ?
           GROUP BY c.id ORDER BY c.name""",
        (current_account_id(),),
    ).fetchall()
    return render_template("campaigns.html", campaigns=rows, errors=errors, form=form), (400 if errors else 200)


@bp.route("/campaigns/<int:item_id>/edit", methods=["GET", "POST"])
def edit_campaign(item_id):
    conn = get_db()
    item = _get_or_404("campaigns", item_id)
    errors, form = {}, item
    if request.method == "POST":
        data, errors = validate_named_item(request.form, with_color=True)
        form = request.form
        if not errors:
            try:
                conn.execute(
                    "UPDATE campaigns SET name = ?, description = ?, color = ?, start_date = ?, end_date = ? "
                    "WHERE id = ? AND account_id = ?",
                    (data["name"], data["description"], data["color"], data["start_date"], data["end_date"],
                     item_id, current_account_id()),
                )
                conn.commit()
                flash("Campaign saved.", "success")
                return redirect(url_for("library.campaigns"))
            except sqlite3.IntegrityError:
                errors["name"] = "A campaign with that name already exists."
    return render_template("campaign_form.html", item=item, form=form, errors=errors), (400 if errors else 200)


@bp.post("/campaigns/<int:item_id>/delete")
def delete_campaign(item_id):
    conn = get_db()
    item = _get_or_404("campaigns", item_id)
    conn.execute("DELETE FROM campaigns WHERE id = ? AND account_id = ?", (item_id, current_account_id()))
    conn.commit()
    flash(f"Deleted campaign “{item['name']}”. Its posts were kept without a campaign.", "success")
    return redirect(url_for("library.campaigns"))


# ---------------------------------------------------------------- categories

@bp.route("/categories", methods=["GET", "POST"])
def categories():
    conn = get_db()
    errors, form = {}, {}
    if request.method == "POST":
        data, errors = validate_named_item(request.form)
        form = request.form
        if not errors:
            try:
                conn.execute(
                    "INSERT INTO categories (account_id, name, description, created_at) VALUES (?, ?, ?, ?)",
                    (current_account_id(), data["name"], data["description"], now_iso()),
                )
                conn.commit()
                flash(f"Category “{data['name']}” created.", "success")
                return redirect(url_for("library.categories"))
            except sqlite3.IntegrityError:
                errors["name"] = "A category with that name already exists."
        flash("Please fix the highlighted fields.", "error")
    rows = conn.execute(
        """SELECT k.*,
                  (SELECT COUNT(*) FROM posts p WHERE p.category_id = k.id AND p.account_id = k.account_id) AS post_count,
                  (SELECT COUNT(*) FROM ideas i WHERE i.category_id = k.id AND i.account_id = k.account_id) AS idea_count
           FROM categories k WHERE k.account_id = ? ORDER BY k.name""",
        (current_account_id(),),
    ).fetchall()
    return render_template("categories.html", categories=rows, errors=errors, form=form), (400 if errors else 200)


@bp.route("/categories/<int:item_id>/edit", methods=["GET", "POST"])
def edit_category(item_id):
    conn = get_db()
    item = _get_or_404("categories", item_id)
    errors, form = {}, item
    if request.method == "POST":
        data, errors = validate_named_item(request.form)
        form = request.form
        if not errors:
            try:
                conn.execute(
                    "UPDATE categories SET name = ?, description = ? WHERE id = ? AND account_id = ?",
                    (data["name"], data["description"], item_id, current_account_id()),
                )
                conn.commit()
                flash("Category saved.", "success")
                return redirect(url_for("library.categories"))
            except sqlite3.IntegrityError:
                errors["name"] = "A category with that name already exists."
    return render_template("category_form.html", item=item, form=form, errors=errors), (400 if errors else 200)


@bp.post("/categories/<int:item_id>/delete")
def delete_category(item_id):
    conn = get_db()
    item = _get_or_404("categories", item_id)
    conn.execute("DELETE FROM categories WHERE id = ? AND account_id = ?", (item_id, current_account_id()))
    conn.commit()
    flash(f"Deleted category “{item['name']}”. Posts and ideas were kept.", "success")
    return redirect(url_for("library.categories"))


# ---------------------------------------------------------------- templates

def _validate_template(form):
    errors, data = {}, {}
    data["name"], ok = clean_text(form.get("name"), MAX_NAME)
    if not data["name"]:
        errors["name"] = "Name is required."
    elif not ok:
        errors["name"] = f"Keep the name under {MAX_NAME} characters."
    data["body"], ok = clean_text(form.get("body"), MAX_TEMPLATE, multiline=True)
    if not data["body"]:
        errors["body"] = "Template text is required."
    elif not ok:
        errors["body"] = f"Keep templates under {MAX_TEMPLATE} characters."
    data["hashtags"], err = normalize_hashtags(form.get("hashtags"))
    if err:
        errors["hashtags"] = err
    platform = form.get("platform") or ""
    if platform and platform not in PLATFORMS:
        errors["platform"] = "Choose a platform."
    data["platform"] = platform or None
    return data, errors


@bp.route("/templates", methods=["GET", "POST"])
def templates():
    conn = get_db()
    errors, form = {}, {}
    if request.method == "POST":
        data, errors = _validate_template(request.form)
        form = request.form
        if not errors:
            ts = now_iso()
            conn.execute(
                "INSERT INTO templates (account_id, name, body, hashtags, platform, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (current_account_id(), data["name"], data["body"], data["hashtags"], data["platform"], ts, ts),
            )
            conn.commit()
            flash("Template saved.", "success")
            return redirect(url_for("library.templates"))
        flash("Please fix the highlighted fields.", "error")
    rows = conn.execute("SELECT * FROM templates WHERE account_id = ? ORDER BY name",
                        (current_account_id(),)).fetchall()
    return render_template(
        "templates_list.html", templates=rows, errors=errors, form=form, find_placeholders=find_placeholders
    ), (400 if errors else 200)


@bp.route("/templates/<int:item_id>/edit", methods=["GET", "POST"])
def edit_template(item_id):
    conn = get_db()
    item = _get_or_404("templates", item_id)
    errors, form = {}, item
    if request.method == "POST":
        data, errors = _validate_template(request.form)
        form = request.form
        if not errors:
            conn.execute(
                "UPDATE templates SET name = ?, body = ?, hashtags = ?, platform = ?, updated_at = ? "
                "WHERE id = ? AND account_id = ?",
                (data["name"], data["body"], data["hashtags"], data["platform"], now_iso(), item_id,
                 current_account_id()),
            )
            conn.commit()
            flash("Template saved.", "success")
            return redirect(url_for("library.templates"))
    return render_template("template_form.html", item=item, form=form, errors=errors), (400 if errors else 200)


@bp.post("/templates/<int:item_id>/delete")
def delete_template(item_id):
    conn = get_db()
    item = _get_or_404("templates", item_id)
    conn.execute("DELETE FROM templates WHERE id = ? AND account_id = ?", (item_id, current_account_id()))
    conn.commit()
    flash(f"Deleted template “{item['name']}”.", "success")
    return redirect(url_for("library.templates"))


@bp.route("/templates/<int:item_id>/use", methods=["GET", "POST"])
def use_template(item_id):
    """Fill a template's {placeholders} locally, then open a prefilled post form."""
    item = _get_or_404("templates", item_id)
    placeholders = find_placeholders(item["body"])
    if request.method == "POST":
        values = {name: request.form.get(f"ph_{name}", "") for name in placeholders}
        caption = fill_template(item["body"], values)
        post = {k: "" for k in FORM_FIELDS}
        post.update(
            caption=caption,
            hashtags=item["hashtags"],
            platform=item["platform"] or "instagram",
            status="draft",
            title=(values.get("product") or item["name"])[:MAX_TITLE],
        )
        flash("Caption filled from template. Review it and save the post.", "info")
        return render_template(
            "posts/form.html",
            post=post,
            errors={},
            status_choices=("draft", "review", "scheduled"),
            action=url_for("posts.new_post"),
            editing=None,
            extra={},
            **filter_choices(get_db(), current_account_id()),
        )
    return render_template(
        "template_use.html",
        item=item,
        fields=[(name, label_for(name)) for name in placeholders],
    )


# ---------------------------------------------------------------- ideas

def _validate_idea(form, conn):
    errors, data = {}, {}
    data["title"], ok = clean_text(form.get("title"), MAX_TITLE)
    if not data["title"]:
        errors["title"] = "Describe the idea in a few words."
    elif not ok:
        errors["title"] = f"Keep it under {MAX_TITLE} characters."
    data["notes"], ok = clean_text(form.get("notes"), MAX_NOTES, multiline=True)
    if not ok:
        errors["notes"] = f"Notes must be under {MAX_NOTES} characters."
    raw = (form.get("category_id") or "").strip()
    data["category_id"] = None
    if raw:
        if raw.isdigit() and len(raw) <= 18 and conn.execute(
                "SELECT 1 FROM categories WHERE id = ? AND account_id = ?",
                (int(raw), current_account_id())).fetchone():
            data["category_id"] = int(raw)
        else:
            errors["category_id"] = "Invalid category."
    return data, errors


@bp.route("/ideas", methods=["GET", "POST"])
def ideas():
    conn = get_db()
    errors, form = {}, {}
    if request.method == "POST":
        data, errors = _validate_idea(request.form, conn)
        form = request.form
        if not errors:
            conn.execute(
                "INSERT INTO ideas (account_id, title, notes, category_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (current_account_id(), data["title"], data["notes"], data["category_id"], now_iso()),
            )
            conn.commit()
            flash("Idea added to the bank.", "success")
            return redirect(url_for("library.ideas"))
        flash("Please fix the highlighted fields.", "error")
    show = request.args.get("show", "new")
    if show not in ("new", "used", "all"):
        show = "new"
    sql = ("SELECT i.*, k.name AS category_name FROM ideas i "
           "LEFT JOIN categories k ON k.id = i.category_id AND k.account_id = i.account_id "
           "WHERE i.account_id = ?")
    params = (current_account_id(),)
    if show != "all":
        sql += " AND i.status = ?"
        params += (show,)
    rows = conn.execute(sql + " ORDER BY i.created_at DESC, i.id DESC", params).fetchall()
    return render_template(
        "ideas.html", ideas=rows, errors=errors, form=form, show=show,
        categories=conn.execute("SELECT id, name FROM categories WHERE account_id = ? ORDER BY name",
                                (current_account_id(),)).fetchall(),
    ), (400 if errors else 200)


@bp.route("/ideas/<int:item_id>/edit", methods=["GET", "POST"])
def edit_idea(item_id):
    conn = get_db()
    item = _get_or_404("ideas", item_id)
    errors, form = {}, item
    if request.method == "POST":
        data, errors = _validate_idea(request.form, conn)
        form = request.form
        if not errors:
            conn.execute(
                "UPDATE ideas SET title = ?, notes = ?, category_id = ? WHERE id = ? AND account_id = ?",
                (data["title"], data["notes"], data["category_id"], item_id, current_account_id()),
            )
            conn.commit()
            flash("Idea saved.", "success")
            return redirect(url_for("library.ideas"))
    return render_template(
        "idea_form.html", item=item, form=form, errors=errors,
        categories=conn.execute("SELECT id, name FROM categories WHERE account_id = ? ORDER BY name",
                                (current_account_id(),)).fetchall(),
    ), (400 if errors else 200)


@bp.post("/ideas/<int:item_id>/toggle")
def toggle_idea(item_id):
    conn = get_db()
    item = _get_or_404("ideas", item_id)
    new_status = "new" if item["status"] == "used" else "used"
    conn.execute("UPDATE ideas SET status = ? WHERE id = ? AND account_id = ?",
                 (new_status, item_id, current_account_id()))
    conn.commit()
    flash("Idea marked as used." if new_status == "used" else "Idea moved back to new.", "success")
    return redirect(url_for("library.ideas"))


@bp.post("/ideas/<int:item_id>/delete")
def delete_idea(item_id):
    conn = get_db()
    _get_or_404("ideas", item_id)
    conn.execute("DELETE FROM ideas WHERE id = ? AND account_id = ?", (item_id, current_account_id()))
    conn.commit()
    flash("Idea deleted.", "success")
    return redirect(url_for("library.ideas"))
