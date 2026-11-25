"""Landing page, dashboard, calendar, printable calendar, CSV export and image serving."""
import csv
import io
from datetime import timedelta

from flask import (Blueprint, Response, abort, current_app, flash, g, render_template, request,
                   send_from_directory)

from .. import timeutil
from ..auth import current_account_id
from ..calendar_utils import parse_anchor, view_range
from ..constants import PLATFORMS, STATUS_LABELS, STATUSES
from ..db import get_db
from ..queries import empty_filters, fetch_posts, filter_query_args, parse_filters
from ..uploads import is_stored_name

bp = Blueprint("main", __name__)


def filter_choices(conn, account_id):
    return {
        "campaigns": conn.execute("SELECT id, name, color FROM campaigns WHERE account_id = ? ORDER BY name",
                                  (account_id,)).fetchall(),
        "categories": conn.execute("SELECT id, name FROM categories WHERE account_id = ? ORDER BY name",
                                   (account_id,)).fetchall(),
    }


@bp.route("/")
def dashboard():
    if g.get("account") is None:
        return render_template("landing.html")
    conn = get_db()
    account_id = current_account_id()
    counts = {s: 0 for s in STATUSES}
    for row in conn.execute("SELECT status, COUNT(*) AS n FROM posts WHERE account_id = ? GROUP BY status",
                            (account_id,)):
        counts[row["status"]] = row["n"]

    overdue = fetch_posts(conn, account_id, {**empty_filters(), "status": "overdue"})
    overdue_ids = {p["id"] for p in overdue}
    today = timeutil.today()
    upcoming = [
        p for p in fetch_posts(
            conn, account_id,
            {**empty_filters(), "status": "active", "date_from": today, "date_to": today + timedelta(days=6)},
        )
        if p["id"] not in overdue_ids
    ]
    drafts = fetch_posts(conn, account_id, {**empty_filters(), "status": "draft"}, order="p.updated_at DESC", limit=5)
    in_review = fetch_posts(conn, account_id, {**empty_filters(), "status": "review"}, order="p.updated_at DESC", limit=5)
    published = fetch_posts(conn, account_id, {**empty_filters(), "status": "published"},
                            order="p.published_at DESC", limit=5)
    campaigns = conn.execute(
        """SELECT c.id, c.name, c.color,
                  SUM(CASE WHEN p.status IN ('draft','review','scheduled') THEN 1 ELSE 0 END) AS open_posts,
                  SUM(CASE WHEN p.status = 'published' THEN 1 ELSE 0 END) AS published_posts
           FROM campaigns c LEFT JOIN posts p ON p.campaign_id = c.id AND p.account_id = c.account_id
           WHERE c.account_id = ?
           GROUP BY c.id ORDER BY c.name""",
        (account_id,),
    ).fetchall()
    is_empty = conn.execute("SELECT 1 FROM posts WHERE account_id = ? LIMIT 1", (account_id,)).fetchone() is None
    return render_template(
        "dashboard.html",
        counts=counts,
        overdue=overdue,
        upcoming=upcoming,
        drafts=drafts,
        in_review=in_review,
        published=published,
        campaigns=campaigns,
        is_empty=is_empty,
    )


def _calendar_context():
    conn = get_db()
    account_id = current_account_id()
    view = request.args.get("view", "month")
    if view not in ("month", "week"):
        view = "month"
    anchor = parse_anchor(request.args.get("date"), timeutil.today())
    filters, errors = parse_filters(request.args)
    # The calendar decides the date range itself.
    filters["date_from"] = filters["date_to"] = None
    filters["undated"] = False

    weeks, start, end, prev_anchor, next_anchor, title = view_range(view, anchor)
    posts = fetch_posts(conn, account_id, {**filters, "date_from": start, "date_to": end})
    by_day = {}
    for post in posts:
        by_day.setdefault(post["planned_date"], []).append(post)
    archived_hidden = 0
    if filters["status"] == "":  # default view hides archived posts; say so instead of silently dropping them
        archived_hidden = len(fetch_posts(conn, account_id,
                                          {**filters, "status": "archived", "date_from": start, "date_to": end}))
    undated_count = conn.execute(
        "SELECT COUNT(*) FROM posts WHERE account_id = ? AND planned_date IS NULL AND status != 'archived'",
        (account_id,),
    ).fetchone()[0]
    return dict(
        view=view,
        anchor=anchor,
        weeks=weeks,
        start=start,
        end=end,
        prev_anchor=prev_anchor,
        next_anchor=next_anchor,
        title=title,
        by_day=by_day,
        total=len(posts),
        filters=filters,
        filter_args=filter_query_args(filters),
        filter_errors=errors,
        undated_count=undated_count,
        archived_hidden=archived_hidden,
        **filter_choices(conn, account_id),
    )


@bp.route("/calendar")
def calendar_view():
    ctx = _calendar_context()
    for msg in ctx["filter_errors"]:
        flash(msg, "warning")
    return render_template("calendar.html", **ctx)


@bp.route("/calendar/print")
def calendar_print():
    return render_template("calendar_print.html", **_calendar_context())


CSV_COLUMNS = [
    "id", "title", "platform", "status", "campaign", "category", "planned_date", "planned_time",
    "caption", "hashtags", "notes", "marked_published_by_user_at", "published_url", "created_at", "updated_at",
]


def csv_safe(value):
    """Neutralise spreadsheet formula injection (=, +, -, @ at cell start)."""
    if value is None:
        return ""
    text = str(value)
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


@bp.route("/export.csv")
def export_csv():
    conn = get_db()
    # Same filter semantics as the Posts list, so "Export CSV" contains exactly
    # the rows on screen. Use ?status=all for a complete export.
    filters, _errors = parse_filters(request.args)
    posts = fetch_posts(conn, current_account_id(), filters)
    buf = io.StringIO()
    buf.write("﻿")  # BOM so Excel opens UTF-8 (emoji, accents) correctly
    writer = csv.writer(buf)
    writer.writerow(CSV_COLUMNS)
    for p in posts:
        writer.writerow([
            csv_safe(v) for v in (
                p["id"], p["title"], PLATFORMS.get(p["platform"], {}).get("label", p["platform"]),
                STATUS_LABELS.get(p["status"], p["status"]), p["campaign_name"], p["category_name"],
                p["planned_date"], p["planned_time"], p["caption"], p["hashtags"], p["notes"],
                p["published_at"], p["published_url"], p["created_at"], p["updated_at"],
            )
        ])
    filename = f"contentqueue-export-{timeutil.today():%Y%m%d}.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.route("/uploads/<filename>")
def uploaded_file(filename):
    # An image is only served to the account whose post uses it. Other accounts get the
    # same 404 as for a file that doesn't exist, even if they guess the random name.
    if not is_stored_name(filename):
        abort(404)
    owned = get_db().execute("SELECT 1 FROM posts WHERE image_filename = ? AND account_id = ? LIMIT 1",
                             (filename, current_account_id())).fetchone()
    if owned is None:
        abort(404)
    resp = send_from_directory(current_app.config["UPLOAD_FOLDER"], filename)
    resp.headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"
    resp.headers["Cache-Control"] = "private, no-store"
    return resp
