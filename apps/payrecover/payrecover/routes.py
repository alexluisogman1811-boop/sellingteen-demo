import re
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from flask import (Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template,
                   request, url_for)

from . import ai
from .auth import EMAIL_RE, login_required
from .db import get_db, today
from .followups import STAGES, days_overdue, display_state, next_followup, planned_schedule
from .formatting import money, nice_date

bp = Blueprint("routes", __name__)

FILTERS = {
    "all": "All",
    "followup": "Follow-up today",
    "overdue": "Overdue",
    "due_soon": "Due soon",
    "paid": "Paid",
}
PHONE_RE = re.compile(r"^[0-9+()\-.\s]{7,25}$")
INVOICE_NO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_/#.]{0,39}$")
MAX_AMOUNT = Decimal("1000000")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _like(term):
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# ------------------------------------------------------------------ loading

def load_invoices(user_id, *, q="", customer_id=None, invoice_id=None):
    """Invoices for one owner, enriched with state and follow-up recommendation."""
    sql = [
        "SELECT i.*, c.name AS customer_name, c.company AS customer_company,"
        " c.email AS customer_email, c.phone AS customer_phone"
        " FROM invoices i JOIN customers c ON c.id = i.customer_id"
        " WHERE i.user_id = ?"
    ]
    params = [user_id]
    if customer_id is not None:
        sql.append("AND i.customer_id = ?")
        params.append(customer_id)
    if invoice_id is not None:
        sql.append("AND i.id = ?")
        params.append(invoice_id)
    if q:
        sql.append(
            "AND (c.name LIKE ? ESCAPE '\\' OR c.company LIKE ? ESCAPE '\\'"
            " OR c.email LIKE ? ESCAPE '\\' OR i.number LIKE ? ESCAPE '\\')"
        )
        params += [_like(q)] * 4
    sql.append("ORDER BY i.due_date, i.id")
    db = get_db()
    rows = db.execute(" ".join(sql), params).fetchall()

    history = {}
    if rows:
        for f in db.execute(
            "SELECT invoice_id, stage, action, message, on_date FROM followups WHERE user_id = ? ORDER BY stage",
            (user_id,),
        ):
            history.setdefault(f["invoice_id"], []).append(dict(f))

    now = today()
    return [enrich(r, history.get(r["id"], []), now) for r in rows]


def enrich(row, history, now):
    inv = dict(row)
    due = date.fromisoformat(inv["due_date"])
    inv["history"] = history
    inv["state"] = display_state(inv["status"], due, now)
    inv["days_overdue"] = days_overdue(due, now) if inv["state"] == "overdue" else 0
    inv["days_until_due"] = (due - now).days
    pairs = [(h["stage"], date.fromisoformat(h["on_date"])) for h in history]
    inv["next"] = next_followup(inv["status"], due, pairs, now)
    inv["followup_due"] = bool(inv["next"] and inv["next"].is_due)
    inv["reminders_sent"] = sum(1 for h in history if h["action"] == "sent")
    return inv


def get_invoice_or_404(invoice_id):
    found = load_invoices(g.user["id"], invoice_id=invoice_id)
    if not found:
        abort(404)
    return found[0]


def get_customer_or_404(customer_id):
    row = get_db().execute(
        "SELECT * FROM customers WHERE id = ? AND user_id = ?", (customer_id, g.user["id"])
    ).fetchone()
    if row is None:
        abort(404)
    return row


def apply_filter(invoices, key):
    if key == "followup":
        return [i for i in invoices if i["followup_due"]]
    if key in ("overdue", "due_soon", "paid"):
        return [i for i in invoices if i["state"] == key]
    return invoices


def summarize(invoices):
    unpaid = [i for i in invoices if i["status"] == "sent"]
    overdue = [i for i in unpaid if i["state"] == "overdue"]
    due_soon = [i for i in unpaid if i["state"] == "due_soon"]
    paid = [i for i in invoices if i["status"] == "paid"]
    return {
        "outstanding": sum(i["amount_cents"] for i in unpaid),
        "outstanding_count": len(unpaid),
        "overdue": sum(i["amount_cents"] for i in overdue),
        "overdue_count": len(overdue),
        "due_soon": sum(i["amount_cents"] for i in due_soon),
        "due_soon_count": len(due_soon),
        "followups_today": sum(1 for i in invoices if i["followup_due"]),
        "recovered": sum(i["amount_cents"] for i in paid),
        "recovered_count": len(paid),
        "recovered_after_followup": sum(
            i["amount_cents"] for i in paid if any(h["action"] == "sent" for h in i["history"])
        ),
        "drafts": sum(1 for i in invoices if i["status"] == "draft"),
    }


# ---------------------------------------------------------------- dashboard

@bp.route("/")
def index():
    """Owners go to their dashboard; everyone else sees the public landing page."""
    if g.user:
        return redirect(url_for("routes.dashboard"))
    return render_template("landing.html")


@bp.route("/healthz")
def healthz():
    """For uptime monitoring: checks the database answers. Reveals nothing about accounts."""
    try:
        get_db().execute("SELECT 1").fetchone()
    except sqlite3.Error:
        return jsonify(status="error"), 503
    return jsonify(status="ok")


@bp.route("/dashboard")
@login_required
def dashboard():
    invoices = load_invoices(g.user["id"])
    stats = summarize(invoices)
    followups = sorted(
        (i for i in invoices if i["followup_due"]), key=lambda i: (-i["days_overdue"], i["due_date"])
    )
    owed = sorted(
        (i for i in invoices if i["status"] == "sent"),
        key=lambda i: (-i["days_overdue"], i["due_date"]),
    )
    has_customers = get_db().execute(
        "SELECT 1 FROM customers WHERE user_id = ? LIMIT 1", (g.user["id"],)
    ).fetchone() is not None
    return render_template(
        "dashboard.html", stats=stats, followups=followups, owed=owed,
        has_customers=has_customers, has_invoices=bool(invoices), today=today(),
    )


# ---------------------------------------------------------------- customers

def validate_customer(form):
    data = {k: form.get(k, "").strip() for k in ("name", "email", "phone", "company")}
    data["email"] = data["email"].lower()
    errors = {}
    if not 1 <= len(data["name"]) <= 100:
        errors["name"] = "Enter the customer's name (up to 100 characters)."
    if data["email"] and (not EMAIL_RE.match(data["email"]) or len(data["email"]) > 254):
        errors["email"] = "Enter a valid email address."
    if data["phone"] and (not PHONE_RE.match(data["phone"]) or sum(c.isdigit() for c in data["phone"]) < 7):
        errors["phone"] = "Enter a valid phone number."
    if not data["email"] and not data["phone"] and "email" not in errors and "phone" not in errors:
        errors["email"] = "Add an email or phone number so you can send reminders."
    if len(data["company"]) > 100:
        errors["company"] = "Company name is too long (100 characters max)."
    return data, errors


@bp.route("/customers")
@login_required
def customers():
    q = request.args.get("q", "").strip()[:100]
    sql = (
        "SELECT c.*,"
        " COALESCE(SUM(CASE WHEN i.status = 'sent' THEN i.amount_cents END), 0) AS owed_cents,"
        " COUNT(i.id) AS invoice_count"
        " FROM customers c LEFT JOIN invoices i ON i.customer_id = c.id"
        " WHERE c.user_id = ?"
    )
    params = [g.user["id"]]
    if q:
        sql += (" AND (c.name LIKE ? ESCAPE '\\' OR c.company LIKE ? ESCAPE '\\'"
                " OR c.email LIKE ? ESCAPE '\\' OR c.phone LIKE ? ESCAPE '\\')")
        params += [_like(q)] * 4
    sql += " GROUP BY c.id ORDER BY c.name COLLATE NOCASE"
    rows = get_db().execute(sql, params).fetchall()
    return render_template("customers.html", customers=rows, q=q)


@bp.route("/customers/new", methods=["GET", "POST"])
@login_required
def customer_new():
    data, errors = {"name": "", "email": "", "phone": "", "company": ""}, {}
    if request.method == "POST":
        data, errors = validate_customer(request.form)
        if not errors:
            db = get_db()
            cur = db.execute(
                "INSERT INTO customers (user_id, name, email, phone, company, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (g.user["id"], data["name"], data["email"], data["phone"], data["company"], _now()),
            )
            db.commit()
            flash(f"{data['name']} added. Now create an invoice for them.", "success")
            return redirect(url_for("routes.invoice_new", customer_id=cur.lastrowid))
    return render_template("customer_form.html", data=data, errors=errors), (400 if errors else 200)


@bp.route("/customers/<int:customer_id>")
@login_required
def customer_detail(customer_id):
    customer = get_customer_or_404(customer_id)
    invoices = load_invoices(g.user["id"], customer_id=customer_id)
    activity = []
    for inv in invoices:
        activity.append((inv["created_at"][:10], f"Invoice {inv['number']} created ({money(inv['amount_cents'])})"))
        for h in inv["history"]:
            verb = "sent" if h["action"] == "sent" else "skipped"
            activity.append((h["on_date"], f"{STAGES[h['stage'] - 1].label} {verb} for {inv['number']}"))
        if inv["paid_on"]:
            activity.append((inv["paid_on"], f"Invoice {inv['number']} paid ({money(inv['amount_cents'])})"))
    activity.sort(key=lambda a: a[0], reverse=True)
    return render_template(
        "customer_detail.html", customer=customer, invoices=invoices,
        stats=summarize(invoices), activity=activity,
    )


# ----------------------------------------------------------------- invoices

def next_invoice_number(user_id):
    rows = get_db().execute("SELECT number FROM invoices WHERE user_id = ?", (user_id,)).fetchall()
    highest = 1000
    for r in rows:
        m = re.fullmatch(r"INV-(\d+)", r["number"])
        if m:
            highest = max(highest, int(m.group(1)))
    return f"INV-{highest + 1}"


def _parse_date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def validate_invoice(form, user_id):
    data = {
        "customer_id": form.get("customer_id", ""),
        "number": form.get("number", "").strip(),
        "amount": form.get("amount", "").strip().replace(",", "").lstrip("$"),
        "issue_date": form.get("issue_date", "").strip(),
        "due_date": form.get("due_date", "").strip(),
        "notes": form.get("notes", "").strip(),
        "status": form.get("status", "sent"),
    }
    errors = {}
    customer = None
    if data["customer_id"].isdigit():
        customer = get_db().execute(
            "SELECT id FROM customers WHERE id = ? AND user_id = ?", (int(data["customer_id"]), user_id)
        ).fetchone()
    if customer is None:
        errors["customer_id"] = "Choose a customer."
    if not INVOICE_NO_RE.match(data["number"]):
        errors["number"] = "Use letters, numbers and - _ / # . (up to 40 characters)."
    elif get_db().execute(
        "SELECT 1 FROM invoices WHERE user_id = ? AND number = ?", (user_id, data["number"])
    ).fetchone():
        errors["number"] = "You already have an invoice with this number."
    amount_cents = None
    try:
        amount = Decimal(data["amount"])
        if not amount.is_finite() or amount <= 0 or amount > MAX_AMOUNT or amount != amount.quantize(Decimal("0.01")):
            raise InvalidOperation
        amount_cents = int(amount * 100)
    except (InvalidOperation, ValueError):
        errors["amount"] = "Enter an amount greater than 0, like 250 or 1250.50."
    issue, due = _parse_date(data["issue_date"]), _parse_date(data["due_date"])
    if issue is None:
        errors["issue_date"] = "Enter the date you issued the invoice."
    if due is None:
        errors["due_date"] = "Enter the date payment is due."
    elif issue and due < issue:
        errors["due_date"] = "The due date can't be before the issue date."
    if len(data["notes"]) > 1000:
        errors["notes"] = "Notes are too long (1000 characters max)."
    if data["status"] not in ("draft", "sent", "paid"):
        errors["status"] = "Choose a status."
    return data, amount_cents, errors


@bp.route("/invoices")
@login_required
def invoices():
    q = request.args.get("q", "").strip()[:100]
    key = request.args.get("filter", "all")
    if key not in FILTERS:
        key = "all"
    all_invoices = load_invoices(g.user["id"], q=q)
    counts = {k: len(apply_filter(all_invoices, k)) for k in FILTERS}
    shown = apply_filter(all_invoices, key)
    if key == "paid":
        shown = sorted(shown, key=lambda i: i["paid_on"] or "", reverse=True)
    return render_template("invoices.html", invoices=shown, q=q, active=key, filters=FILTERS, counts=counts)


@bp.route("/invoices/new", methods=["GET", "POST"])
@login_required
def invoice_new():
    db = get_db()
    customers_list = db.execute(
        "SELECT id, name, company FROM customers WHERE user_id = ? ORDER BY name COLLATE NOCASE",
        (g.user["id"],),
    ).fetchall()
    if not customers_list:
        flash("Add a customer first, then create their invoice.", "info")
        return redirect(url_for("routes.customer_new"))
    now = today()
    data = {
        "customer_id": request.args.get("customer_id", ""),
        "number": next_invoice_number(g.user["id"]),
        "amount": "",
        "issue_date": now.isoformat(),
        "due_date": "",
        "notes": "",
        "status": "sent",
    }
    errors = {}
    if request.method == "POST":
        data, amount_cents, errors = validate_invoice(request.form, g.user["id"])
        if not errors:
            sent_on = data["issue_date"] if data["status"] in ("sent", "paid") else None
            paid_on = now.isoformat() if data["status"] == "paid" else None
            cur = db.execute(
                "INSERT INTO invoices (user_id, customer_id, number, amount_cents, issue_date, due_date,"
                " notes, status, sent_on, paid_on, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (g.user["id"], int(data["customer_id"]), data["number"], amount_cents, data["issue_date"],
                 data["due_date"], data["notes"], data["status"], sent_on, paid_on, _now()),
            )
            db.commit()
            flash(f"Invoice {data['number']} saved.", "success")
            return redirect(url_for("routes.invoice_detail", invoice_id=cur.lastrowid))
    return render_template(
        "invoice_form.html", data=data, errors=errors, customers=customers_list
    ), (400 if errors else 200)


def build_timeline(inv, now):
    """Steps for the invoice timeline: created, sent, due, reminders 1-4, paid."""
    due = date.fromisoformat(inv["due_date"])
    steps = [{"label": "Invoice created", "date": inv["issue_date"], "state": "done"}]
    if inv["sent_on"]:
        steps.append({"label": "Invoice sent", "date": inv["sent_on"], "state": "done"})
    else:
        steps.append({"label": "Invoice sent", "date": None, "state": "upcoming", "detail": "Not sent yet"})
    steps.append({
        "label": "Due date", "date": inv["due_date"],
        "state": "done" if now >= due or inv["status"] == "paid" else "upcoming",
    })
    done = {h["stage"]: h for h in inv["history"]}
    pairs = [(h["stage"], date.fromisoformat(h["on_date"])) for h in inv["history"]]
    plan = planned_schedule(inv["status"], due, pairs, now)
    for stage in STAGES:
        step = {"label": stage.label, "date": None}
        if stage.number in done:
            h = done[stage.number]
            step.update(date=h["on_date"], state="done" if h["action"] == "sent" else "skipped",
                        detail="Sent" if h["action"] == "sent" else "Skipped")
        elif stage.number in plan:
            planned = plan[stage.number]
            due_now = inv["next"].stage.number == stage.number and inv["next"].is_due
            step.update(date=None if due_now else planned.isoformat(),
                        state="due" if due_now else "upcoming",
                        detail="Due now" if due_now else "Planned")
        elif inv["status"] == "draft":
            step.update(state="upcoming", detail=f"{stage.day} day{'s' if stage.day > 1 else ''} after due date")
        else:
            step.update(state="off", detail="Not needed")
        steps.append(step)
    if inv["paid_on"]:
        steps.append({"label": "Paid", "date": inv["paid_on"], "state": "done"})
    else:
        steps.append({"label": "Paid", "date": None, "state": "upcoming", "detail": "Waiting for payment"})
    return steps


@bp.route("/invoices/<int:invoice_id>")
@login_required
def invoice_detail(invoice_id):
    inv = get_invoice_or_404(invoice_id)
    now = today()
    return render_template("invoice_detail.html", inv=inv, timeline=build_timeline(inv, now), today=now)


@bp.route("/invoices/<int:invoice_id>/send", methods=["POST"])
@login_required
def invoice_mark_sent(invoice_id):
    inv = get_invoice_or_404(invoice_id)
    if inv["status"] != "draft":
        flash("This invoice is already marked as sent.", "info")
    else:
        db = get_db()
        db.execute(
            "UPDATE invoices SET status = 'sent', sent_on = ? WHERE id = ? AND user_id = ?",
            (today().isoformat(), invoice_id, g.user["id"]),
        )
        db.commit()
        flash(f"Invoice {inv['number']} marked as sent. We'll recommend follow-ups if it goes overdue.", "success")
    return redirect(url_for("routes.invoice_detail", invoice_id=invoice_id))


@bp.route("/invoices/<int:invoice_id>/paid", methods=["POST"])
@login_required
def invoice_mark_paid(invoice_id):
    inv = get_invoice_or_404(invoice_id)
    if inv["status"] == "paid":
        flash("This invoice is already marked as paid.", "info")
        return redirect(url_for("routes.invoice_detail", invoice_id=invoice_id))
    now = today()
    paid_on = _parse_date(request.form.get("paid_on", "").strip() or now.isoformat())
    if paid_on is None or paid_on > now or paid_on < date.fromisoformat(inv["issue_date"]):
        flash("Enter a payment date between the issue date and today.", "error")
        return redirect(url_for("routes.invoice_detail", invoice_id=invoice_id))
    db = get_db()
    db.execute(
        "UPDATE invoices SET status = 'paid', paid_on = ?, sent_on = COALESCE(sent_on, ?)"
        " WHERE id = ? AND user_id = ?",
        (paid_on.isoformat(), inv["issue_date"], invoice_id, g.user["id"]),
    )
    db.commit()
    flash(f"Paid! {money(inv['amount_cents'])} from {inv['customer_name']} added to recovered revenue. "
          "Follow-ups for this invoice have stopped.", "success")
    return redirect(request.form.get("return_to") == "dashboard"
                    and url_for("routes.dashboard")
                    or url_for("routes.invoice_detail", invoice_id=invoice_id))


# ---------------------------------------------------------------- follow-ups

def reminder_context(inv):
    return ai.ReminderContext(
        customer_name=inv["customer_name"],
        invoice_number=inv["number"],
        amount=money(inv["amount_cents"]),
        due_date=nice_date(inv["due_date"]),
        days_overdue=inv["days_overdue"],
        stage_key=inv["next"].stage.key,
        business_name=g.user["business_name"],
    )


@bp.route("/invoices/<int:invoice_id>/followup", methods=["GET", "POST"])
@login_required
def followup(invoice_id):
    inv = get_invoice_or_404(invoice_id)
    if not inv["followup_due"]:
        if inv["status"] == "paid":
            flash("This invoice is paid, so no follow-up is needed.", "info")
        elif inv["next"]:
            flash(f"No follow-up needed yet. Next one is on {nice_date(inv['next'].due_on)}.", "info")
        else:
            flash("There are no follow-ups left for this invoice.", "info")
        return redirect(url_for("routes.invoice_detail", invoice_id=invoice_id))

    stage = inv["next"].stage
    if request.method == "POST":
        action = request.form.get("action")
        if action not in ("sent", "skip") or request.form.get("stage") != str(stage.number):
            flash("That follow-up was already handled. Here's the current one.", "info")
            return redirect(url_for("routes.followup", invoice_id=invoice_id))
        message = request.form.get("message", "").strip()[:2000] if action == "sent" else ""
        db = get_db()
        db.execute(
            "INSERT INTO followups (user_id, invoice_id, stage, action, message, on_date, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (g.user["id"], invoice_id, stage.number, "sent" if action == "sent" else "skipped",
             message, today().isoformat(), _now()),
        )
        db.commit()
        updated = get_invoice_or_404(invoice_id)
        what = f"{stage.label} for {inv['number']} " + ("marked as sent." if action == "sent" else "skipped.")
        if updated["next"]:
            what += f" Next follow-up: {nice_date(updated['next'].due_on)}."
        else:
            what += " That was the last scheduled reminder."
        flash(what, "success")
        return redirect(url_for("routes.dashboard"))

    draft = ai.generate_reminder(reminder_context(inv), current_app.config)
    return render_template("followup.html", inv=inv, stage=stage, draft=draft, total_stages=len(STAGES))
