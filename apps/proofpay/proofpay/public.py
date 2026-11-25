"""Public pages: marketing site and the customer-facing job report.

Customer pages are addressed only by an unguessable share token. They show the job,
approved summary, photos and invoice status, never internal IDs, other jobs, or the
customer's own contact details.
"""
import re

from flask import Blueprint, abort, jsonify, make_response, redirect, render_template, request

from .models import Job, JobPhoto, db
from .payments import get_provider
from .photos import send_photo

bp = Blueprint("public", __name__)

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")


def _job_for_token(token: str) -> Job:
    if not TOKEN_RE.match(token or ""):
        abort(404)
    job = db.session.scalar(db.select(Job).filter_by(share_token=token))
    if job is None:
        abort(404)
    return job


def _private(resp):
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


@bp.route("/")
def home():
    return render_template("public/home.html")


@bp.route("/pricing")
def pricing():
    return render_template("public/pricing.html")


@bp.route("/healthz")
def healthz():
    """For uptime monitors. No login, no database query: it only shows the app is running."""
    resp = jsonify(status="ok")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.route("/p/<token>")
def job_page(token):
    job = _job_for_token(token)
    return _private(make_response(render_template("public/job.html", job=job, token=token)))


@bp.route("/p/<token>/pay")
def pay(token):
    job = _job_for_token(token)
    checkout = None
    if not job.is_paid and job.price_cents > 0:
        checkout = get_provider().create_checkout(job.invoice)
        if checkout.online and checkout.redirect_url:
            return redirect(checkout.redirect_url)
    return _private(make_response(
        render_template("public/pay.html", job=job, token=token, checkout=checkout)))


@bp.route("/p/<token>/photos/<photo_ref>")
def photo(token, photo_ref):
    job = _job_for_token(token)
    photo = db.session.scalar(db.select(JobPhoto).filter_by(ref=photo_ref, job_id=job.id))
    if photo is None:
        abort(404)
    resp = send_photo(photo, thumb=request.args.get("thumb") == "1")
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp
