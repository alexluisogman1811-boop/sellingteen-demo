"""Public, account-free customer view of a shared report: /report/<token>.

Only the random token identifies the report; no internal IDs appear in these URLs.
"""
import os
import re

from flask import Blueprint, abort, make_response, render_template, send_file, url_for

from . import images
from .db import get_db
from .models import get_photos, get_report, photos_by_stage
from .profile import logo_path
from .reports import pdf_response, photo_disk_path

bp = Blueprint("share", __name__, url_prefix="/report")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{20,100}$")


def _resolve(token):
    """Return (link, job, business) or render the right error page."""
    if not TOKEN_RE.match(token or ""):
        return None, "invalid"
    row = get_db().execute(
        "SELECT s.id AS link_id, s.revoked_at, r.job_id, j.business_id FROM share_links s "
        "JOIN reports r ON r.id = s.report_id JOIN jobs j ON j.id = r.job_id WHERE s.token = ?",
        (token,),
    ).fetchone()
    if row is None:
        return None, "invalid"
    if row["revoked_at"]:
        return None, "revoked"
    db = get_db()
    job = db.execute("SELECT * FROM jobs WHERE id = ?", (row["job_id"],)).fetchone()
    business = db.execute("SELECT * FROM businesses WHERE id = ?", (row["business_id"],)).fetchone()
    return (job, business), None


def _error(kind):
    if kind == "revoked":
        return render_template("share/unavailable.html", title="This link is no longer active",
                               message="The business that sent you this report has turned this link off. "
                                       "Please contact them for an updated link."), 410
    return render_template("share/unavailable.html", title="Report not found",
                           message="This report link isn't valid. Check that you copied the whole link, "
                                   "or ask the business to send it again."), 404


def _headers(response):
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.route("/<token>")
def view(token):
    found, err = _resolve(token)
    if err:
        return _error(err)
    job, business = found
    report = get_report(job["id"])
    photos = get_photos(job["id"])
    html = render_template(
        "share/view.html", job=job, business=business, report=report, content=report["content"],
        grouped=photos_by_stage(photos), token=token,
        photo_url=lambda p: url_for("share.photo", token=token, file_key=p["file_key"]),
        logo_url=url_for("share.logo", token=token) if logo_path(business) else None,
    )
    return _headers(make_response(html))


@bp.route("/<token>/photo/<file_key>")
def photo(token, file_key):
    found, err = _resolve(token)
    if err or not images.KEY_RE.match(file_key or ""):
        abort(404)
    job, _business = found
    row = get_db().execute("SELECT * FROM photos WHERE file_key = ? AND job_id = ?",
                           (file_key, job["id"])).fetchone()
    if row is None or not os.path.exists(photo_disk_path(row)):
        abort(404)
    return _headers(send_file(photo_disk_path(row), max_age=0))


@bp.route("/<token>/logo")
def logo(token):
    found, err = _resolve(token)
    if err:
        abort(404)
    path = logo_path(found[1])
    if not path:
        abort(404)
    return _headers(send_file(path, max_age=0))


@bp.route("/<token>/pdf")
def pdf(token):
    found, err = _resolve(token)
    if err:
        return _error(err)
    job, business = found
    # The public copy leaves out the customer's phone and email, matching the share page.
    response = pdf_response(business, job, get_report(job["id"]), get_photos(job["id"]),
                            include_customer_contact=False)
    if response is None:
        return render_template("share/unavailable.html", title="PDF unavailable",
                               message="We couldn't create the PDF right now. You can still view the report "
                                       "online - please try the download again later."), 500
    return _headers(response)
