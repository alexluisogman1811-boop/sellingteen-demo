"""Client portal: token-scoped, client sees only their own client-assigned items."""
from flask import (Blueprint, abort, current_app, flash, redirect, render_template, request,
                   send_file, url_for)

from . import services as svc
from .db import get_db, now_iso, today
from .security import ALLOWED_UPLOADS, UploadError, is_well_formed_token, upload_path, validate_upload

bp = Blueprint("portal", __name__, url_prefix="/p")

# Portal flashes use their own categories so agency messages (which can name other
# clients) never render in a portal page, and portal messages never leak into owner pages.
PORTAL_OK, PORTAL_ERR = "portal-success", "portal-error"


@bp.after_request
def no_store(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


def client_for(token):
    """Resolve a portal token to its client.

    Malformed, unknown, disabled, and archived links all get the same 404. A correct but
    expired link gets 410 so the client knows to ask for a new one (it reveals nothing
    to someone who doesn't already hold the exact token).
    """
    if not is_well_formed_token(token):
        abort(404)
    # The business name shown to the client comes from that client's own account.
    client = get_db().execute(
        """SELECT c.*, a.name AS business_name FROM clients c JOIN accounts a ON a.id = c.account_id
           WHERE c.portal_token = ? AND c.portal_enabled = 1 AND c.status != 'archived'""",
        (token,)).fetchone()
    if client is None:
        abort(404)
    if svc.portal_link_state(client) == "expired":
        abort(410)
    return client


def portal_task(client, task_id):
    """A task is visible only if it belongs to THIS client and is assigned to the client."""
    task = get_db().execute(
        "SELECT * FROM tasks WHERE id = ? AND client_id = ? AND account_id = ? AND assignee = 'client'",
        (task_id, client["id"], client["account_id"])).fetchone()
    if task is None:
        abort(404)
    return task


def editable_task(client, task_id):
    task = portal_task(client, task_id)
    if client["status"] != "active" or task["status"] == "done":
        abort(403, description="This item is already complete and can no longer be changed.")
    return task


def home_url(token, task_id=None):
    return url_for("portal.home", token=token) + (f"#task-{task_id}" if task_id else "")


@bp.get("/<token>")
def home(token):
    client = client_for(token)
    db = get_db()
    db.execute("UPDATE clients SET portal_last_viewed_at = ? WHERE id = ?", (now_iso(), client["id"]))
    db.commit()
    tasks = db.execute(
        "SELECT * FROM tasks WHERE client_id = ? AND account_id = ? AND assignee = 'client' ORDER BY position, id",
        (client["id"], client["account_id"])).fetchall()
    ids = [t["id"] for t in tasks]
    files, comments = {}, {}
    if ids:
        marks = ",".join("?" * len(ids))
        for f in db.execute(f"SELECT * FROM uploads WHERE account_id = ? AND task_id IN ({marks}) ORDER BY id",
                            [client["account_id"], *ids]):
            files.setdefault(f["task_id"], []).append(f)
        for c in db.execute(
                f"SELECT * FROM comments WHERE account_id = ? AND task_id IN ({marks}) AND is_internal = 0 ORDER BY id",
                [client["account_id"], *ids]):
            comments.setdefault(c["task_id"], []).append(c)
    # Corrections first, then everything else still with the client; submitted; done.
    groups = [
        ("needs_you", "Needs your attention",
         [t for t in tasks if t["status"] == "needs_correction"]
         + [t for t in tasks if t["status"] in ("todo", "in_progress")]),
        ("in_review", "With the team for review", [t for t in tasks if t["status"] == "submitted"]),
        ("done", "Completed", [t for t in tasks if t["status"] == "done"]),
    ]
    return render_template(
        "portal.html", client=client, tasks=tasks, groups=groups, files=files, comments=comments,
        token=token, client_progress=svc.calculate_progress(t["status"] for t in tasks),
        is_overdue=svc.is_overdue, allowed_ext=sorted(ALLOWED_UPLOADS),
        max_mb=current_app.config["MAX_UPLOAD_BYTES"] // (1024 * 1024),
        link_state=svc.portal_link_state(client), today=today().isoformat(),
        agency_name=client["business_name"],  # this business's own name, never another account's
    )


@bp.post("/<token>/tasks/<int:task_id>/answer")
def answer(token, task_id):
    client = client_for(token)
    task = editable_task(client, task_id)
    if task["kind"] != "question":
        abort(400)
    value = (request.form.get("answer") or "").strip()[:5000]
    if not value:
        flash("Please write an answer before submitting.", PORTAL_ERR)
        return redirect(home_url(token, task_id))
    db = get_db()
    db.execute("UPDATE tasks SET answer = ? WHERE id = ? AND account_id = ?", (value, task_id, client["account_id"]))
    svc.set_task_status(task, "submitted", actor="client")
    svc.log_activity(client["id"], "client", f"Answered “{task['title']}”.")
    db.commit()
    flash("Thanks! Your answer was submitted for review.", PORTAL_OK)
    return redirect(home_url(token, task_id))


@bp.post("/<token>/tasks/<int:task_id>/complete")
def complete(token, task_id):
    client = client_for(token)
    task = editable_task(client, task_id)
    if task["kind"] == "question" and not task["answer"]:
        flash("Please answer the question first.", PORTAL_ERR)
        return redirect(home_url(token, task_id))
    if task["kind"] == "file" and not get_db().execute(
            "SELECT 1 FROM uploads WHERE task_id = ? AND account_id = ?", (task_id, client["account_id"])).fetchone():
        flash("Please upload the requested file first.", PORTAL_ERR)
        return redirect(home_url(token, task_id))
    # Simple actions are complete when the client says so; answers and files go to review.
    new_status = "done" if task["kind"] == "task" else "submitted"
    svc.set_task_status(task, new_status, actor="client")
    svc.log_activity(client["id"], "client", f"Marked “{task['title']}” complete.")
    get_db().commit()
    flash("Marked complete — thank you!" if new_status == "done" else "Submitted for review — thank you!", PORTAL_OK)
    return redirect(home_url(token, task_id))


@bp.post("/<token>/tasks/<int:task_id>/upload")
def upload(token, task_id):
    client = client_for(token)
    task = editable_task(client, task_id)
    if task["kind"] != "file":
        abort(400)
    try:
        display, ext, mime, data = validate_upload(request.files.get("file"))
    except UploadError as e:
        flash(str(e), PORTAL_ERR)
        return redirect(home_url(token, task_id))
    svc.save_upload(task, display, ext, mime, data, "client")
    svc.set_task_status(task, "submitted", actor="client")
    svc.log_activity(client["id"], "client", f"Uploaded {display} for “{task['title']}”.")
    get_db().commit()
    flash(f"Uploaded {display}. It's now with the team for review.", PORTAL_OK)
    return redirect(home_url(token, task_id))


@bp.post("/<token>/tasks/<int:task_id>/comment")
def comment(token, task_id):
    client = client_for(token)
    task = portal_task(client, task_id)
    body = (request.form.get("body") or "").strip()[:4000]
    if not body:
        flash("Message cannot be empty.", PORTAL_ERR)
    else:
        svc.add_comment(task, "client", body)
        svc.log_activity(client["id"], "client", f"Commented on “{task['title']}”.")
        get_db().commit()
        flash("Message sent to the team.", PORTAL_OK)
    return redirect(home_url(token, task_id))


def portal_file(client, file_id):
    """An upload is reachable only through its own client's token and a client-assigned task."""
    f = get_db().execute(
        """SELECT u.* FROM uploads u JOIN tasks t ON t.id = u.task_id
           WHERE u.id = ? AND u.client_id = ? AND t.client_id = ? AND t.assignee = 'client'
             AND u.account_id = ? AND t.account_id = ?""",
        (file_id, client["id"], client["id"], client["account_id"], client["account_id"])).fetchone()
    if f is None:
        abort(404)
    return f


@bp.get("/<token>/files/<int:file_id>")
def file_download(token, file_id):
    f = portal_file(client_for(token), file_id)
    return send_file(upload_path(f["stored_name"]), mimetype=f["mime_type"], as_attachment=True,
                     download_name=f["original_name"])


@bp.post("/<token>/files/<int:file_id>/delete")
def file_delete(token, file_id):
    """Let a client remove a file they uploaded by mistake, until the item is approved."""
    client = client_for(token)
    f = portal_file(client, file_id)
    task = editable_task(client, f["task_id"])
    if f["uploaded_by"] != "client":
        abort(403, description="Only files you uploaded can be removed.")
    svc.delete_upload(f, "client")
    remaining = get_db().execute("SELECT COUNT(*) FROM uploads WHERE task_id = ? AND account_id = ?",
                                 (task["id"], task["account_id"])).fetchone()[0]
    if remaining == 0 and task["status"] == "submitted":
        svc.set_task_status(task, "todo", actor="client")
        get_db().commit()
    flash(f"Removed {f['original_name']}.", PORTAL_OK)
    return redirect(home_url(token, task["id"]))
