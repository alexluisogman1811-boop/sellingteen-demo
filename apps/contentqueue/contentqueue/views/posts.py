"""Post CRUD, status workflow, duplicate and move."""
import re
import secrets

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for

from ..auth import current_account_id
from ..constants import ACTIVE_STATUSES, CREATE_STATUSES, MAX_TITLE, PLATFORMS, STATUS_LABELS, TRANSITIONS
from ..db import get_db, now_iso
from ..queries import POST_SELECT, fetch_posts, filter_query_args, parse_filters
from ..uploads import UploadError, copy_upload, delete_upload, save_upload
from ..validation import (
    check_transition,
    parse_date,
    parse_time,
    published_record_errors,
    safe_next,
    today_and_now,
    validate_post_form,
)
from .main import filter_choices

bp = Blueprint("posts", __name__, url_prefix="/posts")

FORM_FIELDS = (
    "title", "caption", "hashtags", "platform", "status", "campaign_id", "category_id",
    "planned_date", "planned_time", "notes", "published_url",
)

# --- duplicate-submission guard -------------------------------------------
# Create/duplicate forms carry a one-time token. If the same token arrives
# twice (double click, browser back + resubmit, refresh of a POST), we send
# the user to the record created the first time instead of inserting again.
NONCE_SESSION_KEY = "_form_nonces"
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
MAX_REMEMBERED_NONCES = 50
MAX_ID = 2**63 - 1  # largest id SQLite can store; bigger URL ids can't exist


def new_form_nonce():
    return secrets.token_urlsafe(18)


def _valid_nonce(value):
    return value if value and NONCE_RE.fullmatch(value) else None


def nonce_target(nonce):
    """Post id previously created with this nonce, if it still exists."""
    nonce = _valid_nonce(nonce)
    if not nonce:
        return None
    post_id = session.get(NONCE_SESSION_KEY, {}).get(nonce)
    if post_id and get_db().execute("SELECT 1 FROM posts WHERE id = ? AND account_id = ?",
                                    (post_id, current_account_id())).fetchone():
        return post_id
    return None


def remember_nonce(nonce, post_id):
    nonce = _valid_nonce(nonce)
    if not nonce:
        return
    seen = dict(session.get(NONCE_SESSION_KEY, {}))
    seen[nonce] = post_id
    while len(seen) > MAX_REMEMBERED_NONCES:
        seen.pop(next(iter(seen)))
    session[NONCE_SESSION_KEY] = seen


# ---------------------------------------------------------------------------

def get_post_or_404(post_id):
    """The post, only if it belongs to the signed-in account (otherwise 404, never 403,
    so other businesses' post ids aren't even confirmed to exist)."""
    if post_id > MAX_ID:
        abort(404)
    row = get_db().execute(POST_SELECT + " WHERE p.id = ? AND p.account_id = ?",
                           (post_id, current_account_id())).fetchone()
    if row is None:
        abort(404)
    return row


def form_values(source):
    """Normalise a row/dict/form into string values for re-rendering the form."""
    values = {}
    for key in FORM_FIELDS:
        value = source.get(key) if hasattr(source, "get") else source[key]
        values[key] = "" if value is None else str(value)
    return values


def edit_status_choices(current):
    """Statuses selectable in the edit form. Publishing needs the explicit confirm action."""
    return [current] + [s for s in TRANSITIONS[current] if s != "published"]


def _image_was_chosen():
    f = request.files.get("image")
    return bool(f and f.filename)


def render_form(post, errors, status_choices, action, editing=None, extra=None, code=200):
    extra = dict(extra or {})
    extra.setdefault("form_nonce", _valid_nonce(request.form.get("form_nonce")) or new_form_nonce())
    if errors and request.method == "POST" and _image_was_chosen() and "image" not in errors:
        extra["image_dropped"] = True
    return render_template(
        "posts/form.html",
        post=post,
        errors=errors,
        status_choices=status_choices,
        action=action,
        editing=editing,
        extra=extra,
        **filter_choices(get_db(), current_account_id()),
    ), code


def _flash_warnings(data):
    for msg in data.get("warnings", []):
        flash(msg, "warning")


@bp.route("")
def list_posts():
    conn = get_db()
    filters, errors = parse_filters(request.args)
    for msg in errors:
        flash(msg, "warning")
    posts = fetch_posts(conn, current_account_id(), filters)
    return render_template(
        "posts/list.html",
        posts=posts,
        filters=filters,
        filter_args=filter_query_args(filters),
        **filter_choices(conn, current_account_id()),
    )


@bp.route("/new", methods=["GET", "POST"])
def new_post():
    conn = get_db()
    if request.method == "POST":
        existing = nonce_target(request.form.get("form_nonce"))
        if existing:
            flash("This post was already saved — no duplicate was created.", "info")
            return redirect(url_for("posts.detail", post_id=existing))

        data, errors = validate_post_form(request.form, conn, CREATE_STATUSES, has_image=_image_was_chosen(),
                                          account_id=current_account_id())
        image = None
        if not errors:
            try:
                image = save_upload(
                    request.files.get("image"),
                    current_app.config["UPLOAD_FOLDER"],
                    current_app.config["MAX_IMAGE_BYTES"],
                )
            except UploadError as exc:
                errors["image"] = str(exc)
        if errors:
            flash("Please fix the highlighted fields.", "error")
            return render_form(
                form_values(request.form), errors, CREATE_STATUSES, url_for("posts.new_post"),
                extra={"idea_id": request.form.get("idea_id", "")}, code=400,
            )
        ts = now_iso()
        try:
            cur = conn.execute(
                """INSERT INTO posts (account_id, title, caption, hashtags, platform, status, campaign_id,
                                      category_id, planned_date, planned_time, notes, published_url,
                                      image_filename, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (current_account_id(), data["title"], data["caption"], data["hashtags"], data["platform"], data["status"],
                 data["campaign_id"], data["category_id"], data["planned_date"], data["planned_time"],
                 data["notes"], data["published_url"], image, ts, ts),
            )
            idea_id = request.form.get("idea_id", "")
            if idea_id.isdigit():
                conn.execute("UPDATE ideas SET status = 'used' WHERE id = ? AND account_id = ?",
                             (int(idea_id), current_account_id()))
            conn.commit()
        except Exception:
            conn.rollback()
            delete_upload(current_app.config["UPLOAD_FOLDER"], image)  # no orphaned files
            raise
        remember_nonce(request.form.get("form_nonce"), cur.lastrowid)
        session["_clear_draft"] = "new"  # server confirmed the save; drop the browser's local copy
        flash("Post created.", "success")
        _flash_warnings(data)
        return redirect(url_for("posts.detail", post_id=cur.lastrowid))

    # GET: optional prefill from ?date=, ?idea_id=, ?campaign_id=
    post = {k: "" for k in FORM_FIELDS}
    post["platform"] = "instagram"
    post["status"] = "draft"
    extra = {}
    try:
        d = parse_date(request.args.get("date"))
        if d:
            post["planned_date"] = d.isoformat()
    except ValueError:
        pass
    campaign_id = request.args.get("campaign_id", "")
    if campaign_id.isdigit():
        post["campaign_id"] = campaign_id
    idea_id = request.args.get("idea_id", "")
    if idea_id.isdigit() and len(idea_id) <= 18:
        idea = conn.execute("SELECT * FROM ideas WHERE id = ? AND account_id = ?",
                            (int(idea_id), current_account_id())).fetchone()
        if idea:
            post["title"] = idea["title"][:MAX_TITLE]
            post["notes"] = idea["notes"]
            post["category_id"] = "" if idea["category_id"] is None else str(idea["category_id"])
            extra["idea_id"] = str(idea["id"])
    return render_form(post, {}, CREATE_STATUSES, url_for("posts.new_post"), extra=extra)


@bp.route("/<int:post_id>")
def detail(post_id):
    post = get_post_or_404(post_id)
    return render_template(
        "posts/detail.html",
        post=post,
        transitions=TRANSITIONS[post["status"]],
        limit=PLATFORMS.get(post["platform"], {}).get("limit"),
        form_nonce=new_form_nonce(),
    )


@bp.route("/<int:post_id>/edit", methods=["GET", "POST"])
def edit(post_id):
    conn = get_db()
    post = get_post_or_404(post_id)
    choices = edit_status_choices(post["status"])
    action = url_for("posts.edit", post_id=post_id)
    if request.method == "POST":
        keeps_image = bool(post["image_filename"]) and request.form.get("remove_image") != "1"
        data, errors = validate_post_form(request.form, conn, choices,
                                          has_image=keeps_image or _image_was_chosen(),
                                          account_id=current_account_id())
        if not errors:
            errors.update(published_record_errors(post, data))
        sent_version = request.form.get("version", "")
        if sent_version.isdigit() and int(sent_version) != post["version"]:
            return _conflict(post, choices, action)
        new_image = None
        if not errors:
            try:
                new_image = save_upload(
                    request.files.get("image"),
                    current_app.config["UPLOAD_FOLDER"],
                    current_app.config["MAX_IMAGE_BYTES"],
                )
            except UploadError as exc:
                errors["image"] = str(exc)
        if errors:
            flash("Please fix the highlighted fields.", "error")
            return render_form(form_values(request.form), errors, choices, action, editing=post, code=400)

        image = post["image_filename"]
        old_image_to_delete = None
        if new_image:
            old_image_to_delete, image = image, new_image
        elif request.form.get("remove_image") == "1":
            old_image_to_delete, image = image, None

        published_at = post["published_at"]
        if data["status"] in ACTIVE_STATUSES:
            published_at = None

        try:
            cur = conn.execute(
                """UPDATE posts SET title = ?, caption = ?, hashtags = ?, platform = ?, status = ?,
                                    campaign_id = ?, category_id = ?, planned_date = ?, planned_time = ?,
                                    notes = ?, published_url = ?, image_filename = ?, published_at = ?,
                                    updated_at = ?, version = version + 1
                   WHERE id = ? AND account_id = ? AND version = ?""",
                (data["title"], data["caption"], data["hashtags"], data["platform"], data["status"],
                 data["campaign_id"], data["category_id"], data["planned_date"], data["planned_time"],
                 data["notes"], data["published_url"], image, published_at, now_iso(), post_id,
                 current_account_id(), post["version"]),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            delete_upload(current_app.config["UPLOAD_FOLDER"], new_image)
            raise
        if cur.rowcount == 0:  # changed between our read and write
            delete_upload(current_app.config["UPLOAD_FOLDER"], new_image)
            return _conflict(get_post_or_404(post_id), choices, action)
        if old_image_to_delete:
            delete_upload(current_app.config["UPLOAD_FOLDER"], old_image_to_delete)
        session["_clear_draft"] = f"post-{post_id}"
        flash("Post saved.", "success")
        if post["status"] == "published" and data["status"] in ACTIVE_STATUSES:
            flash("The published record was cleared because the post is no longer marked published.", "info")
        _flash_warnings(data)
        return redirect(url_for("posts.detail", post_id=post_id))
    return render_form(form_values(post), {}, choices, action, editing=post,
                       extra={"version": post["version"]})


def _conflict(current, choices, action):
    """Someone saved this post (another tab/device) after this form was opened.

    Nothing is written. The user's typed text is shown again, with the hidden
    version updated so a deliberate second save overwrites the newer copy."""
    flash(
        f"This post was changed elsewhere (last saved {current['updated_at']}) after you opened it. "
        "Your edits are shown below but have NOT been saved. Compare with the current version, "
        "then save again to overwrite it.", "error",
    )
    return render_form(
        form_values(request.form), {"_conflict": True}, choices, action, editing=current,
        extra={"version": current["version"], "conflict": True}, code=409,
    )


@bp.post("/<int:post_id>/delete")
def delete(post_id):
    conn = get_db()
    post = get_post_or_404(post_id)
    conn.execute("DELETE FROM posts WHERE id = ? AND account_id = ?", (post_id, current_account_id()))
    conn.commit()
    delete_upload(current_app.config["UPLOAD_FOLDER"], post["image_filename"])
    flash(f"Deleted “{post['title']}”.", "success")
    return redirect(safe_next(request.form.get("next"), url_for("posts.list_posts")))


@bp.post("/<int:post_id>/status")
def change_status(post_id):
    conn = get_db()
    post = get_post_or_404(post_id)
    new_status = request.form.get("status", "")
    back = safe_next(request.form.get("next"), url_for("posts.detail", post_id=post_id))
    error = check_transition(
        post["status"], new_status, post["planned_date"],
        request.form.get("confirm_published") == "yes",
        was_published=bool(post["published_at"]),
        has_content=bool(post["caption"] or post["image_filename"]),
    )
    if error:
        flash(error, "error")
        return redirect(back)

    published_at = post["published_at"]
    restored = post["status"] == "archived" and new_status == "published"
    if new_status == "published" and not restored:
        published_at = now_iso()
    elif new_status in ACTIVE_STATUSES:
        published_at = None
    conn.execute(
        "UPDATE posts SET status = ?, published_at = ?, updated_at = ?, version = version + 1 "
        "WHERE id = ? AND account_id = ?",
        (new_status, published_at, now_iso(), post_id, current_account_id()),
    )
    conn.commit()
    if restored:
        flash("Restored to Published with its original publish record.", "success")
    elif new_status == "published":
        flash("Recorded as published by you. ContentQueue did not post anything to the platform.", "success")
    else:
        flash(f"Status changed to {STATUS_LABELS[new_status]}.", "success")
        if post["status"] == "published" and new_status in ACTIVE_STATUSES:
            flash("The published record was cleared.", "info")
    return redirect(back)


@bp.post("/<int:post_id>/duplicate")
def duplicate(post_id):
    conn = get_db()
    post = get_post_or_404(post_id)
    back = url_for("posts.detail", post_id=post_id)
    existing = nonce_target(request.form.get("form_nonce"))
    if existing:
        flash("That copy was already created — no second duplicate was made.", "info")
        return redirect(url_for("posts.edit", post_id=existing))
    try:
        new_date = parse_date(request.form.get("new_date"))
        new_time = parse_time(request.form.get("new_time"))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(back)
    planned_date = new_date.isoformat() if new_date else post["planned_date"]
    planned_time = new_time or post["planned_time"]
    if planned_time and not planned_date:
        planned_time = None

    image = copy_upload(current_app.config["UPLOAD_FOLDER"], post["image_filename"])
    title = post["title"]
    suffix = " (copy)"
    title = title[: MAX_TITLE - len(suffix)] + suffix
    ts = now_iso()
    try:
        cur = conn.execute(
            """INSERT INTO posts (account_id, title, caption, hashtags, platform, status, campaign_id,
                                  category_id, planned_date, planned_time, notes, image_filename,
                                  created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (current_account_id(), title, post["caption"], post["hashtags"], post["platform"], post["campaign_id"],
             post["category_id"], planned_date, planned_time, post["notes"], image, ts, ts),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        delete_upload(current_app.config["UPLOAD_FOLDER"], image)
        raise
    remember_nonce(request.form.get("form_nonce"), cur.lastrowid)
    flash("Duplicated as a new draft. Review it before scheduling.", "success")
    return redirect(url_for("posts.edit", post_id=cur.lastrowid))


@bp.post("/<int:post_id>/move")
def move(post_id):
    conn = get_db()
    post = get_post_or_404(post_id)
    back = safe_next(request.form.get("next"), url_for("posts.detail", post_id=post_id))
    if post["status"] in ("published", "archived"):
        flash("Published and archived posts keep their date for your records. Duplicate it to reuse the content.", "error")
        return redirect(back)
    try:
        new_date = parse_date(request.form.get("new_date"))
        new_time = parse_time(request.form.get("new_time"))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(back)
    if not new_date:
        flash("Choose a new date.", "error")
        return redirect(back)
    planned_time = new_time or post["planned_time"]
    conn.execute(
        "UPDATE posts SET planned_date = ?, planned_time = ?, updated_at = ?, version = version + 1 "
        "WHERE id = ? AND account_id = ?",
        (new_date.isoformat(), planned_time, now_iso(), post_id, current_account_id()),
    )
    conn.commit()
    flash(f"Moved to {new_date:%a %d %b %Y}{' at ' + planned_time if planned_time else ''}.", "success")
    today, hm = today_and_now()
    if new_date.isoformat() < today or (new_date.isoformat() == today and planned_time and planned_time < hm):
        flash("That time is already in the past, so the post will show as overdue.", "warning")
    return redirect(back)
