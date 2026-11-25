"""Demo data and workspace reset — always limited to the signed-in account."""
import os

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from ..auth import current_account_id
from ..db import get_db
from ..demo import account_backup_folder, demo_present, load_demo, remove_demo, reset_workspace

bp = Blueprint("data", __name__, url_prefix="/data")

RESET_PHRASE = "RESET"


@bp.route("")
def index():
    conn = get_db()
    account_id = current_account_id()
    stats = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table} WHERE account_id = ?", (account_id,)).fetchone()[0]
        for table in ("posts", "campaigns", "categories", "templates", "ideas")
    }
    backup_root = account_backup_folder(current_app.config["BACKUP_FOLDER"], account_id)
    backups = sorted(os.listdir(backup_root), reverse=True)[:10] if os.path.isdir(backup_root) else []
    return render_template(
        "data.html", stats=stats, demo_loaded=demo_present(conn, account_id), backups=backups,
        reset_phrase=RESET_PHRASE,
    )


@bp.post("/demo")
def demo():
    if load_demo(get_db(), current_account_id()):
        flash("Demo data loaded. Everything is labelled as demo and can be removed in one click.", "success")
    else:
        flash("Demo data is already loaded.", "info")
    return redirect(url_for("main.dashboard"))


@bp.post("/demo/remove")
def demo_remove():
    counts = remove_demo(get_db(), current_account_id(), current_app.config["UPLOAD_FOLDER"])
    flash(f"Removed demo data ({counts['posts']} posts). Your own content was kept.", "success")
    return redirect(url_for("data.index"))


@bp.post("/reset")
def reset():
    if request.form.get("confirm_text", "").strip() != RESET_PHRASE:
        flash(f"Type {RESET_PHRASE} to confirm the reset. Nothing was deleted.", "error")
        return redirect(url_for("data.index"))
    conn = get_db()
    account_id = current_account_id()
    backup_dir = reset_workspace(conn, account_id, current_app.config["UPLOAD_FOLDER"],
                                 current_app.config["BACKUP_FOLDER"])
    if request.form.get("reload_demo") == "1":
        load_demo(conn, account_id)
    flash(f"Workspace reset. A backup of your content was saved ({os.path.basename(backup_dir)}).",
          "success")
    return redirect(url_for("data.index"))
