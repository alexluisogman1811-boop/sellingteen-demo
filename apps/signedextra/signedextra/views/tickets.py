"""Ticket pages: create, edit, price, photos, on-site signing, signature links, notices, void."""
from urllib.parse import quote

from flask import (Blueprint, Response, abort, flash, g, redirect, render_template, request,
                   send_file, session, url_for)

from ..db import get_db
from ..links import shareable_url
from ..security import audit, can_see_prices, is_manager, login_required, roles_required
from ..signing import (MAX_PHOTOS_PER_TICKET, NOTICE_REASONS, assert_hash_current, create_sign_link,
                       notice_recipients, parse_signature_form, photo_path, prepare_notice,
                       record_signature, store_photo)
from ..tickets_service import (EDITABLE, NOTICE_FROM, PRICE_EDITABLE, STATUS_LABELS, VOIDABLE, bump_version,
                               create_ticket, get_job, get_lines, get_photos, get_ticket, has_unpriced_lines,
                               labor_rates, set_status, update_pricing, update_ticket, user_can_access_job)
from ..util import money_to_cents, now_iso, x100_to_str
from ..validation import Form, parse_lines

bp = Blueprint("tickets", __name__)


def _ticket(ticket_id):
    return get_ticket(ticket_id, g.company["id"], g.user)


def _ticket_rates(ticket_id):
    """Active rates plus any (since deactivated) rate already used on this ticket,
    so existing drafts stay editable after an office deactivates a worker type."""
    rates = {r["id"]: r for r in labor_rates(g.company["id"])}
    for r in get_db().execute(
            "SELECT DISTINCT r.* FROM labor_rates r JOIN ticket_lines l ON l.labor_rate_id = r.id"
            " WHERE l.ticket_id = ? AND r.company_id = ?", (ticket_id, g.company["id"])):
        rates.setdefault(r["id"], r)
    return sorted(rates.values(), key=lambda r: r["label"])


def _parse_ticket_form(data, rates):
    form = Form(data)
    fields = {
        "description": form.text("description", "Description of the extra work", max_len=4000),
        "requested_by_name": form.text("requested_by_name", "Who asked for it", max_len=120),
        "requested_by_role": form.text("requested_by_role", "Their role", required=False, max_len=120),
        "work_date": form.date("work_date", "Work date"),
    }
    lines, line_errors = parse_lines(data, {r["id"] for r in rates})
    if line_errors:
        form.errors["lines"] = " ".join(line_errors)
    elif not lines:
        form.errors["lines"] = "Add at least one labor or material line."
    return form, fields, lines


def _form_lines_from_db(lines):
    return [{"kind": l["kind"], "labor_rate_id": l["labor_rate_id"], "description": l["description"],
             "qty_raw": x100_to_str(l["qty_x100"]), "unit": l["unit"],
             "rate_raw": "" if l["rate_cents"] is None else f"{l['rate_cents'] / 100:.2f}"}
            for l in lines]


@bp.route("/tickets")
@login_required
def ticket_list():
    status = request.args.get("status", "")
    sql = ("SELECT t.*, j.name AS job_name, c.name AS customer_name FROM tickets t JOIN jobs j ON j.id = t.job_id"
           " JOIN customers c ON c.id = j.customer_id WHERE t.company_id = ?")
    params = [g.company["id"]]
    if g.user["role"] == "foreman":
        sql += " AND t.job_id IN (SELECT job_id FROM job_members WHERE user_id = ?)"
        params.append(g.user["id"])
    if status in STATUS_LABELS:
        sql += " AND t.status = ?"
        params.append(status)
    rows = get_db().execute(sql + " ORDER BY t.updated_at DESC LIMIT 200", params).fetchall()
    return render_template("tickets/list.html", tickets=rows, status=status, statuses=STATUS_LABELS)


@bp.route("/jobs/<int:job_id>/tickets/new", methods=["GET", "POST"])
@login_required
def new_ticket(job_id):
    job = get_job(job_id, g.company["id"])
    if not user_can_access_job(g.user, job_id) or job["status"] != "active":
        abort(404)
    rates = labor_rates(g.company["id"])
    if not rates:
        flash("Add at least one labor rate in Settings before creating tickets." if is_manager()
              else "Your office needs to set up labor rates before tickets can be created.", "error")
        return redirect(url_for("core.settings") if is_manager() else url_for("core.jobs"))
    if request.method == "POST":
        form, fields, lines = _parse_ticket_form(request.form, rates)
        if form.ok:
            ticket_id = create_ticket(g.company, job, fields, lines, include_prices=can_see_prices())
            flash("Ticket saved. Add photos, then get it signed.", "success")
            return redirect(url_for("tickets.ticket_detail", ticket_id=ticket_id))
        form_lines = lines
        status = 422
    else:
        form = Form({})
        form.values["work_date"] = now_iso()[:10]
        form_lines, status = [], 200
    return render_template("tickets/form.html", form=form, job=job, rates=rates, form_lines=form_lines,
                           ticket=None), status


@bp.route("/tickets/<int:ticket_id>/edit", methods=["GET", "POST"])
@login_required
def edit_ticket(ticket_id):
    ticket = _ticket(ticket_id)
    if ticket["status"] not in EDITABLE:
        abort(409, description="This ticket is locked and can no longer be edited.")
    job = get_job(ticket["job_id"], g.company["id"])
    rates = _ticket_rates(ticket_id)
    if request.method == "POST":
        form, fields, lines = _parse_ticket_form(request.form, rates)
        if form.ok:
            update_ticket(ticket, fields, lines, include_prices=can_see_prices())
            flash("Ticket updated. Any signature link sent earlier no longer works; send a new one.", "success")
            return redirect(url_for("tickets.ticket_detail", ticket_id=ticket_id))
        form_lines, status = lines, 422
    else:
        form = Form({})
        form.values = dict(ticket)
        form_lines, status = _form_lines_from_db(get_lines(ticket_id)), 200
    return render_template("tickets/form.html", form=form, job=job, rates=rates, form_lines=form_lines,
                           ticket=ticket), status


@bp.route("/tickets/<int:ticket_id>")
@login_required
def ticket_detail(ticket_id):
    ticket = _ticket(ticket_id)
    db = get_db()
    job = get_job(ticket["job_id"], g.company["id"])
    lines = get_lines(ticket_id)
    signatures = db.execute("SELECT id, method, outcome, signer_name, signer_title, signer_contact, decline_reason,"
                            " version, content_hash, ip_address, created_at FROM signatures WHERE ticket_id = ?"
                            " ORDER BY id", (ticket_id,)).fetchall()
    notices = db.execute("SELECT * FROM notices WHERE ticket_id = ? ORDER BY id", (ticket_id,)).fetchall()
    links = db.execute("SELECT * FROM signature_requests WHERE ticket_id = ? ORDER BY id DESC", (ticket_id,)).fetchall()
    events = db.execute("SELECT * FROM audit_events WHERE ticket_id = ? AND company_id = ? ORDER BY id",
                        (ticket_id, g.company["id"])).fetchall()
    recipients, _ = notice_recipients(job["customer_id"], g.company["id"])
    return render_template(
        "tickets/detail.html", ticket=ticket, job=job, lines=lines, photos=get_photos(ticket_id),
        signatures=signatures, notices=notices, links=links, events=events, recipients=recipients,
        editable=ticket["status"] in EDITABLE, price_editable=ticket["status"] in PRICE_EDITABLE,
        can_notice=ticket["status"] in NOTICE_FROM, can_void=ticket["status"] in VOIDABLE,
        unpriced=has_unpriced_lines(lines), notice_reasons=NOTICE_REASONS,
        max_photos=MAX_PHOTOS_PER_TICKET, new_link=_pop_new_link(ticket_id))


def _pop_new_link(ticket_id):
    pending = session.pop("new_link", None)
    if pending and pending.get("ticket_id") == ticket_id:
        return pending["url"]
    return None


@bp.route("/tickets/<int:ticket_id>/pricing", methods=["POST"])
@roles_required("owner", "office")
def ticket_pricing(ticket_id):
    ticket = _ticket(ticket_id)
    form = Form(request.form)
    markup = form.decimal("markup_pct", "Markup %", min_value=0, max_value=100)
    rates = {}
    for line in get_lines(ticket_id):
        key = f"rate_{line['id']}"
        if key in request.form:
            value = form.decimal(key, f"Rate for '{line['description']}'", required=False, min_value=0,
                                 max_value=1000000)
            rates[line["id"]] = money_to_cents(value) if value is not None else None
    if not form.ok:
        flash(" ".join(form.errors.values()), "error")
        return redirect(url_for("tickets.ticket_detail", ticket_id=ticket_id))
    update_pricing(ticket, rates, int(markup * 100))
    flash("Pricing saved.", "success")
    return redirect(url_for("tickets.ticket_detail", ticket_id=ticket_id))


# --- photos -------------------------------------------------------------------

@bp.route("/tickets/<int:ticket_id>/photos", methods=["POST"])
@login_required
def upload_photo(ticket_id):
    ticket = _ticket(ticket_id)
    if ticket["status"] not in EDITABLE:
        abort(409, description="Photos can't be added after a ticket is signed or noticed.")
    files = [f for f in request.files.getlist("photos") if f and f.filename]
    if not files:
        flash("Choose a photo to upload.", "error")
        return redirect(url_for("tickets.ticket_detail", ticket_id=ticket_id))
    saved, errors = 0, []
    for f in files:
        error = store_photo(ticket, f)
        if error:
            errors.append(f"{f.filename}: {error}")
        else:
            saved += 1
    if saved:
        revoked = bump_version(ticket, "ticket.photos_added", {"count": saved})
        get_db().commit()
        msg = f"{saved} photo(s) added."
        if revoked:
            msg += " The earlier signature link was replaced; send a new one."
        flash(msg, "success")
    else:
        get_db().rollback()
    for e in errors:
        flash(e, "error")
    return redirect(url_for("tickets.ticket_detail", ticket_id=ticket_id))


@bp.route("/tickets/<int:ticket_id>/photos/<int:photo_id>/delete", methods=["POST"])
@login_required
def delete_photo(ticket_id, photo_id):
    ticket = _ticket(ticket_id)
    if ticket["status"] not in EDITABLE:
        abort(409, description="Photos can't be removed after a ticket is signed or noticed.")
    db = get_db()
    photo = db.execute("SELECT * FROM attachments WHERE id = ? AND ticket_id = ? AND company_id = ?",
                       (photo_id, ticket_id, g.company["id"])).fetchone()
    if photo is None:
        abort(404)
    db.execute("DELETE FROM attachments WHERE id = ?", (photo_id,))
    bump_version(ticket, "ticket.photo_removed", {"sha256": photo["sha256"]})
    db.commit()
    path = photo_path(photo)
    if path.exists():
        path.unlink()
    flash("Photo removed.", "success")
    return redirect(url_for("tickets.ticket_detail", ticket_id=ticket_id))


@bp.route("/files/<int:photo_id>")
@login_required
def photo(photo_id):
    row = get_db().execute("SELECT * FROM attachments WHERE id = ? AND company_id = ?",
                           (photo_id, g.company["id"])).fetchone()
    if row is None:
        abort(404)
    get_ticket(row["ticket_id"], g.company["id"], g.user)  # enforces foreman job access
    path = photo_path(row)
    if not path.exists():
        abort(404)
    return send_file(path, mimetype=row["content_type"], max_age=0)


@bp.route("/tickets/<int:ticket_id>/signatures/<int:sig_id>.png")
@login_required
def signature_image(ticket_id, sig_id):
    _ticket(ticket_id)
    row = get_db().execute("SELECT signature_png FROM signatures WHERE id = ? AND ticket_id = ? AND company_id = ?",
                           (sig_id, ticket_id, g.company["id"])).fetchone()
    if row is None or row["signature_png"] is None:
        abort(404)
    return Response(row["signature_png"], mimetype="image/png")


# --- signing --------------------------------------------------------------------

@bp.route("/tickets/<int:ticket_id>/sign-here", methods=["GET", "POST"])
@login_required
def sign_on_device(ticket_id):
    ticket = _ticket(ticket_id)
    if ticket["status"] not in EDITABLE:
        abort(409, description="This ticket isn't waiting for a signature.")
    lines = get_lines(ticket_id)
    if request.method == "POST":
        assert_hash_current(ticket)
        if request.form.get("content_hash") != ticket["content_hash"]:
            abort(409, description="The ticket changed while it was open. Reload and review it before signing.")
        form, png = parse_signature_form(request.form)
        if form.ok:
            record_signature(ticket, "on_device", "signed", form, png)
            flash("Signed. The ticket is now locked.", "success")
            return redirect(url_for("tickets.ticket_detail", ticket_id=ticket_id))
        status = 422
    else:
        form, status = Form({}), 200
    return render_template("sign/sign.html", ticket=ticket, lines=lines, photos=get_photos(ticket_id), form=form,
                           post_url=url_for("tickets.sign_on_device", ticket_id=ticket_id), decline_url=None,
                           photo_url=lambda p: url_for("tickets.photo", photo_id=p["id"]),
                           company=g.company), status


@bp.route("/tickets/<int:ticket_id>/sign-link", methods=["POST"])
@login_required
def sign_link(ticket_id):
    ticket = _ticket(ticket_id)
    raw = create_sign_link(ticket)
    # Kept in the session (not the URL) so the token doesn't end up in browser history.
    session["new_link"] = {"ticket_id": ticket_id, "url": shareable_url("public.sign", token=raw)}
    flash("Signature link created. Share it now: for security it is shown only once.", "success")
    return redirect(url_for("tickets.ticket_detail", ticket_id=ticket_id))


@bp.route("/tickets/<int:ticket_id>/notice", methods=["POST"])
@login_required
def create_notice(ticket_id):
    ticket = _ticket(ticket_id)
    reason = request.form.get("reason", "")
    if reason not in NOTICE_REASONS:
        abort(400, description="Choose why the ticket wasn't signed.")
    note = (request.form.get("note") or "").strip()[:500]
    job = get_job(ticket["job_id"], g.company["id"])
    notice_id = prepare_notice(ticket, g.company, job, reason, note)
    return redirect(url_for("tickets.notice_detail", ticket_id=ticket_id, notice_id=notice_id))


def _notice(ticket_id, notice_id):
    row = get_db().execute("SELECT * FROM notices WHERE id = ? AND ticket_id = ? AND company_id = ?",
                           (notice_id, ticket_id, g.company["id"])).fetchone()
    if row is None:
        abort(404)
    return row


@bp.route("/tickets/<int:ticket_id>/notices/<int:notice_id>")
@login_required
def notice_detail(ticket_id, notice_id):
    ticket = _ticket(ticket_id)
    notice = _notice(ticket_id, notice_id)
    job = get_job(ticket["job_id"], g.company["id"])
    _, emails = notice_recipients(job["customer_id"], g.company["id"])
    mailto = "mailto:" + quote(",".join(emails)) + "?subject=" + quote(notice["subject"]) + "&body=" + quote(notice["body"])
    return render_template("tickets/notice.html", ticket=ticket, notice=notice, mailto=mailto, emails=emails)


@bp.route("/tickets/<int:ticket_id>/notices/<int:notice_id>/sent", methods=["POST"])
@login_required
def notice_sent(ticket_id, notice_id):
    ticket = _ticket(ticket_id)
    notice = _notice(ticket_id, notice_id)
    method = request.form.get("method", "")
    if method not in ("email", "hand_delivered", "other"):
        abort(400, description="Choose how the notice was sent.")
    if notice["status"] == "sent":
        abort(409, description="This notice is already marked as sent.")
    db = get_db()
    db.execute("UPDATE notices SET status = 'sent', sent_method = ?, sent_at = ?, marked_by = ? WHERE id = ?",
               (method, now_iso(), g.user["id"], notice_id))
    if ticket["status"] == "notice_pending":
        set_status(ticket_id, g.company["id"], "noticed", lock=True)
    audit(g.company["id"], "notice.marked_sent", ticket_id, {"notice_id": notice_id, "method": method,
                                                             "content_hash": notice["content_hash"]})
    db.commit()
    flash("Notice recorded as sent. The ticket can now be invoiced.", "success")
    return redirect(url_for("tickets.ticket_detail", ticket_id=ticket_id))


@bp.route("/tickets/<int:ticket_id>/void", methods=["POST"])
@roles_required("owner", "office")
def void_ticket(ticket_id):
    ticket = _ticket(ticket_id)
    if ticket["status"] not in VOIDABLE:
        abort(409, description="Invoiced or paid tickets can't be voided.")
    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash("Give a reason for voiding the ticket.", "error")
        return redirect(url_for("tickets.ticket_detail", ticket_id=ticket_id))
    db = get_db()
    db.execute("UPDATE tickets SET void_reason = ? WHERE id = ?", (reason[:500], ticket_id))
    db.execute("UPDATE signature_requests SET revoked_at = ? WHERE ticket_id = ? AND used_at IS NULL"
               " AND revoked_at IS NULL", (now_iso(), ticket_id))
    set_status(ticket_id, g.company["id"], "void", lock=True)
    audit(g.company["id"], "ticket.voided", ticket_id, {"reason": reason[:500]})
    db.commit()
    flash("Ticket voided.", "success")
    return redirect(url_for("tickets.ticket_detail", ticket_id=ticket_id))


@bp.route("/tickets/<int:ticket_id>/print")
@login_required
def print_ticket(ticket_id):
    ticket = _ticket(ticket_id)
    job = get_job(ticket["job_id"], g.company["id"])
    signatures = get_db().execute("SELECT * FROM signatures WHERE ticket_id = ? ORDER BY id", (ticket_id,)).fetchall()
    return render_template("tickets/print.html", ticket=ticket, job=job, lines=get_lines(ticket_id),
                           photos=get_photos(ticket_id), signatures=signatures, company=g.company)
