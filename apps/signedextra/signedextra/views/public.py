"""Public signing page reached from a signature link. No account needed."""
from flask import Blueprint, abort, render_template, request, send_file, url_for

from ..db import get_db
from ..ratelimit import blocked, limit_or_abort, record
from ..security import hash_token
from ..signing import load_sign_request, parse_signature_form, photo_path, record_signature, assert_hash_current
from ..tickets_service import get_lines, get_photos
from ..validation import Form

bp = Blueprint("public", __name__, url_prefix="/s")


@bp.before_request
def _rate_limit():
    """Per-IP limits: a cap on signing-page requests, and a lower cap on unknown links (stops guessing).
    Photo requests from a valid page aren't counted, so a ticket with many photos still loads."""
    if blocked("RATE_LIMIT_SIGN_BAD"):
        abort(429, description="Too many invalid signing links were opened from your network. Please wait 15 minutes.")
    token = (request.view_args or {}).get("token")
    if token is not None and get_db().execute("SELECT 1 FROM signature_requests WHERE token_hash = ?",
                                              (hash_token(token),)).fetchone() is None:
        record("RATE_LIMIT_SIGN_BAD")
        return None  # the view answers 404 as before
    if request.endpoint in ("public.sign", "public.decline"):
        limit_or_abort("RATE_LIMIT_SIGN")
    return None


@bp.after_request
def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _render(token, req, ticket, form, status=200):
    return render_template(
        "sign/sign.html", ticket=ticket, lines=get_lines(ticket["id"]), photos=get_photos(ticket["id"]),
        form=form, post_url=url_for("public.sign", token=token),
        decline_url=url_for("public.decline", token=token),
        photo_url=lambda p: url_for("public.photo", token=token, photo_id=p["id"]),
        company={"name": ticket["company_name"], "phone": ticket["company_phone"],
                 "email": ticket["company_email"]},
        public=True, expires_at=req["expires_at"]), status


@bp.route("/<token>", methods=["GET", "POST"])
def sign(token):
    req, ticket = load_sign_request(token)
    if request.method == "POST":
        assert_hash_current(ticket)
        form, png = parse_signature_form(request.form)
        if form.ok:
            record_signature(ticket, "link", "signed", form, png, request_row=req)
            return render_template("sign/done.html", outcome="signed", ticket=ticket)
        return _render(token, req, ticket, form, 422)
    return _render(token, req, ticket, Form({}))


@bp.route("/<token>/decline", methods=["POST"])
def decline(token):
    req, ticket = load_sign_request(token)
    form = Form(request.form)
    form.text("signer_name", "Your full name", max_len=120)
    form.text("signer_title", "Your title", max_len=120)
    form.text("signer_contact", "Email or phone", required=False, max_len=120)
    form.text("decline_reason", "Reason", max_len=1000)
    if not form.ok:
        return _render(token, req, ticket, form, 422)
    record_signature(ticket, "link", "declined", form, None, request_row=req)
    return render_template("sign/done.html", outcome="declined", ticket=ticket)


@bp.route("/<token>/photos/<int:photo_id>")
def photo(token, photo_id):
    req, ticket = load_sign_request(token)
    row = get_db().execute("SELECT * FROM attachments WHERE id = ? AND ticket_id = ? AND company_id = ?",
                           (photo_id, ticket["id"], ticket["company_id"])).fetchone()
    if row is None:
        abort(404)
    path = photo_path(row)
    if not path.exists():
        abort(404)
    return send_file(path, mimetype=row["content_type"], max_age=0)
