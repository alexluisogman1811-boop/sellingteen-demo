"""Public, unauthenticated routes: submission form and approved-testimonial pages.

These routes only ever look up a project by its unguessable token and never
reveal pending, rejected, archived or permission-less testimonials. They are used by the
account owner's clients and website visitors, so they work regardless of the owner's
trial or subscription state (see billing.NEVER_GATED_BLUEPRINTS).
"""
import secrets
import sqlite3
from datetime import datetime, timezone

from flask import Blueprint, abort, current_app, redirect, render_template, request, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .db import get_db, log_activity, publishable_for_project, utcnow
from .security import client_ip
from .validation import LIMITS, validate_testimonial

bp = Blueprint("public", __name__)

HONEYPOT_FIELD = "contact_fax"


def permission_statement(business_name):
    """The exact consent text shown to (and stored for) the client. Names the business."""
    who = business_name or "the business that sent me this link"
    return (f"I give permission for {who} to publish my name, business, rating and the feedback "
            "above as a testimonial on its website and marketing materials.")


def _project_by(column, token):
    if column not in ("submit_token", "wall_token"):
        raise ValueError(column)
    # Tokens are URL-safe base64; reject anything else before touching the DB.
    if not token or len(token) > 64 or not all(c.isalnum() or c in "-_" for c in token):
        abort(404)
    # The owning account's public profile comes along: business name and website only.
    row = get_db().execute(
        f"SELECT p.*, a.business_name, a.website AS business_website FROM projects p "
        f"JOIN accounts a ON a.id = p.account_id WHERE p.{column} = ?", (token,)).fetchone()
    if row is None:
        abort(404)
    return row


def _form_serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="submission-form")


def _render_form(project, values=None, errors=None, status=200):
    business_name = project["business_name"]
    # "n" is a single-use nonce: a stamp can create at most one testimonial.
    # "b" is the business name shown in the consent text, so we store exactly what was agreed to.
    stamp = _form_serializer().dumps({"p": project["id"], "n": secrets.token_urlsafe(16),
                                      "b": business_name})
    return render_template(
        "public/submit.html", project=project, values=values or {}, errors=errors or {},
        form_stamp=stamp, limits=LIMITS, permission_statement=permission_statement(business_name),
        honeypot_field=HONEYPOT_FIELD, business_name=business_name,
    ), status


def _check_form_stamp(project):
    """Return (error message or None, payload dict) for the submitted form stamp."""
    try:
        payload, signed_at = _form_serializer().loads(
            request.form.get("form_stamp", ""),
            max_age=current_app.config["FORM_MAX_AGE_SECONDS"], return_timestamp=True,
        )
    except SignatureExpired:
        return "This form was open for a long time, so we refreshed it. Please press Send again.", {}
    except BadSignature:
        return "This form could not be verified. Please check your answers and press Send again.", {}
    if not isinstance(payload, dict) or payload.get("p") != project["id"]:
        return "This form could not be verified. Please check your answers and press Send again.", {}
    elapsed = (datetime.now(timezone.utc) - signed_at).total_seconds()
    if elapsed < current_app.config["MIN_SUBMIT_SECONDS"]:
        return "That was quick! Please take a moment to review your feedback, then submit again.", {}
    return None, payload


def _form_values(data, permission):
    return dict(data, rating=request.form.get("rating", ""), permission=permission)


@bp.route("/s/<token>", methods=["GET", "POST"])
def submit(token):
    project = _project_by("submit_token", token)
    if not project["accepting"]:
        return render_template("public/closed.html", project=project), 200
    if request.method == "GET":
        return _render_form(project)

    thanks = redirect(url_for("public.thanks", token=token))
    limiter, ip = current_app.limiter, client_ip()

    # Coarse flood guard on every POST (generous, so honest typos never hit it).
    limit, window = current_app.config["SUBMIT_ATTEMPT_LIMIT"]
    if not limiter.hit(f"submit-attempt:{ip}", limit, window):
        abort(429, description="Too many attempts from your network. Please try again later.")

    # Honeypot: real visitors never see or fill this field. Pretend success, store nothing.
    if request.form.get(HONEYPOT_FIELD):
        return thanks

    data, errors = validate_testimonial(request.form)
    permission = request.form.get("permission") == "yes"
    stamp_error, stamp = _check_form_stamp(project)
    if stamp_error:
        errors["form"] = stamp_error
    if errors:
        return _render_form(project, _form_values(data, permission), errors, status=400)
    nonce = stamp.get("n") if isinstance(stamp.get("n"), str) else None
    shown_name = stamp["b"] if isinstance(stamp.get("b"), str) else project["business_name"]

    db = get_db()
    # Double-click, back-button resubmit or replayed stamp: already stored, just say thanks.
    if nonce and db.execute("SELECT 1 FROM testimonials WHERE submission_key = ?", (nonce,)).fetchone():
        return thanks
    if db.execute(
        "SELECT 1 FROM testimonials WHERE project_id = ? AND feedback = ? "
        "AND submitted_at >= datetime('now', '-1 day')",
        (project["id"], data["feedback"]),
    ).fetchone():
        return thanks

    # Only stored submissions count toward the per-IP submission quota.
    limit, window = current_app.config["SUBMIT_RATE_LIMIT"]
    if not limiter.hit(f"submit:{ip}", limit, window):
        errors["form"] = ("We've received several submissions from your network recently. "
                          "Please wait a few minutes and press Send again. Your text is kept below.")
        return _render_form(project, _form_values(data, permission), errors, status=429)

    now = utcnow()
    try:
        cur = db.execute(
            "INSERT INTO testimonials (account_id, project_id, customer_name, business, feedback, rating, "
            "website, status, permission_granted, permission_source, permission_note, permission_at, is_demo, "
            "submission_key, submitted_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project["account_id"], project["id"], data["customer_name"], data["business"], data["feedback"],
                data["rating"], data["website"],
                1 if permission else 0,
                "submitter" if permission else "",
                f'Submitter ticked: "{permission_statement(shown_name)}"' if permission else "",
                now if permission else None,
                project["is_demo"], nonce, now, now,
            ),
        )
    except sqlite3.IntegrityError:
        # A concurrent request with the same stamp won the race.
        db.rollback()
        return thanks
    log_activity(
        "submitted",
        f"New submission from {data['customer_name']}"
        + (" (with publication permission)" if permission else " (no publication permission)"),
        project_id=project["id"], testimonial_id=cur.lastrowid, is_demo=project["is_demo"],
        account_id=project["account_id"],
    )
    db.commit()
    return redirect(url_for("public.thanks", token=token))


@bp.route("/s/<token>/thanks")
def thanks(token):
    project = _project_by("submit_token", token)
    return render_template("public/thanks.html", project=project)


@bp.route("/w/<token>")
def wall(token):
    project = _project_by("wall_token", token)
    return render_template("public/wall.html", project=project,
                           profile={"business_name": project["business_name"],
                                    "website": project["business_website"]},
                           testimonials=publishable_for_project(project["id"]))


@bp.route("/w/<token>/embed")
def embed(token):
    project = _project_by("wall_token", token)
    return render_template("public/embed.html", project=project,
                           testimonials=publishable_for_project(project["id"]))
