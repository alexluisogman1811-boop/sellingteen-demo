from datetime import timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

from . import followup_ai
from .db import get_db
from .util import (
    CURRENCIES, DUE_SOON_DAYS, MAX_AMOUNT_CENTS, STATUS_LABELS, ZERO_DECIMAL_CURRENCIES, amount_input, due_relative, days_overdue, format_money, invoice_status, like_pattern,
    limiter, local_now_iso, login_required, now_iso, parse_amount, parse_date, today,
)

bp = Blueprint("invoices", __name__, url_prefix="/invoices")
PER_PAGE = 50

INVOICE_SELECT = """
    SELECT i.*, c.name AS customer_name, c.contact_name, c.email AS customer_email, c.phone AS customer_phone,
           f.last_followup_at, COALESCE(f.followup_count, 0) AS followup_count
    FROM invoices i
    JOIN customers c ON c.id = i.customer_id AND c.user_id = i.user_id
    LEFT JOIN (
        SELECT invoice_id, MAX(created_at) AS last_followup_at, COUNT(*) AS followup_count
        FROM followups WHERE user_id = :uid GROUP BY invoice_id
    ) f ON f.invoice_id = i.id
    WHERE i.user_id = :uid
"""


def status_clause(status):
    """SQL fragment matching util.invoice_status() for a filter value (same `today`)."""
    t = today()
    params = {"t": t.isoformat(), "soon": (t + timedelta(days=DUE_SOON_DAYS)).isoformat()}
    clause = {
        "paid": "i.paid_at IS NOT NULL",
        "unpaid": "i.paid_at IS NULL",
        "overdue": "i.paid_at IS NULL AND i.due_date < :t",
        "due_soon": "i.paid_at IS NULL AND i.due_date >= :t AND i.due_date <= :soon",
        "open": "i.paid_at IS NULL AND i.due_date > :soon",
    }.get(status)
    return (clause, params) if clause else None


def _filters(status=None, q="", customer_id=None):
    sql, params = "", {"uid": g.user["id"]}
    clause = status_clause(status)
    if clause:
        sql += " AND " + clause[0]
        params.update(clause[1])
    if q:
        sql += " AND (i.number LIKE :q ESCAPE '\\' OR c.name LIKE :q ESCAPE '\\')"
        params["q"] = like_pattern(q)
    if customer_id:
        sql += " AND i.customer_id = :cid"
        params["cid"] = customer_id
    return sql, params


def query_invoices(status=None, q="", customer_id=None, limit=None, offset=0, order="due"):
    where, params = _filters(status, q, customer_id)
    order_by = {
        "due": "(i.paid_at IS NOT NULL), i.due_date, i.id",
        "recent": "i.created_at DESC, i.id DESC",
    }[order]
    sql = INVOICE_SELECT + where + f" ORDER BY {order_by}"
    if limit:
        sql += " LIMIT :limit OFFSET :offset"
        params.update(limit=int(limit), offset=int(offset))
    return [decorate(r) for r in get_db().execute(sql, params).fetchall()]


def count_invoices(status=None, q="", customer_id=None):
    where, params = _filters(status, q, customer_id)
    sql = "SELECT COUNT(*) FROM invoices i JOIN customers c ON c.id = i.customer_id WHERE i.user_id = :uid" + where
    return get_db().execute(sql, params).fetchone()[0]


def decorate(row):
    inv = dict(row)
    inv["status"] = invoice_status(inv)
    inv["days_overdue"] = days_overdue(inv) if inv["status"] == "overdue" else 0
    return inv


def get_invoice(invoice_id):
    row = get_db().execute(INVOICE_SELECT + " AND i.id = :iid", {"uid": g.user["id"], "iid": invoice_id}).fetchone()
    if row is None:
        abort(404)
    return decorate(row)


def user_customers():
    return get_db().execute(
        "SELECT id, name FROM customers WHERE user_id = ? ORDER BY name COLLATE NOCASE", (g.user["id"],)
    ).fetchall()


def next_invoice_number():
    rows = get_db().execute("SELECT number FROM invoices WHERE user_id = ?", (g.user["id"],)).fetchall()
    nums = [int(r["number"][4:]) for r in rows if r["number"].upper().startswith("INV-") and r["number"][4:].isdigit()]
    return f"INV-{(max(nums) + 1 if nums else 1):04d}"


def amount_error(raw, currency):
    """Explain why an amount was rejected, in the owner's terms."""
    whole = currency in ZERO_DECIMAL_CURRENCIES
    cleaned = raw.replace(",", "").replace(CURRENCIES.get(currency, ""), "").strip()
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        value = None
    if value is not None and value.is_finite():
        if value > MAX_AMOUNT_CENTS / 100:
            return "That amount is too large. Please check for extra digits."
        if value > 0 and value.as_tuple().exponent < (0 if whole else -2):
            return f"{currency} amounts can't have decimals." if whole else "Use at most 2 decimal places, like 1250.50."
    return "Enter a whole amount greater than 0, like 1500." if whole else "Enter an amount greater than 0, like 1250 or 1250.50."


def validate(form, invoice_id=None):
    currency = g.user["currency"]
    data = {
        "number": " ".join(form.get("number", "").split()),
        "customer_id": form.get("customer_id", "").strip(),
        "amount": form.get("amount", "").strip(),
        "issue_date": form.get("issue_date", "").strip(),
        "due_date": form.get("due_date", "").strip(),
        "notes": form.get("notes", "").strip(),
    }
    errors = {}
    if not data["number"] or len(data["number"]) > 40:
        errors["number"] = "Invoice number is required (max 40 characters)."
    elif get_db().execute(
        "SELECT 1 FROM invoices WHERE user_id = ? AND number = ? COLLATE NOCASE AND id != ?",
        (g.user["id"], data["number"], invoice_id or 0),
    ).fetchone():
        errors["number"] = "You already have an invoice with this number."
    cid = int(data["customer_id"]) if data["customer_id"].isdigit() else None
    if cid is None or not get_db().execute(
        "SELECT 1 FROM customers WHERE id = ? AND user_id = ?", (cid, g.user["id"])
    ).fetchone():
        errors["customer_id"] = "Choose a customer."
    data["customer_id"] = cid
    data["amount_cents"] = parse_amount(data["amount"], currency)
    if data["amount_cents"] is None:
        errors["amount"] = amount_error(data["amount"], currency)
    issue, due = parse_date(data["issue_date"]), parse_date(data["due_date"])
    if not issue:
        errors["issue_date"] = "Enter a valid issue date (between 2000 and 2100)."
    if not due:
        errors["due_date"] = "Enter a valid due date (between 2000 and 2100)."
    elif issue and due < issue:
        errors["due_date"] = "The due date can't be before the issue date."
    # Store canonical YYYY-MM-DD: fromisoformat also accepts forms like "20261027"
    # that would break the string comparisons used for status filtering.
    if issue:
        data["issue_date"] = issue.isoformat()
    if due:
        data["due_date"] = due.isoformat()
    if len(data["notes"]) > 1000:
        errors["notes"] = "Notes are limited to 1000 characters."
    return data, errors


@bp.get("/")
@login_required
def index():
    status = request.args.get("status", "")
    if status not in ("", "unpaid", *STATUS_LABELS):
        status = ""
    q = request.args.get("q", "").strip()[:100]
    customer_id = request.args.get("customer", type=int)
    page = max(1, request.args.get("page", 1, type=int) or 1)
    total = count_invoices(status or None, q, customer_id)
    invoices = query_invoices(status or None, q, customer_id, limit=PER_PAGE, offset=(page - 1) * PER_PAGE)
    has_any = total > 0 or get_db().execute(
        "SELECT 1 FROM invoices WHERE user_id = ? LIMIT 1", (g.user["id"],)
    ).fetchone() is not None
    return render_template(
        "invoices/index.html", invoices=invoices, status=status, q=q, customer_id=customer_id,
        customers=user_customers(), has_any=has_any, total=total, page=page,
        pages=max(1, -(-total // PER_PAGE)),
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    customers = user_customers()
    if not customers:
        flash("First, add the customer you're billing. It only takes a name.", "info")
        return redirect(url_for("customers.new", then="invoice"))
    t = today()
    data = {
        "number": next_invoice_number(),
        "customer_id": request.args.get("customer_id", type=int),
        "issue_date": t.isoformat(),
        "due_date": (t + timedelta(days=30)).isoformat(),
    }
    errors = {}
    if request.method == "POST":
        data, errors = validate(request.form)
        if not errors:
            db = get_db()
            cur = db.execute(
                "INSERT INTO invoices (user_id, customer_id, number, amount_cents, issue_date, due_date, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (g.user["id"], data["customer_id"], data["number"], data["amount_cents"],
                 data["issue_date"], data["due_date"], data["notes"]),
            )
            db.commit()
            saved = {"paid_at": None, "due_date": data["due_date"]}
            status = invoice_status(saved)
            if status == "overdue":
                note = f"It's already {due_relative(saved)}, so you can write a follow-up right away below."
            elif status == "due_soon":
                note = f"It's {due_relative(saved).lower()}. You can send a friendly reminder below."
            else:
                note = "We'll flag it when it's due soon or overdue."
            flash(f"Invoice {data['number']} saved. {note}", "success")
            return redirect(url_for("invoices.detail", invoice_id=cur.lastrowid))
    return render_template("invoices/form.html", data=data, errors=errors, customers=customers, invoice=None), (400 if errors else 200)


@bp.route("/<int:invoice_id>/edit", methods=["GET", "POST"])
@login_required
def edit(invoice_id):
    invoice = get_invoice(invoice_id)
    data = dict(invoice, amount=amount_input(invoice["amount_cents"], g.user["currency"]))
    errors = {}
    if request.method == "POST":
        data, errors = validate(request.form, invoice_id)
        if not errors:
            db = get_db()
            with db:
                db.execute(
                    "UPDATE invoices SET customer_id = ?, number = ?, amount_cents = ?, issue_date = ?, due_date = ?, "
                    "notes = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                    (data["customer_id"], data["number"], data["amount_cents"], data["issue_date"],
                     data["due_date"], data["notes"], now_iso(), invoice_id, g.user["id"]),
                )
                # A saved draft quotes the old facts; drop it so it can't be sent by mistake.
                db.execute("DELETE FROM drafts WHERE invoice_id = ? AND user_id = ?", (invoice_id, g.user["id"]))
            flash("Invoice updated.", "success")
            return redirect(url_for("invoices.detail", invoice_id=invoice_id))
    return render_template("invoices/form.html", data=data, errors=errors, customers=user_customers(), invoice=invoice), (400 if errors else 200)


@bp.post("/<int:invoice_id>/delete")
@login_required
def delete(invoice_id):
    invoice = get_invoice(invoice_id)
    db = get_db()
    db.execute("DELETE FROM invoices WHERE id = ? AND user_id = ?", (invoice_id, g.user["id"]))
    db.commit()
    flash(f"Invoice {invoice['number']} and its follow-up history were deleted.", "success")
    return redirect(url_for("invoices.index"))


@bp.post("/<int:invoice_id>/paid")
@login_required
def mark_paid(invoice_id):
    invoice = get_invoice(invoice_id)
    paid = request.form.get("paid") == "1"
    paid_on = parse_date(request.form.get("paid_on")) or today()
    paid_on = min(paid_on, today())  # can't be paid in the future
    db = get_db()
    with db:
        db.execute(
            "UPDATE invoices SET paid_at = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (paid_on.isoformat() if paid else None, now_iso(), invoice_id, g.user["id"]),
        )
        if paid:
            db.execute("DELETE FROM drafts WHERE invoice_id = ? AND user_id = ?", (invoice_id, g.user["id"]))
    if paid:
        flash(f"Invoice {invoice['number']} marked as paid. Nice, that's {format_money(invoice['amount_cents'], g.user['currency'])} collected.", "success")
    else:
        flash(f"Invoice {invoice['number']} marked as unpaid again.", "info")
    return redirect(request.form.get("return_to") == "dashboard" and url_for("dashboard.index")
                    or url_for("invoices.detail", invoice_id=invoice_id))


def get_draft(invoice_id):
    return get_db().execute(
        "SELECT * FROM drafts WHERE invoice_id = ? AND user_id = ?", (invoice_id, g.user["id"])
    ).fetchone()


@bp.get("/<int:invoice_id>")
@login_required
def detail(invoice_id):
    invoice = get_invoice(invoice_id)
    history = get_db().execute(
        "SELECT * FROM followups WHERE invoice_id = ? AND user_id = ? ORDER BY created_at DESC, id DESC",
        (invoice_id, g.user["id"]),
    ).fetchall()
    draft = get_draft(invoice_id) if invoice["status"] != "paid" else None
    expired_draft_date = None
    if draft and draft["written_for"] != today().isoformat():
        # The text says things like "12 days past due"; a day later that's wrong. Never show it.
        expired_draft_date = draft["written_for"] or draft["created_at"][:10]
        db = get_db()
        db.execute("DELETE FROM drafts WHERE invoice_id = ? AND user_id = ?", (invoice_id, g.user["id"]))
        db.commit()
        draft = None
    mailto = None
    if draft and draft["channel"] == "email" and invoice["customer_email"]:
        subject = followup_ai.subject_for(invoice, g.user, draft["tone"])
        mailto = (f"mailto:{quote(invoice['customer_email'], safe='@')}"
                  f"?subject={quote(subject)}&body={quote(draft['message'])}")
    return render_template(
        "invoices/detail.html", invoice=invoice, history=history, draft=draft, mailto=mailto,
        expired_draft_date=expired_draft_date,
        tones={k: followup_ai.TONES[k] for k in followup_ai.allowed_tones(invoice)},
        channels=followup_ai.CHANNELS, today_iso=today().isoformat(),
        suggested_tone=followup_ai.suggested_tone(invoice), ai_enabled=followup_ai.ai_enabled(),
        tone_label=followup_ai.tone_label, channel_label=followup_ai.channel_label, sources=followup_ai.SOURCES,
    )


@bp.post("/<int:invoice_id>/followup/generate")
@login_required
def generate_followup(invoice_id):
    invoice = get_invoice(invoice_id)
    if invoice["status"] == "paid":
        flash("This invoice is already paid, so it doesn't need a follow-up.", "info")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))
    allow_ai = True
    if followup_ai.ai_enabled():
        ai_limit = limiter("ai", current_app.config["AI_GENERATIONS_PER_HOUR"], 3600)
        allow_ai = not ai_limit.blocked(g.user["id"])
        if allow_ai:
            ai_limit.hit(g.user["id"])
    result = followup_ai.generate(
        invoice, g.user, request.form.get("tone", ""), request.form.get("channel", "email"),
        request.form.get("instruction", ""), allow_ai=allow_ai,
    )
    db = get_db()
    db.execute(
        "INSERT INTO drafts (invoice_id, user_id, tone, channel, message, source, notice, written_for, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(invoice_id) DO UPDATE SET tone = excluded.tone, "
        "channel = excluded.channel, message = excluded.message, source = excluded.source, "
        "notice = excluded.notice, written_for = excluded.written_for, created_at = excluded.created_at",
        (invoice_id, g.user["id"], result["tone"], result["channel"], result["message"],
         result["source"], result["notice"], today().isoformat(), now_iso()),
    )
    db.commit()
    return redirect(url_for("invoices.detail", invoice_id=invoice_id) + "#followup")


@bp.post("/<int:invoice_id>/followup/discard")
@login_required
def discard_draft(invoice_id):
    get_invoice(invoice_id)
    db = get_db()
    db.execute("DELETE FROM drafts WHERE invoice_id = ? AND user_id = ?", (invoice_id, g.user["id"]))
    db.commit()
    return redirect(url_for("invoices.detail", invoice_id=invoice_id) + "#followup")


@bp.post("/<int:invoice_id>/followups")
@login_required
def log_followup(invoice_id):
    invoice = get_invoice(invoice_id)
    message = request.form.get("message", "").strip()
    draft = get_draft(invoice_id)
    tone = draft["tone"] if draft else request.form.get("tone", "")
    channel = draft["channel"] if draft else request.form.get("channel", "")
    if tone not in followup_ai.TONES:
        tone = followup_ai.suggested_tone(invoice)
    if channel not in followup_ai.CHANNELS:
        channel = "other"
    if not message or len(message) > 5000:
        flash("The follow-up message can't be empty (and must be under 5000 characters).", "error")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id) + "#followup")
    source = draft["source"] if draft and draft["message"].strip() == message else "edited"
    db = get_db()
    with db:
        db.execute(
            "INSERT INTO followups (user_id, invoice_id, tone, channel, message, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            # Business-local time on today()'s date, so history shows the owner's own date.
            (g.user["id"], invoice_id, tone, channel, message, source, local_now_iso()),
        )
        db.execute("DELETE FROM drafts WHERE invoice_id = ? AND user_id = ?", (invoice_id, g.user["id"]))
    flash("Follow-up logged. You'll see when you last chased this invoice on your dashboard.", "success")
    return redirect(url_for("invoices.detail", invoice_id=invoice_id) + "#history")


@bp.app_template_filter("money_for_user")
def money_for_user(cents):
    return format_money(cents, g.user["currency"])
