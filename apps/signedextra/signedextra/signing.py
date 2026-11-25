"""Signature capture, signature links, written notices and photo storage."""
import base64
import binascii
import secrets
from pathlib import Path

from flask import abort, current_app, g, request

from .db import get_db
from .security import audit, hash_token, new_token
from .tickets_service import (EDITABLE, NOTICE_FROM, STATUS_LABELS, content_hash,
                              effective_notice_hours, get_lines, get_photos, set_status)
from .util import canonical_json, iso_in, now_iso, parse_iso, sha256_hex, utcnow, x100_to_str
from .validation import Form

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_SIGNATURE_BYTES = 300 * 1024
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_PHOTOS_PER_TICKET = 20


# --- signature capture -------------------------------------------------------

def parse_signature_form(data):
    """Validate a signing submission. Returns (form, png_bytes)."""
    form = Form(data)
    form.text("signer_name", "Your full name", max_len=120)
    form.text("signer_title", "Your title", max_len=120)
    form.text("signer_contact", "Email or phone", required=False, max_len=120)
    if not form.checkbox("consent"):
        form.errors["consent"] = "Please confirm you agree to sign electronically."
    png = decode_signature(data.get("signature_data") or "")
    if png is None:
        form.errors["signature_data"] = "Please draw your signature in the box."
    return form, png


def decode_signature(data_url):
    prefix = "data:image/png;base64,"
    if not data_url.startswith(prefix):
        return None
    try:
        raw = base64.b64decode(data_url[len(prefix):], validate=True)
    except (binascii.Error, ValueError):
        return None
    if not raw.startswith(PNG_MAGIC) or len(raw) > MAX_SIGNATURE_BYTES or len(raw) < 100:
        return None
    return raw


def record_signature(ticket, method, outcome, form, png, request_row=None):
    db = get_db()
    now = now_iso()
    # Claim the ticket (and the link) with conditional updates first, so two
    # simultaneous submissions can't both record a signature.
    allowed = ("awaiting_signature",) if request_row else EDITABLE
    new_status = "signed" if outcome == "signed" else "declined"
    claimed = db.execute(
        f"UPDATE tickets SET status = ?, updated_at = ?,"
        f" locked_at = CASE WHEN ? = 'signed' THEN COALESCE(locked_at, ?) ELSE locked_at END"
        f" WHERE id = ? AND company_id = ? AND content_hash = ? AND status IN ({','.join('?' * len(allowed))})",
        (new_status, now, new_status, now, ticket["id"], ticket["company_id"], ticket["content_hash"], *allowed),
    ).rowcount
    if claimed and request_row:
        claimed = db.execute(
            "UPDATE signature_requests SET used_at = ? WHERE id = ? AND used_at IS NULL AND revoked_at IS NULL",
            (now, request_row["id"])).rowcount
    if not claimed:
        db.rollback()
        abort(409, description="This ticket was already answered or changed. Reload the page to see its status.")
    db.execute(
        "INSERT INTO signatures (company_id, ticket_id, request_id, method, outcome, signer_name, signer_title,"
        " signer_contact, decline_reason, signature_png, version, content_hash, ip_address, user_agent, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ticket["company_id"], ticket["id"], request_row["id"] if request_row else None, method, outcome,
         form.values.get("signer_name", ""), form.values.get("signer_title", ""),
         form.values.get("signer_contact", ""), form.values.get("decline_reason", ""), png,
         ticket["version"], ticket["content_hash"], request.remote_addr or "",
         (request.headers.get("User-Agent") or "")[:300], now_iso()))
    # Any other open links for this ticket are no longer needed.
    db.execute("UPDATE signature_requests SET revoked_at = ? WHERE ticket_id = ? AND used_at IS NULL"
               " AND revoked_at IS NULL", (now, ticket["id"]))
    actor = f"{form.values.get('signer_name', '')} ({form.values.get('signer_title', '')}) - GC signer"
    audit(ticket["company_id"], f"ticket.{outcome}", ticket["id"],
          {"method": method, "version": ticket["version"], "content_hash": ticket["content_hash"]},
          actor=actor, user_id=g.user["id"] if g.get("user") is not None else None)
    db.commit()


def assert_hash_current(ticket):
    """Refuse to sign if stored content no longer matches its fingerprint."""
    if content_hash(ticket, get_lines(ticket["id"])) != ticket["content_hash"]:
        abort(409, description="This ticket's content changed. Reload and review it before signing.")


# --- signature links ---------------------------------------------------------

def create_sign_link(ticket):
    if ticket["status"] not in EDITABLE:
        abort(409, description="Only unsigned tickets can be sent for signature.")
    raw, token_hash = new_token()
    db = get_db()
    db.execute(
        "INSERT INTO signature_requests (company_id, ticket_id, token_hash, version, content_hash, created_by,"
        " created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ticket["company_id"], ticket["id"], token_hash, ticket["version"], ticket["content_hash"], g.user["id"],
         now_iso(), iso_in(days=current_app.config["SIGN_LINK_DAYS"])))
    set_status(ticket["id"], ticket["company_id"], "awaiting_signature")
    audit(ticket["company_id"], "ticket.sign_link_created", ticket["id"], {"version": ticket["version"]})
    db.commit()
    return raw


def load_sign_request(raw_token):
    """Return (request_row, ticket) for a usable link, or abort with a clear message."""
    db = get_db()
    req = db.execute("SELECT * FROM signature_requests WHERE token_hash = ?", (hash_token(raw_token),)).fetchone()
    if req is None:
        abort(404, description="This signing link is not valid.")
    if req["used_at"]:
        abort(404, description="This ticket has already been answered through this link.")
    if req["revoked_at"]:
        abort(404, description="This link was replaced because the ticket changed. Ask the contractor for a new link.")
    if parse_iso(req["expires_at"]) < utcnow():
        abort(404, description="This signing link has expired. Ask the contractor for a new link.")
    ticket = db.execute(
        "SELECT t.*, j.name AS job_name, j.address AS job_address, j.gc_reference, c.name AS customer_name,"
        " co.name AS company_name, co.phone AS company_phone, co.email AS company_email"
        " FROM tickets t JOIN jobs j ON j.id = t.job_id JOIN customers c ON c.id = j.customer_id"
        " JOIN companies co ON co.id = t.company_id WHERE t.id = ? AND t.company_id = ?",
        (req["ticket_id"], req["company_id"])).fetchone()
    if ticket is None or ticket["status"] != "awaiting_signature" or ticket["content_hash"] != req["content_hash"]:
        abort(404, description="This ticket is no longer waiting for a signature.")
    return req, ticket


# --- written notices ---------------------------------------------------------

NOTICE_REASONS = {
    "refused": "The GC's representative refused to sign on site.",
    "no_response": "No response was received to the signature request.",
    "declined": "The GC's representative declined the signature request.",
}


def notice_recipients(customer_id, company_id):
    rows = get_db().execute(
        "SELECT name, email FROM customer_contacts WHERE customer_id = ? AND company_id = ?"
        " AND receives_notices = 1 AND email != '' ORDER BY name", (customer_id, company_id)).fetchall()
    return [f"{r['name']} <{r['email']}>" for r in rows], [r["email"] for r in rows]


def build_notice(ticket, company, job, reason, extra_note=""):
    lines = get_lines(ticket["id"])
    photos = get_photos(ticket["id"])
    work = []
    for l in lines:
        label = l["labor_label"] if l["kind"] == "labor" else l["kind"].title()
        work.append(f"  - {label}: {l['description']}, {x100_to_str(l['qty_x100'])} {l['unit']}".rstrip())
    subject = f"Notice of extra work - {company['name']} - Ticket T-{ticket['number']:04d} - {job['name']}"
    hours = effective_notice_hours(job, company)
    parts = [
        f"To: {job['customer_name']}",
        f"Job: {job['name']}" + (f" (GC ref {job['gc_reference']})" if job["gc_reference"] else ""),
        "",
        f"This is written notice from {company['name']} about work performed on {ticket['work_date']}.",
        f"Requested by: {ticket['requested_by_name']}"
        + (f" ({ticket['requested_by_role']})" if ticket["requested_by_role"] else ""),
        "",
        "Description of the work:",
        ticket["description"],
        "",
        "Labor and materials recorded:",
        *(work or ["  (none recorded)"]),
        "",
        f"Photos on file: {len(photos)}",
        f"Signature status: {NOTICE_REASONS[reason]}",
    ]
    if extra_note:
        parts += ["", f"Note: {extra_note}"]
    parts += [
        "",
        "We consider this work to be outside our contract scope and are requesting a signed change order.",
        f"Please reply within {hours} hours to confirm, or tell us in writing if you disagree.",
        "",
        f"Ticket fingerprint (SHA-256 of the recorded work): {ticket['content_hash']}",
        "",
        company["name"],
        " ".join(x for x in (company["phone"], company["email"]) if x),
    ]
    return subject, "\n".join(parts)


def prepare_notice(ticket, company, job, reason, extra_note=""):
    if ticket["status"] not in NOTICE_FROM:
        abort(409, description="A notice can only be prepared for an unsigned or declined ticket.")
    display, _emails = notice_recipients(job["customer_id"], company["id"])
    subject, body = build_notice(ticket, company, job, reason, extra_note)
    db = get_db()
    db.execute("UPDATE signature_requests SET revoked_at = ? WHERE ticket_id = ? AND used_at IS NULL"
               " AND revoked_at IS NULL", (now_iso(), ticket["id"]))
    notice_id = db.execute(
        "INSERT INTO notices (company_id, ticket_id, reason, recipients, subject, body, content_hash, created_by,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (company["id"], ticket["id"], reason, ", ".join(display), subject, body,
         sha256_hex(canonical_json({"subject": subject, "body": body})), g.user["id"], now_iso())).lastrowid
    set_status(ticket["id"], company["id"], "notice_pending", lock=True)
    audit(company["id"], "notice.prepared", ticket["id"], {"notice_id": notice_id, "reason": reason})
    db.commit()
    return notice_id


# --- photos ------------------------------------------------------------------

def sniff_image(head):
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if head.startswith(PNG_MAGIC):
        return "image/png", ".png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None, None


def store_photo(ticket, file_storage):
    """Validate and save one uploaded photo. Returns an error message or None."""
    if file_storage is None or not file_storage.filename:
        return "Choose a photo to upload."
    count = get_db().execute("SELECT COUNT(*) FROM attachments WHERE ticket_id = ?", (ticket["id"],)).fetchone()[0]
    if count >= MAX_PHOTOS_PER_TICKET:
        return f"A ticket can have at most {MAX_PHOTOS_PER_TICKET} photos."
    data = file_storage.read(MAX_PHOTO_BYTES + 1)
    if len(data) > MAX_PHOTO_BYTES:
        return "Photos must be 10 MB or smaller."
    content_type, ext = sniff_image(data[:16])
    if content_type is None:
        return "Only JPEG, PNG or WebP photos are accepted."
    stored = secrets.token_hex(16) + ext
    folder = Path(current_app.config["UPLOAD_DIR"]) / str(ticket["company_id"])
    folder.mkdir(parents=True, exist_ok=True)
    (folder / stored).write_bytes(data)
    get_db().execute(
        "INSERT INTO attachments (company_id, ticket_id, stored_name, content_type, size_bytes, sha256,"
        " uploaded_by, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ticket["company_id"], ticket["id"], stored, content_type, len(data), sha256_hex(data),
         g.user["id"], now_iso()))
    return None


def photo_path(attachment):
    return Path(current_app.config["UPLOAD_DIR"]) / str(attachment["company_id"]) / attachment["stored_name"]


def status_label(status):
    return STATUS_LABELS.get(status, status)
