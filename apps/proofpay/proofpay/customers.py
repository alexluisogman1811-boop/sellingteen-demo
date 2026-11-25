"""Minimal customer records: name + contact details, and their jobs. Not a CRM."""
from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required

from .auth import EMAIL_RE
from .helpers import current_business, get_customer_or_404
from .models import Customer, Job, db

bp = Blueprint("customers", __name__)


def _read_form():
    f = {k: request.form.get(k, "").strip() for k in ("name", "email", "phone")}
    errors = {}
    if not f["name"] or len(f["name"]) > 120:
        errors["name"] = "Enter the customer's name (under 120 characters)."
    if f["email"] and (not EMAIL_RE.match(f["email"]) or len(f["email"]) > 255):
        errors["email"] = "Enter a valid email address."
    if len(f["phone"]) > 40:
        errors["phone"] = "That phone number is too long."
    return f, errors


@bp.route("/customers")
@login_required
def list_customers():
    business = current_business()
    job_count = (db.select(db.func.count(Job.id)).where(Job.customer_id == Customer.id)
                 .correlate(Customer).scalar_subquery())
    rows = db.session.execute(
        db.select(Customer, job_count).filter_by(business_id=business.id)
        .order_by(Customer.name)).all()
    return render_template("customers/list.html", rows=rows)


@bp.route("/customers/new", methods=["GET", "POST"])
@login_required
def new_customer():
    business = current_business()
    form, errors = {}, {}
    if request.method == "POST":
        form, errors = _read_form()
        if not errors:
            customer = Customer(business_id=business.id, name=form["name"],
                                email=form["email"].lower() or None, phone=form["phone"] or None)
            db.session.add(customer)
            db.session.commit()
            flash("Customer added.", "success")
            return redirect(url_for("customers.customer_detail", ref=customer.ref))
    return render_template("customers/form.html", customer=None, form=form,
                           errors=errors), 400 if errors else 200


@bp.route("/customers/<ref>")
@login_required
def customer_detail(ref):
    customer = get_customer_or_404(ref)
    jobs = db.session.scalars(db.select(Job).filter_by(
        customer_id=customer.id, business_id=customer.business_id)
        .order_by(Job.created_at.desc())).all()
    return render_template("customers/detail.html", customer=customer, jobs=jobs)


@bp.route("/customers/<ref>/edit", methods=["GET", "POST"])
@login_required
def edit_customer(ref):
    customer = get_customer_or_404(ref)
    errors = {}
    form = {"name": customer.name, "email": customer.email or "", "phone": customer.phone or ""}
    if request.method == "POST":
        form, errors = _read_form()
        if not errors:
            customer.name = form["name"]
            customer.email = form["email"].lower() or None
            customer.phone = form["phone"] or None
            db.session.commit()
            flash("Customer updated.", "success")
            return redirect(url_for("customers.customer_detail", ref=customer.ref))
    return render_template("customers/form.html", customer=customer, form=form,
                           errors=errors), 400 if errors else 200
