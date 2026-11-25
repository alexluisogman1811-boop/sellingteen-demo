import re
import secrets
from datetime import date, timedelta

from flask import (Blueprint, abort, current_app, flash, g, redirect, render_template,
                   request, url_for)

from . import STATUS_LABELS
from .ai import AIError, generate_review_request, use_mock
from .db import get_db
from .security import MAX_ID, is_safe_local_path, login_required

bp = Blueprint("customers", __name__)

PHONE_RE = re.compile(r"^\+?[\d\s().-]{7,20}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
FIELDS = ("name", "phone", "email", "service", "job_date", "status")


@bp.context_processor
def date_limits():
    return {"max_date": (date.today() + timedelta(days=1)).isoformat()}


def validate_customer(form):
    data = {k: " ".join(form.get(k, "").split()) for k in FIELDS}  # trim + collapse whitespace
    errors = {}
    if not data["name"]:
        errors["name"] = "Enter the customer's name."
    elif len(data["name"]) > 100:
        errors["name"] = "Keep the name under 100 characters."
    digits = re.sub(r"\D", "", data["phone"])
    if not data["phone"]:
        errors["phone"] = "Enter the customer's phone number."
    elif not PHONE_RE.match(data["phone"]) or not 7 <= len(digits) <= 15:
        errors["phone"] = "Enter a phone number using digits, like (555) 123-4567."
    if data["email"] and (not EMAIL_RE.match(data["email"]) or len(data["email"]) > 254):
        errors["email"] = "That email doesn't look right. Check it, or leave it blank."
    if not data["service"]:
        errors["service"] = "Enter the job you did, like \"Panel upgrade\"."
    elif len(data["service"]) > 120:
        errors["service"] = "Keep the job description under 120 characters."
    try:
        job_date = date.fromisoformat(data["job_date"])
    except ValueError:
        errors["job_date"] = "Choose the date of the job."
    else:
        # One day of slack for time zones; messages thank customers for work already done.
        if job_date > date.today() + timedelta(days=1):
            errors["job_date"] = "The job date can't be in the future. Add customers once the job is done."
        elif job_date.year < 2000:
            errors["job_date"] = "Check the year of the job date."
    if data["status"] not in STATUS_LABELS:
        errors["status"] = "Choose a status from the list."
    return data, errors


def get_customer_or_404(customer_id):
    # Every lookup is scoped to the logged-in business, so other businesses' rows 404.
    if customer_id > MAX_ID:
        abort(404, description="Customer not found.")
    row = get_db().execute(
        "SELECT * FROM customers WHERE id = ? AND business_id = ?",
        (customer_id, g.business["id"]),
    ).fetchone()
    if row is None:
        abort(404, description="Customer not found.")
    return row


def get_templates():
    return get_db().execute(
        "SELECT id, name, body FROM message_templates WHERE business_id = ? ORDER BY name",
        (g.business["id"],),
    ).fetchall()


@bp.route("/")
def index():
    if g.business:
        return redirect(url_for("customers.dashboard"))
    from .demo import render_landing
    return render_landing()


@bp.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    stats = db.execute(
        """SELECT COUNT(*) AS total,
                  COALESCE(SUM(status = 'not_contacted'), 0) AS not_contacted,
                  COALESCE(SUM(status = 'contacted'), 0) AS contacted,
                  COALESCE(SUM(status = 'review_received'), 0) AS reviews
           FROM customers WHERE business_id = ?""",
        (g.business["id"],),
    ).fetchone()
    to_contact = db.execute(
        """SELECT * FROM customers WHERE business_id = ? AND status = 'not_contacted'
           ORDER BY job_date DESC, id DESC LIMIT 5""",
        (g.business["id"],),
    ).fetchall()
    return render_template("dashboard.html", stats=stats, to_contact=to_contact)


@bp.route("/customers")
@login_required
def list_customers():
    q = request.args.get("q", "").strip()[:100]
    status = request.args.get("status", "")
    sql = "SELECT * FROM customers WHERE business_id = ?"
    params = [g.business["id"]]
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        sql += (" AND (name LIKE ? ESCAPE '\\' OR phone LIKE ? ESCAPE '\\'"
                " OR email LIKE ? ESCAPE '\\' OR service LIKE ? ESCAPE '\\')")
        params += [like] * 4
    if status in STATUS_LABELS:
        sql += " AND status = ?"
        params.append(status)
    else:
        status = ""
    sql += " ORDER BY job_date DESC, id DESC"
    customers = get_db().execute(sql, params).fetchall()
    return render_template("customers/list.html", customers=customers, q=q, status=status)


@bp.route("/customers/new", methods=["GET", "POST"])
@login_required
def create_customer():
    data = {"job_date": date.today().isoformat(), "status": "not_contacted"}
    errors = {}
    if request.method == "POST":
        data, errors = validate_customer(request.form)
        if not errors:
            db = get_db()
            cur = db.execute(
                """INSERT INTO customers (business_id, name, phone, email, service, job_date, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (g.business["id"], data["name"], data["phone"], data["email"],
                 data["service"], data["job_date"], data["status"]),
            )
            db.commit()
            flash(f"{data['name']} added. Next: tap “Write message” to create their review request.", "success")
            return redirect(url_for("customers.detail", customer_id=cur.lastrowid) + "#message")
    return render_template("customers/form.html", data=data, errors=errors, customer=None), (
        400 if errors else 200)


@bp.route("/customers/<int:customer_id>")
@login_required
def detail(customer_id):
    customer = get_customer_or_404(customer_id)
    return render_template("customers/detail.html", customer=customer, templates=get_templates(),
                           mock_mode=use_mock(current_app.config))


@bp.route("/customers/<int:customer_id>/edit", methods=["GET", "POST"])
@login_required
def edit_customer(customer_id):
    customer = get_customer_or_404(customer_id)
    data, errors = dict(customer), {}
    if request.method == "POST":
        data, errors = validate_customer(request.form)
        if not errors:
            # A saved message that mentions old details must not be sent as-is.
            stale = bool(customer["message"]) and any(
                data[k] != customer[k] for k in ("name", "service", "job_date"))
            db = get_db()
            db.execute(
                """UPDATE customers SET name = ?, phone = ?, email = ?, service = ?, job_date = ?,
                   status = ?, message_stale = MAX(message_stale, ?), updated_at = datetime('now')
                   WHERE id = ? AND business_id = ?""",
                (data["name"], data["phone"], data["email"], data["service"], data["job_date"],
                 data["status"], int(stale), customer_id, g.business["id"]),
            )
            db.commit()
            if stale:
                flash("Customer updated. Write a new version of the message so it uses the new details.", "info")
            else:
                flash("Customer updated.", "success")
            return redirect(url_for("customers.detail", customer_id=customer_id))
    return render_template("customers/form.html", data=data, errors=errors, customer=customer), (
        400 if errors else 200)


@bp.route("/customers/<int:customer_id>/delete", methods=["POST"])
@login_required
def delete_customer(customer_id):
    customer = get_customer_or_404(customer_id)
    db = get_db()
    db.execute("DELETE FROM customers WHERE id = ? AND business_id = ?", (customer_id, g.business["id"]))
    db.commit()
    flash(f"{customer['name']} deleted.", "info")
    return redirect(url_for("customers.list_customers"))


@bp.route("/customers/<int:customer_id>/status", methods=["POST"])
@login_required
def update_status(customer_id):
    customer = get_customer_or_404(customer_id)
    status = request.form.get("status", "")
    if status not in STATUS_LABELS:
        abort(400, description="Invalid status.")
    db = get_db()
    db.execute(
        "UPDATE customers SET status = ?, updated_at = datetime('now') WHERE id = ? AND business_id = ?",
        (status, customer_id, g.business["id"]),
    )
    db.commit()
    name = customer["name"]
    flash({
        "contacted": f"{name} is marked as contacted. When they leave a review, open their page and tap “They left a review”.",
        "review_received": f"Review received from {name}. Nice work!",
        "not_contacted": f"{name} is back on your “Not asked yet” list.",
    }[status], "success")
    return redirect(safe_back(url_for("customers.detail", customer_id=customer_id)))


@bp.route("/customers/<int:customer_id>/generate", methods=["POST"])
@login_required
def generate_message(customer_id):
    customer = get_customer_or_404(customer_id)
    template_body = None
    template_id = request.form.get("template_id", "")
    if template_id:
        if not template_id.isdigit() or int(template_id) > MAX_ID:
            abort(404, description="Template not found.")
        row = get_db().execute(
            "SELECT body FROM message_templates WHERE id = ? AND business_id = ?",
            (template_id, g.business["id"]),
        ).fetchone()
        if row is None:
            abort(404, description="Template not found.")
        template_body = row["body"]
    try:
        message, _mode = generate_review_request(
            current_app.config, g.business, customer, template_body, seed=secrets.randbits(32))
    except AIError as exc:
        flash(str(exc), "error")
        return redirect(url_for("customers.detail", customer_id=customer_id))
    db = get_db()
    db.execute(
        """UPDATE customers SET message = ?, message_generated_at = datetime('now'), message_stale = 0,
           updated_at = datetime('now') WHERE id = ? AND business_id = ?""",
        (message, customer_id, g.business["id"]),
    )
    db.commit()
    flash("Message ready. Read it over, copy it, and send it by text or email.", "success")
    return redirect(url_for("customers.detail", customer_id=customer_id) + "#message")


def safe_back(default):
    target = request.form.get("next", "")
    return target if is_safe_local_path(target) else default
