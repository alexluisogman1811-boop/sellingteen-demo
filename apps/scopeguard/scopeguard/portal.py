"""Client review pages. Access is granted only by an unexpired, unrevoked token, and every
lookup is scoped to the token's own project, so one client can never reach another's data.

These pages deliberately do not depend on cookies: form protection is an HMAC bound to the
review link, and results are shown via the redirect URL, so links keep working in email-app
browsers and other environments that block cookies."""
import hashlib
import hmac
import re

from flask import (
    Blueprint, abort, current_app, g, redirect, render_template, request, session, url_for,
)

from . import services as s
from .db import get_db

bp = Blueprint("portal", __name__, url_prefix="/review")

TOKEN_PREFIX = re.compile(r"^[A-Za-z0-9_-]+")
CONFIRM_TEXT = {
    "approved": "I confirm that I, {name}, approve this.",
    "changes_requested": "I confirm that I, {name}, am requesting changes.",
    "rejected": "I confirm that I, {name}, decline this.",
}
DONE_MESSAGES = {
    "scope_approved": "Thank you. Your approval of this scope version has been recorded.",
    "scope_changes_requested": "Thank you. Your request for changes has been recorded and shared with the freelancer.",
    "change_approved": "Thank you. You approved the change request.",
    "change_rejected": "Thank you. You rejected the change request.",
}


def form_token(link_id):
    key = current_app.config["SECRET_KEY"].encode("utf-8")
    return hmac.new(key, f"portal-form:{link_id}".encode(), hashlib.sha256).hexdigest()


@bp.url_value_preprocessor
def load_link(endpoint, values):
    token = values.get("token") if values else None
    db = get_db()
    link = s.resolve_token(db, token)
    if link is None:
        # Links pasted from emails often pick up trailing punctuation ("…AhI." or "…AhI)").
        m = TOKEN_PREFIX.match(token or "")
        if request.method == "GET" and m and m.group(0) != token and s.resolve_token(db, m.group(0)):
            abort(redirect(url_for(endpoint, **{**values, "token": m.group(0)})))
        abort(404)
    g.link = link
    # The link only ever opens its own project, and only within the account that issued it.
    g.project = db.execute(
        "SELECT p.*, c.name AS client_name, c.company AS client_company FROM projects p"
        " JOIN clients c ON c.id = p.client_id AND c.account_id = p.account_id"
        " WHERE p.id = ? AND p.account_id = ?",
        (link["project_id"], link["account_id"]),
    ).fetchone()
    if g.project is None:
        abort(404)
    # The project's own freelancer previewing the page is not the client: they can't decide for the
    # client, and the visit isn't counted as client use. (Someone signed in to a *different* account
    # is just a client here.)
    g.owner_preview = session.get("account_id") == g.project["account_id"]
    if not g.owner_preview:
        db.execute("UPDATE share_links SET last_used_at = ? WHERE id = ?", (s.now(), link["id"]))
        db.commit()


@bp.before_request
def check_form_token():
    if request.method == "POST":
        if not hmac.compare_digest(request.form.get("form_token", ""), form_token(g.link["id"])):
            abort(400, "This form could not be verified. Please reload the review page and try again.")
        if not g.link["can_decide"]:
            abort(403, "This review link is view-only. Ask the freelancer for a link that lets you approve or reject.")
        if g.owner_preview:
            # Never let the freelancer record a decision that would be labelled as the client's own.
            abort(403, "You are signed in to ScopeGuard as the freelancer, so you cannot record a decision as the "
                       "client. Ask the client to use the link, or use “Record off-platform decision” with evidence.")


def _client_decision_form(allowed):
    """Decisions are made only as the person the link was issued to; they confirm by typing their name."""
    f = s.Form(request.form)
    decision = f.choice("decision", "Decision", allowed)
    typed = f.text("approver_name", "Your full name", max_len=120)
    name = g.link["recipient_name"] or g.project["client_name"]
    email = g.link["recipient_email"]
    if typed and not s.same_person(typed, name):
        f.errors.append(f"This link was issued to {name}. Type that name exactly to confirm it is you. "
                        "If you are someone else, ask the freelancer for your own link.")
    comment = f.text("comment", "Comment", required=decision == "changes_requested", max_len=3000) or None
    if decision == "changes_requested" and not comment:
        f.errors[-1] = "Please describe the changes you would like in the comment box."
    if request.form.get("confirm") != "1":
        f.errors.append("Tick the confirmation box to record your decision.")
    f.check()
    return decision, name, email, comment


def _record(subject_type, subject_id, decision, name, email, comment):
    s.record_decision(
        get_db(), project_id=g.project["id"], subject_type=subject_type, subject_id=subject_id,
        decision=decision, approver_name=name, approver_email=email, method="client_review_link",
        comment=comment, evidence_note=CONFIRM_TEXT[decision].format(name=name),
        ip_address=request.remote_addr, user_agent=request.headers.get("User-Agent"),
        share_link_id=g.link["id"],
    )


def _decisions(db, project_id, where="", params=()):
    return db.execute(
        "SELECT a.*, v.version_no, cr.title AS cr_title FROM approvals a"
        " LEFT JOIN scope_versions v ON v.id = a.scope_version_id"
        " LEFT JOIN change_requests cr ON cr.id = a.change_request_id"
        f" WHERE a.project_id = ? {where} ORDER BY a.id DESC",
        (project_id, *params),
    ).fetchall()


def _render_review(token, status=200, errors=None, error_target=None):
    db = get_db()
    p = g.project
    latest = s.latest_version(db, p["id"])
    if latest is None:
        abort(404)
    scope = s.snapshot(latest)
    prev = db.execute("SELECT * FROM scope_versions WHERE project_id = ? AND version_no = ?",
                      (p["id"], latest["version_no"] - 1)).fetchone()
    versions = db.execute("SELECT * FROM scope_versions WHERE project_id = ? ORDER BY version_no DESC",
                          (p["id"],)).fetchall()
    approved_v, approved_dec = s.approved_version(db, p["id"])
    return render_template(
        "portal.html", token=token, p=p, latest=latest, scope=scope,
        diff=s.diff_scopes(s.snapshot(prev), scope) if prev else None,
        latest_dec=s.latest_decision(db, "scope_version", latest["id"]),
        approved_v=approved_v, approved_dec=approved_dec,
        versions=[(v, s.latest_decision(db, "scope_version", v["id"])) for v in versions],
        crs=s.group_change_requests(db, p["id"], client_view=True),
        baseline=s.scope_baseline(db, p["id"]),
        link=g.link,
        usage=s.revision_usage(db, p, scope),
        decisions=_decisions(db, p["id"]),
        form_token=form_token(g.link["id"]),
        errors=errors or [], error_target=error_target, form=request.form if errors else {},
        done=DONE_MESSAGES.get(request.args.get("done", "")),
    ), status


@bp.route("/<token>")
def review(token):
    return _render_review(token)


@bp.route("/<token>/versions/<int:version_no>")
def version(token, version_no):
    db = get_db()
    p = g.project
    v = db.execute("SELECT * FROM scope_versions WHERE project_id = ? AND version_no = ?",
                   (p["id"], version_no)).fetchone()
    if v is None:
        abort(404)
    prev = db.execute("SELECT * FROM scope_versions WHERE project_id = ? AND version_no = ?",
                      (p["id"], version_no - 1)).fetchone()
    latest = s.latest_version(db, p["id"])
    return render_template(
        "portal_version.html", token=token, p=p, v=v, scope=s.snapshot(v),
        diff=s.diff_scopes(s.snapshot(prev), s.snapshot(v)) if prev else None,
        is_latest=latest["id"] == v["id"],
        decisions=_decisions(db, p["id"], "AND a.scope_version_id = ?", (v["id"],)),
    )


@bp.route("/<token>/scope/<int:version_id>", methods=["POST"])
def decide_scope(token, version_id):
    db = get_db()
    latest = s.latest_version(db, g.project["id"])
    if latest is None or latest["id"] != version_id:
        # Unknown, another project's, or superseded version.
        return _render_review(token, 409, ["This scope version is no longer current. Please review the latest "
                                           "version below before deciding."], "decide")
    try:
        decision, name, email, comment = _client_decision_form(("approved", "changes_requested"))
        _record("scope_version", version_id, decision, name, email, comment)
    except s.ValidationError as exc:
        db.rollback()
        return _render_review(token, 400, exc.errors, "decide")
    s.log_event(db, g.project["id"], "client_decision",
                f"Client decision on scope v{latest['version_no']}: {s.DECISION_LABELS[decision]} by {name}"
                " (via review link).", f"client:{name}")
    db.commit()
    return redirect(url_for("portal.review", token=token, done=f"scope_{decision}", _anchor="top"))


@bp.route("/<token>/changes/<int:cr_id>", methods=["POST"])
def decide_change(token, cr_id):
    db = get_db()
    cr = db.execute("SELECT * FROM change_requests WHERE id = ? AND project_id = ? AND account_id = ?"
                    " AND submitted_at IS NOT NULL", (cr_id, g.project["id"], g.project["account_id"])).fetchone()
    if cr is None:  # another project's request, or a draft the client has never been shown
        abort(404)
    if cr["status"] != "submitted":
        return _render_review(token, 409, ["This change request has already been decided."], f"cr-{cr_id}")
    try:
        decision, name, email, comment = _client_decision_form(("approved", "rejected"))
        _record("change_request", cr_id, decision, name, email, comment)
        s.change_cr_status(db, cr, decision, f"client:{name}")
        s.log_event(db, g.project["id"], "client_decision",
                    f"Client decision on change request #{cr_id} “{cr['title']}”: {s.DECISION_LABELS[decision]} "
                    f"by {name} (via review link).", f"client:{name}")
    except s.ValidationError as exc:
        db.rollback()
        return _render_review(token, 400, exc.errors, f"cr-{cr_id}")
    db.commit()
    return redirect(url_for("portal.review", token=token, done=f"change_{decision}", _anchor="changes"))
