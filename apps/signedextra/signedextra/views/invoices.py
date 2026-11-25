"""Invoices built from signed or noticed tickets on one job."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from ..db import get_db
from ..security import audit, roles_required
from ..tickets_service import INVOICEABLE, get_job, get_lines, has_unpriced_lines, set_status
from ..util import now_iso

bp = Blueprint("invoices", __name__)
MANAGERS = ("owner", "office")

# Allowed invoice status changes and the ticket status each one implies.
TRANSITIONS = {
    "open": {"sent": "invoiced", "paid": "paid", "disputed": "disputed"},
    "sent": {"paid": "paid", "disputed": "disputed"},
    "disputed": {"paid": "paid", "sent": "invoiced"},
    "paid": {},
}


def _invoice(invoice_id):
    row = get_db().execute(
        "SELECT i.*, j.name AS job_name, j.address AS job_address, j.gc_reference, c.name AS customer_name,"
        " c.address AS customer_address FROM invoices i JOIN jobs j ON j.id = i.job_id"
        " JOIN customers c ON c.id = j.customer_id WHERE i.id = ? AND i.company_id = ?",
        (invoice_id, g.company["id"])).fetchone()
    if row is None:
        abort(404)
    return row


@bp.route("/invoices")
@roles_required(*MANAGERS)
def invoice_list():
    rows = get_db().execute(
        "SELECT i.*, j.name AS job_name, c.name AS customer_name FROM invoices i JOIN jobs j ON j.id = i.job_id"
        " JOIN customers c ON c.id = j.customer_id WHERE i.company_id = ? ORDER BY i.number DESC",
        (g.company["id"],)).fetchall()
    return render_template("invoices/list.html", invoices=rows)


@bp.route("/jobs/<int:job_id>/invoices/new", methods=["GET", "POST"])
@roles_required(*MANAGERS)
def new_invoice(job_id):
    job = get_job(job_id, g.company["id"])
    db = get_db()
    candidates = db.execute(
        f"SELECT * FROM tickets WHERE job_id = ? AND company_id = ? AND status IN ({','.join('?' * len(INVOICEABLE))})"
        " ORDER BY number", (job_id, g.company["id"], *INVOICEABLE)).fetchall()
    unpriced = {t["id"] for t in candidates if has_unpriced_lines(get_lines(t["id"]))}
    error = None
    if request.method == "POST":
        chosen_ids = {int(v) for v in request.form.getlist("ticket_id") if v.isdigit()}
        chosen = [t for t in candidates if t["id"] in chosen_ids]
        notes = (request.form.get("notes") or "").strip()[:2000]
        if not chosen or len(chosen) != len(chosen_ids):
            error = "Select at least one signed or noticed ticket from this job."
        elif any(t["id"] in unpriced for t in chosen):
            error = "Some selected tickets have unpriced lines. Price them on the ticket page first."
        else:
            number = g.company["next_invoice_no"]
            total = sum(t["total_cents"] for t in chosen)
            db.execute("UPDATE companies SET next_invoice_no = next_invoice_no + 1 WHERE id = ?", (g.company["id"],))
            invoice_id = db.execute(
                "INSERT INTO invoices (company_id, job_id, number, total_cents, notes, created_by, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (g.company["id"], job_id, number, total, notes, g.user["id"], now_iso())).lastrowid
            for t in chosen:
                db.execute("UPDATE tickets SET invoice_id = ? WHERE id = ?", (invoice_id, t["id"]))
                set_status(t["id"], g.company["id"], "invoiced", lock=True)
                audit(g.company["id"], "ticket.invoiced", t["id"], {"invoice_number": number})
            audit(g.company["id"], "invoice.created", details={"invoice_id": invoice_id, "total_cents": total})
            db.commit()
            flash(f"Invoice INV-{number:04d} created.", "success")
            return redirect(url_for("invoices.invoice_detail", invoice_id=invoice_id))
    return render_template("invoices/new.html", job=job, candidates=candidates, unpriced=unpriced,
                           error=error), (422 if error else 200)


@bp.route("/invoices/<int:invoice_id>")
@roles_required(*MANAGERS)
def invoice_detail(invoice_id):
    invoice = _invoice(invoice_id)
    tickets = get_db().execute("SELECT * FROM tickets WHERE invoice_id = ? AND company_id = ? ORDER BY number",
                               (invoice_id, g.company["id"])).fetchall()
    ticket_lines = {t["id"]: get_lines(t["id"]) for t in tickets}
    return render_template("invoices/detail.html", invoice=invoice, tickets=tickets, ticket_lines=ticket_lines,
                           company=g.company, transitions=TRANSITIONS[invoice["status"]])


@bp.route("/invoices/<int:invoice_id>/status", methods=["POST"])
@roles_required(*MANAGERS)
def invoice_status(invoice_id):
    invoice = _invoice(invoice_id)
    new_status = request.form.get("status", "")
    allowed = TRANSITIONS[invoice["status"]]
    if new_status not in allowed:
        abort(409, description="That status change isn't allowed.")
    db = get_db()
    now = now_iso()
    db.execute("UPDATE invoices SET status = ?, sent_at = CASE WHEN ? = 'sent' THEN ? ELSE sent_at END,"
               " paid_at = CASE WHEN ? = 'paid' THEN ? ELSE paid_at END WHERE id = ?",
               (new_status, new_status, now, new_status, now, invoice_id))
    for t in db.execute("SELECT id FROM tickets WHERE invoice_id = ? AND company_id = ?",
                        (invoice_id, g.company["id"])).fetchall():
        set_status(t["id"], g.company["id"], allowed[new_status])
        audit(g.company["id"], f"ticket.{allowed[new_status]}", t["id"], {"invoice_id": invoice_id})
    audit(g.company["id"], f"invoice.{new_status}", details={"invoice_id": invoice_id})
    db.commit()
    flash("Invoice updated.", "success")
    return redirect(url_for("invoices.invoice_detail", invoice_id=invoice_id))
