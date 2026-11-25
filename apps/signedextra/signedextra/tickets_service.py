"""Ticket business rules: totals, versioning, locking, status changes, data access.

Every query here is scoped by company_id so one company can never read or
change another company's data.
"""
from decimal import ROUND_HALF_UP, Decimal

from flask import abort, g

from .db import get_db
from .security import audit
from .util import canonical_json, money_to_cents, now_iso, sha256_hex

EDITABLE = ("draft", "awaiting_signature", "declined")
INVOICEABLE = ("signed", "noticed")
PRICE_EDITABLE = ("draft", "awaiting_signature", "signed", "declined", "notice_pending", "noticed")
VOIDABLE = ("draft", "awaiting_signature", "signed", "declined", "notice_pending", "noticed")
NOTICE_FROM = ("draft", "awaiting_signature", "declined")

STATUS_LABELS = {
    "draft": "Draft",
    "awaiting_signature": "Awaiting signature",
    "signed": "Signed",
    "declined": "Declined by GC",
    "notice_pending": "Notice prepared",
    "noticed": "Notice sent",
    "invoiced": "Invoiced",
    "paid": "Paid",
    "disputed": "Disputed",
    "void": "Void",
}


# --- data access ------------------------------------------------------------

def get_job(job_id, company_id):
    job = get_db().execute(
        "SELECT j.*, c.name AS customer_name FROM jobs j JOIN customers c ON c.id = j.customer_id"
        " WHERE j.id = ? AND j.company_id = ?", (job_id, company_id)).fetchone()
    if job is None:
        abort(404)
    return job


def user_can_access_job(user, job_id):
    if user["role"] in ("owner", "office"):
        return True
    row = get_db().execute("SELECT 1 FROM job_members WHERE job_id = ? AND user_id = ?",
                           (job_id, user["id"])).fetchone()
    return row is not None


def get_ticket(ticket_id, company_id, user=None):
    ticket = get_db().execute(
        "SELECT t.*, j.name AS job_name, j.customer_id, c.name AS customer_name"
        " FROM tickets t JOIN jobs j ON j.id = t.job_id JOIN customers c ON c.id = j.customer_id"
        " WHERE t.id = ? AND t.company_id = ?", (ticket_id, company_id)).fetchone()
    if ticket is None:
        abort(404)
    if user is not None and not user_can_access_job(user, ticket["job_id"]):
        abort(404)
    return ticket


def get_lines(ticket_id):
    return get_db().execute(
        "SELECT l.*, r.label AS labor_label FROM ticket_lines l"
        " LEFT JOIN labor_rates r ON r.id = l.labor_rate_id"
        " WHERE l.ticket_id = ? ORDER BY l.position, l.id", (ticket_id,)).fetchall()


def labor_rates(company_id, active_only=True):
    sql = "SELECT * FROM labor_rates WHERE company_id = ?"
    if active_only:
        sql += " AND active = 1"
    return get_db().execute(sql + " ORDER BY label", (company_id,)).fetchall()


def effective_markup_bp(job, company):
    return job["markup_bp"] if job["markup_bp"] is not None else company["default_markup_bp"]


def effective_notice_hours(job, company):
    return job["notice_hours"] if job["notice_hours"] is not None else company["notice_hours"]


# --- totals -----------------------------------------------------------------

def line_amount_cents(qty_x100, rate_cents):
    if rate_cents is None:
        return 0
    amount = Decimal(qty_x100) / 100 * Decimal(rate_cents) / 100
    return money_to_cents(amount)


def compute_totals(lines, markup_bp):
    """Labor at its rate; materials/equipment plus markup. Returns a dict of cents."""
    labor = sum(l["amount_cents"] for l in lines if l["kind"] == "labor")
    material = sum(l["amount_cents"] for l in lines if l["kind"] != "labor")
    markup = int((Decimal(material) * Decimal(markup_bp) / 10000).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP))
    return {"labor_cents": labor, "material_cents": material, "markup_cents": markup,
            "total_cents": labor + material + markup}


def has_unpriced_lines(lines):
    return any(l["rate_cents"] is None for l in lines)


# --- content hash -----------------------------------------------------------

def get_photos(ticket_id):
    return get_db().execute("SELECT * FROM attachments WHERE ticket_id = ? ORDER BY id",
                            (ticket_id,)).fetchall()


def work_snapshot(ticket, lines, photos=None):
    """The work content a signer confirms: description, requester, quantities and
    photo fingerprints. Prices are excluded on purpose: the GC confirms the work;
    pricing follows the contract rates."""
    if photos is None:
        photos = get_photos(ticket["id"])
    return {
        "photos": sorted(p["sha256"] for p in photos),
        "number": ticket["number"],
        "job_id": ticket["job_id"],
        "description": ticket["description"],
        "requested_by_name": ticket["requested_by_name"],
        "requested_by_role": ticket["requested_by_role"],
        "work_date": ticket["work_date"],
        "lines": [
            {"kind": l["kind"], "labor_rate_id": l["labor_rate_id"],
             "description": l["description"], "qty_x100": l["qty_x100"], "unit": l["unit"]}
            for l in lines
        ],
    }


def content_hash(ticket, lines):
    return sha256_hex(canonical_json(work_snapshot(ticket, lines)))


# --- writes -----------------------------------------------------------------

def _resolve_lines(parsed_lines, company_id, include_prices):
    rates = {r["id"]: r for r in labor_rates(company_id, active_only=False)}
    resolved = []
    for pos, line in enumerate(parsed_lines):
        qty_x100 = int(line["qty"] * 100)
        if line["kind"] == "labor":
            rate = rates[line["labor_rate_id"]]
            rate_cents = rate["rate_cents"]
            description = line["description"] or rate["label"]
        else:
            rate_cents = money_to_cents(line["rate"]) if (include_prices and line["rate"] is not None) else None
            description = line["description"]
        resolved.append({
            "kind": line["kind"], "labor_rate_id": line["labor_rate_id"],
            "description": description, "qty_x100": qty_x100, "unit": line["unit"],
            "rate_cents": rate_cents, "amount_cents": line_amount_cents(qty_x100, rate_cents),
            "position": pos,
        })
    return resolved


def _write_lines(ticket_id, lines):
    db = get_db()
    db.execute("DELETE FROM ticket_lines WHERE ticket_id = ?", (ticket_id,))
    for l in lines:
        db.execute(
            "INSERT INTO ticket_lines (ticket_id, kind, labor_rate_id, description, qty_x100, unit,"
            " rate_cents, amount_cents, position) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ticket_id, l["kind"], l["labor_rate_id"], l["description"], l["qty_x100"], l["unit"],
             l["rate_cents"], l["amount_cents"], l["position"]))


def _save_version_and_totals(ticket_id, company_id):
    db = get_db()
    ticket = db.execute("SELECT * FROM tickets WHERE id = ? AND company_id = ?",
                        (ticket_id, company_id)).fetchone()
    lines = get_lines(ticket_id)
    totals = compute_totals(lines, ticket["markup_bp"])
    snapshot = work_snapshot(ticket, lines)
    digest = sha256_hex(canonical_json(snapshot))
    db.execute(
        "UPDATE tickets SET labor_cents = ?, material_cents = ?, markup_cents = ?, total_cents = ?,"
        " content_hash = ? WHERE id = ?",
        (totals["labor_cents"], totals["material_cents"], totals["markup_cents"],
         totals["total_cents"], digest, ticket_id))
    db.execute(
        "INSERT INTO ticket_versions (ticket_id, version, snapshot, content_hash, edited_by, edited_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (ticket_id, ticket["version"], canonical_json(snapshot), digest, g.user["id"], now_iso()))
    return digest


def create_ticket(company, job, fields, parsed_lines, include_prices):
    db = get_db()
    now = now_iso()
    number = company["next_ticket_no"]
    db.execute("UPDATE companies SET next_ticket_no = next_ticket_no + 1 WHERE id = ?", (company["id"],))
    cur = db.execute(
        "INSERT INTO tickets (company_id, job_id, number, description, requested_by_name,"
        " requested_by_role, work_date, markup_bp, created_by, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (company["id"], job["id"], number, fields["description"], fields["requested_by_name"],
         fields["requested_by_role"], fields["work_date"], effective_markup_bp(job, company),
         g.user["id"], now, now))
    ticket_id = cur.lastrowid
    _write_lines(ticket_id, _resolve_lines(parsed_lines, company["id"], include_prices))
    _save_version_and_totals(ticket_id, company["id"])
    audit(company["id"], "ticket.created", ticket_id, {"number": number})
    db.commit()
    return ticket_id


def update_ticket(ticket, fields, parsed_lines, include_prices):
    """Edit a ticket that is not yet signed. Creates a new version and revokes
    any outstanding signature links, because the signer would see old content."""
    if ticket["status"] not in EDITABLE:
        abort(409, description="This ticket is locked and can no longer be edited.")
    db = get_db()
    old_lines = get_lines(ticket["id"])
    resolved = _resolve_lines(parsed_lines, ticket["company_id"], include_prices)
    if not include_prices:
        # Keep prices the office already set on matching non-labor lines.
        old_prices = {(l["kind"], l["description"], l["unit"]): l["rate_cents"]
                      for l in old_lines if l["kind"] != "labor"}
        for l in resolved:
            if l["kind"] != "labor":
                l["rate_cents"] = old_prices.get((l["kind"], l["description"], l["unit"]))
                l["amount_cents"] = line_amount_cents(l["qty_x100"], l["rate_cents"])
    db.execute(
        "UPDATE tickets SET description = ?, requested_by_name = ?, requested_by_role = ?,"
        " work_date = ?, version = version + 1, updated_at = ?, status = 'draft'"
        " WHERE id = ? AND company_id = ?",
        (fields["description"], fields["requested_by_name"], fields["requested_by_role"],
         fields["work_date"], now_iso(), ticket["id"], ticket["company_id"]))
    _write_lines(ticket["id"], resolved)
    _save_version_and_totals(ticket["id"], ticket["company_id"])
    revoked = db.execute(
        "UPDATE signature_requests SET revoked_at = ? WHERE ticket_id = ? AND used_at IS NULL"
        " AND revoked_at IS NULL", (now_iso(), ticket["id"])).rowcount
    audit(ticket["company_id"], "ticket.edited", ticket["id"],
          {"version": ticket["version"] + 1, "signature_links_revoked": revoked})
    db.commit()


def bump_version(ticket, action, details=None):
    """Record a content change that isn't a form edit (e.g. a photo added or removed).
    Revokes outstanding signature links and returns the ticket to draft."""
    if ticket["status"] not in EDITABLE:
        abort(409, description="This ticket is locked and can no longer be changed.")
    db = get_db()
    db.execute("UPDATE tickets SET version = version + 1, updated_at = ?, status = 'draft'"
               " WHERE id = ? AND company_id = ?", (now_iso(), ticket["id"], ticket["company_id"]))
    _save_version_and_totals(ticket["id"], ticket["company_id"])
    revoked = db.execute(
        "UPDATE signature_requests SET revoked_at = ? WHERE ticket_id = ? AND used_at IS NULL"
        " AND revoked_at IS NULL", (now_iso(), ticket["id"])).rowcount
    audit(ticket["company_id"], action, ticket["id"],
          dict(details or {}, version=ticket["version"] + 1, signature_links_revoked=revoked))
    return revoked


def update_pricing(ticket, rates_by_line_id, markup_bp):
    if ticket["status"] not in PRICE_EDITABLE:
        abort(409, description="Pricing can't change after the ticket is invoiced or voided.")
    db = get_db()
    changes = {}
    for line in get_lines(ticket["id"]):
        if line["id"] in rates_by_line_id:
            new_rate = rates_by_line_id[line["id"]]
            if new_rate != line["rate_cents"]:
                changes[str(line["id"])] = {"from": line["rate_cents"], "to": new_rate}
            db.execute("UPDATE ticket_lines SET rate_cents = ?, amount_cents = ? WHERE id = ?",
                       (new_rate, line_amount_cents(line["qty_x100"], new_rate), line["id"]))
    if markup_bp != ticket["markup_bp"]:
        changes["markup_bp"] = {"from": ticket["markup_bp"], "to": markup_bp}
    db.execute("UPDATE tickets SET markup_bp = ?, updated_at = ? WHERE id = ?",
               (markup_bp, now_iso(), ticket["id"]))
    totals = compute_totals(get_lines(ticket["id"]), markup_bp)
    db.execute("UPDATE tickets SET labor_cents = ?, material_cents = ?, markup_cents = ?,"
               " total_cents = ? WHERE id = ?",
               (totals["labor_cents"], totals["material_cents"], totals["markup_cents"],
                totals["total_cents"], ticket["id"]))
    audit(ticket["company_id"], "ticket.priced", ticket["id"], changes)
    db.commit()


def set_status(ticket_id, company_id, status, lock=False):
    db = get_db()
    if lock:
        db.execute("UPDATE tickets SET status = ?, updated_at = ?, locked_at = COALESCE(locked_at, ?)"
                   " WHERE id = ? AND company_id = ?",
                   (status, now_iso(), now_iso(), ticket_id, company_id))
    else:
        db.execute("UPDATE tickets SET status = ?, updated_at = ? WHERE id = ? AND company_id = ?",
                   (status, now_iso(), ticket_id, company_id))


def status_totals(company_id, job_ids=None):
    sql = "SELECT status, COUNT(*) AS n, COALESCE(SUM(total_cents), 0) AS cents FROM tickets WHERE company_id = ?"
    params = [company_id]
    if job_ids is not None:
        if not job_ids:
            return {}
        sql += f" AND job_id IN ({','.join('?' * len(job_ids))})"
        params += list(job_ids)
    rows = get_db().execute(sql + " GROUP BY status", params).fetchall()
    return {r["status"]: {"n": r["n"], "cents": r["cents"]} for r in rows}
