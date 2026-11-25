from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from . import billing
from .auth import EMAIL_RE
from .db import get_db
from .util import like_pattern, login_required

bp = Blueprint("customers", __name__, url_prefix="/customers")


def get_customer(customer_id):
    row = get_db().execute(
        "SELECT * FROM customers WHERE id = ? AND user_id = ?", (customer_id, g.user["id"])
    ).fetchone()
    if row is None:
        abort(404)
    return row


def validate(form, customer_id=None):
    data = {
        "name": " ".join(form.get("name", "").split()),
        "contact_name": " ".join(form.get("contact_name", "").split()),
        "email": form.get("email", "").strip(),
        "phone": " ".join(form.get("phone", "").split()),
    }
    errors = {}
    if not data["name"] or len(data["name"]) > 120:
        errors["name"] = "Customer name is required (max 120 characters)."
    elif get_db().execute(
        "SELECT 1 FROM customers WHERE user_id = ? AND name = ? COLLATE NOCASE AND id != ?",
        (g.user["id"], data["name"], customer_id or 0),
    ).fetchone():
        errors["name"] = "You already have a customer with this name."
    if len(data["contact_name"]) > 120:
        errors["contact_name"] = "Max 120 characters."
    if data["email"] and (not EMAIL_RE.match(data["email"]) or len(data["email"]) > 254):
        errors["email"] = "Enter a valid email address (like name@company.com) or leave it blank."
    if len(data["phone"]) > 40:
        errors["phone"] = "Max 40 characters."
    return data, errors


@bp.get("/")
@login_required
def index():
    q = request.args.get("q", "").strip()[:100]
    sql = """
        SELECT c.*,
               COUNT(i.id) AS invoice_count,
               COALESCE(SUM(CASE WHEN i.paid_at IS NULL THEN i.amount_cents END), 0) AS outstanding_cents
        FROM customers c LEFT JOIN invoices i ON i.customer_id = c.id AND i.user_id = c.user_id
        WHERE c.user_id = ?
    """
    params = [g.user["id"]]
    if q:
        sql += " AND (c.name LIKE ? ESCAPE '\\' OR c.email LIKE ? ESCAPE '\\' OR c.contact_name LIKE ? ESCAPE '\\')"
        params += [like_pattern(q)] * 3
    sql += " GROUP BY c.id ORDER BY c.name COLLATE NOCASE"
    rows = get_db().execute(sql, params).fetchall()
    return render_template("customers/index.html", customers=rows, q=q)


@bp.get("/<int:customer_id>")
@login_required
def detail(customer_id):
    from .invoices import query_invoices

    customer = get_customer(customer_id)
    invoices = query_invoices(customer_id=customer_id)
    unpaid = [i for i in invoices if i["status"] != "paid"]
    totals = {
        "outstanding": sum(i["amount_cents"] for i in unpaid),
        "overdue": sum(i["amount_cents"] for i in unpaid if i["status"] == "overdue"),
        "paid": sum(i["amount_cents"] for i in invoices if i["status"] == "paid"),
    }
    return render_template("customers/detail.html", customer=customer, invoices=invoices, totals=totals)


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    # Free plan: adding one more customer than the plan allows shows the upgrade page instead.
    # Checked on POST too, so a form opened earlier can't slip past the limit.
    if billing.free_plan_full():
        return billing.upgrade_page(403 if request.method == "POST" else 200)
    data, errors = {}, {}
    if request.method == "POST":
        data, errors = validate(request.form)
        if not errors:
            db = get_db()
            cur = db.execute(
                "INSERT INTO customers (user_id, name, contact_name, email, phone) VALUES (?, ?, ?, ?, ?)",
                (g.user["id"], data["name"], data["contact_name"], data["email"], data["phone"]),
            )
            db.commit()
            flash(f"Customer “{data['name']}” added.", "success")
            if request.args.get("then") == "invoice":
                return redirect(url_for("invoices.new", customer_id=cur.lastrowid))
            return redirect(url_for("customers.detail", customer_id=cur.lastrowid))
    return render_template("customers/form.html", data=data, errors=errors, customer=None), (400 if errors else 200)


@bp.route("/<int:customer_id>/edit", methods=["GET", "POST"])
@login_required
def edit(customer_id):
    customer = get_customer(customer_id)
    data, errors = dict(customer), {}
    if request.method == "POST":
        data, errors = validate(request.form, customer_id)
        if not errors:
            db = get_db()
            db.execute(
                "UPDATE customers SET name = ?, contact_name = ?, email = ?, phone = ? WHERE id = ? AND user_id = ?",
                (data["name"], data["contact_name"], data["email"], data["phone"], customer_id, g.user["id"]),
            )
            db.commit()
            flash("Customer updated.", "success")
            return redirect(url_for("customers.detail", customer_id=customer_id))
    return render_template("customers/form.html", data=data, errors=errors, customer=customer), (400 if errors else 200)


@bp.post("/<int:customer_id>/delete")
@login_required
def delete(customer_id):
    customer = get_customer(customer_id)
    db = get_db()
    count = db.execute(
        "SELECT COUNT(*) FROM invoices WHERE customer_id = ? AND user_id = ?", (customer_id, g.user["id"])
    ).fetchone()[0]
    if count:
        flash(f"“{customer['name']}” still has {count} invoice{'s' if count != 1 else ''}. "
              "Delete or reassign those invoices first, so no invoice is left without a customer.", "error")
        return redirect(url_for("customers.detail", customer_id=customer_id))
    db.execute("DELETE FROM customers WHERE id = ? AND user_id = ?", (customer_id, g.user["id"]))
    db.commit()
    flash(f"Customer “{customer['name']}” deleted.", "success")
    return redirect(url_for("customers.index"))
