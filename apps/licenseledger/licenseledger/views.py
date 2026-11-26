"""Account pages. Every route here requires a signed-in account (see create_app.guard),
except the dashboard URL "/", which shows the public landing page to visitors."""
import io
import zipfile
from datetime import datetime

from flask import (
    Blueprint, Response, abort, current_app, flash, g, make_response, redirect, render_template, request, session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from . import csv_io, demo, services
from .auth import bump_auth_version, validate_account_email, validate_business_name, validate_new_password
from .db import get_db
from .services import NotFound, ValidationError

bp = Blueprint("main", __name__)

PER_PAGE = 50
WINDOWS = (7, 14, 30, 60, 90)


@bp.app_errorhandler(NotFound)
def not_found(_exc):
    return render_template("error.html", code=404, message="That record does not exist."), 404


@bp.app_errorhandler(400)
def bad_request(exc):
    return render_template("error.html", code=400, message=getattr(exc, "description", "Bad request.")), 400


@bp.app_errorhandler(404)
def http_not_found(_exc):
    return render_template("error.html", code=404, message="Page not found."), 404


@bp.app_errorhandler(413)
def too_large(_exc):
    return render_template("error.html", code=413, message="That file is too large (limit 2 MB)."), 413


@bp.app_errorhandler(500)
def server_error(_exc):
    # Generic message only: never echo exception text, form data or keys.
    return render_template("error.html", code=500,
                           message="Something went wrong. Nothing was saved for this request."), 500


def flash_errors(exc):
    for message in exc.errors:
        flash(message, "error")


def window_arg():
    try:
        value = int(request.args.get("window", 30))
    except ValueError:
        value = 30
    return value if value in WINDOWS else 30


def secret_response(html):
    """Responses that contain a plaintext key must never be cached or stored."""
    resp = make_response(html)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


def form_choices(db):
    return {
        "products": services.list_products(db),
        "customers": services.list_customers(db),
        "orders": services.list_orders(db),
    }


# --------------------------------------------------------------------------- dashboard

@bp.route("/")
def dashboard():
    if g.get("account") is None:
        # Public landing page for visitors who are not logged in.
        return render_template("landing.html", price=current_app.config["PRICE_TEXT"],
                               trial_days=current_app.config["TRIAL_DAYS"])
    data = services.dashboard_data(get_db(), window_days=window_arg())
    return render_template("dashboard.html", windows=WINDOWS, **data)


# --------------------------------------------------------------------------- licenses

@bp.route("/licenses")
def licenses():
    db = get_db()
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    filters = {
        "q": request.args.get("q", "").strip()[:200],
        "status": request.args.get("status", ""),
        "license_type": request.args.get("type", ""),
        "product_id": services.parse_id(request.args.get("product_id")),
        "expiry": request.args.get("expiry", "") if request.args.get("expiry") in ("expiring", "expired") else "",
        "delivery": request.args.get("delivery", "") if request.args.get("delivery") in services.DELIVERY_STATUSES else "",
        "window_days": window_arg(),
        "sort": request.args.get("sort", "newest") if request.args.get("sort") in services.SORTS else "newest",
    }
    rows, total = services.search_licenses(db, limit=PER_PAGE, offset=(page - 1) * PER_PAGE, **filters)
    pages = max(1, -(-total // PER_PAGE))
    args = {k: v for k, v in request.args.items() if k != "page"}
    return render_template(
        "licenses/list.html", rows=rows, total=total, page=page, pages=pages, filters=filters,
        products=services.list_products(db), windows=WINDOWS, args=args, sorts=services.SORTS,
    )


@bp.route("/licenses/new", methods=["GET", "POST"])
def license_new():
    db = get_db()
    if request.method == "POST":
        form = request.form
        key = form.get("existing_key", "") if form.get("key_mode") == "existing" else None
        if key is not None and not key.strip():
            flash("Enter the existing license key, or choose to generate a new one.", "error")
        else:
            try:
                license_id, plain_key = services.create_license(
                    db, form, key=key,
                    source="Existing key recorded" if key else "Issued manually",
                )
                db.commit()
            except ValidationError as exc:
                db.rollback()
                flash_errors(exc)
            else:
                lic = services.get_license(db, license_id)
                return secret_response(render_template(
                    "licenses/issued.html", lic=lic, key=plain_key, heading="License created",
                    generated=key is None,
                ))
    else:
        form = {
            "issue_date": services.get_today().isoformat(),
            "order_id": request.args.get("order_id", ""),
            "customer_id": request.args.get("customer_id", ""),
            "product_id": request.args.get("product_id", ""),
        }
        order_id = services.parse_id(form["order_id"])
        if order_id:
            order = db.execute("SELECT customer_id FROM orders WHERE id = ? AND account_id = ?",
                               (order_id, services.acct())).fetchone()
            if order:
                form["customer_id"] = str(order["customer_id"])
    return render_template("licenses/form.html", form=form, lic=None, **form_choices(db))


@bp.route("/licenses/lookup", methods=["POST"])
def license_lookup():
    """Find a license by its full key. POST only, so keys never appear in URLs or access logs."""
    db = get_db()
    lic = services.find_license_by_key(db, request.form.get("key", ""))
    if not lic:
        flash("No license matches that key.", "error")
        return redirect(url_for("main.licenses"))
    flash(f"That key belongs to license #{lic['id']} ({lic['effective_status']}).", "success")
    return redirect(url_for("main.license_detail", license_id=lic["id"]))


@bp.route("/licenses/<int:license_id>")
def license_detail(license_id):
    db = get_db()
    lic = services.get_license(db, license_id)
    chain = services.license_chain(db, license_id)
    return render_template(
        "licenses/detail.html", lic=lic, events=services.list_audit(db, license_id=license_id),
        renewals=services.list_renewals(db, chain), chain=chain,
        has_history=services.license_has_history(db, lic),
        templates=services.list_templates(db), today=services.get_today().isoformat(),
    )


@bp.route("/licenses/<int:license_id>/edit", methods=["GET", "POST"])
def license_edit(license_id):
    db = get_db()
    lic = services.get_license(db, license_id)
    form = dict(lic)
    if request.method == "POST":
        form = request.form
        try:
            changed = services.update_license(db, license_id, form)
            db.commit()
            flash("License updated." if changed else "No changes to save.", "success")
            return redirect(url_for("main.license_detail", license_id=license_id))
        except ValidationError as exc:
            db.rollback()
            flash_errors(exc)
    return render_template("licenses/form.html", form=form, lic=lic, **form_choices(db))


def _license_action(license_id, action, success):
    db = get_db()
    try:
        action(db)
        db.commit()
        flash(success, "success")
    except ValidationError as exc:
        db.rollback()
        flash_errors(exc)
    return redirect(url_for("main.license_detail", license_id=license_id))


@bp.route("/licenses/<int:license_id>/renew", methods=["POST"])
def license_renew(license_id):
    f = request.form
    if "period" in f:
        days, months = services.parse_period(f.get("period"))
        if days is None and months is None:
            days = "invalid"  # reported by renew_license's validation
    else:
        days, months = f.get("days"), None
    return _license_action(
        license_id,
        lambda db: services.renew_license(db, license_id, days=days, months=months,
                                          new_expiration=f.get("new_expiration"), note=f.get("note", "")),
        "License renewed.",
    )


@bp.route("/licenses/<int:license_id>/revoke", methods=["POST"])
def license_revoke(license_id):
    return _license_action(
        license_id, lambda db: services.revoke_license(db, license_id, request.form.get("reason", "")),
        "License revoked.",
    )


@bp.route("/licenses/<int:license_id>/status", methods=["POST"])
def license_status(license_id):
    return _license_action(
        license_id,
        lambda db: services.set_license_status(db, license_id, request.form.get("status", ""),
                                               request.form.get("reason", "")),
        "Status updated.",
    )


@bp.route("/licenses/<int:license_id>/deliver", methods=["POST"])
def license_deliver(license_id):
    f = request.form
    return _license_action(
        license_id,
        lambda db: services.mark_delivered(db, license_id, f.get("method", ""), f.get("reference", ""),
                                           allow_unpaid=f.get("allow_unpaid") == "1"),
        "Marked as delivered.",
    )


@bp.route("/licenses/<int:license_id>/reopen-delivery", methods=["POST"])
def license_reopen_delivery(license_id):
    return _license_action(
        license_id, lambda db: services.reopen_delivery(db, license_id, request.form.get("reason", "")),
        "Delivery reopened; the license is pending delivery again.",
    )


@bp.route("/licenses/<int:license_id>/note", methods=["POST"])
def license_note(license_id):
    return _license_action(
        license_id, lambda db: services.add_note(db, license_id, request.form.get("note", "")), "Note added."
    )


@bp.route("/licenses/<int:license_id>/replace", methods=["POST"])
def license_replace(license_id):
    db = get_db()
    try:
        new_id, key = services.replace_license(db, license_id, request.form.get("reason", ""))
        db.commit()
    except ValidationError as exc:
        db.rollback()
        flash_errors(exc)
        return redirect(url_for("main.license_detail", license_id=license_id))
    lic = services.get_license(db, new_id)
    return secret_response(render_template(
        "licenses/issued.html", lic=lic, key=key, heading=f"Replacement for license #{license_id}", generated=True,
    ))


@bp.route("/licenses/<int:license_id>/delete", methods=["POST"])
def license_delete(license_id):
    db = get_db()
    try:
        services.delete_license(db, license_id)
    except ValidationError as exc:
        db.rollback()
        flash_errors(exc)
        return redirect(url_for("main.license_detail", license_id=license_id))
    db.commit()
    flash(f"License #{license_id} deleted. The audit history keeps a record of the deletion.", "success")
    return redirect(url_for("main.licenses"))


@bp.route("/licenses/<int:license_id>/reminder")
def license_reminder(license_id):
    db = get_db()
    lic = services.get_license(db, license_id)
    templates = services.list_templates(db)
    if not templates:
        flash("Create a reminder template first.", "error")
        return redirect(url_for("main.templates"))
    template_id = services.parse_id(request.args.get("template_id")) or templates[0]["id"]
    tpl = services.get_template(db, template_id)
    subject, body = services.render_reminder(db, tpl, lic)
    return render_template(
        "licenses/reminder.html", lic=lic, templates=templates, tpl=tpl, subject=subject, body=body,
    )


# --------------------------------------------------------------------------- products

@bp.route("/products")
def products():
    return render_template("products/list.html", products=services.list_products(get_db()))


@bp.route("/products/new", methods=["GET", "POST"], defaults={"product_id": None})
@bp.route("/products/<int:product_id>/edit", methods=["GET", "POST"])
def product_form(product_id):
    db = get_db()
    product = services.get_product(db, product_id) if product_id else None
    form = dict(product) if product else {"default_license_type": "fixed", "default_term_days": 365}
    if request.method == "POST":
        form = request.form
        try:
            services.save_product(db, form, product_id)
            db.commit()
            flash("Product saved.", "success")
            return redirect(url_for("main.products"))
        except ValidationError as exc:
            db.rollback()
            flash_errors(exc)
    return render_template("products/form.html", form=form, product=product)


@bp.route("/products/<int:product_id>/delete", methods=["POST"])
def product_delete(product_id):
    return _delete("products", product_id, "product", "main.products")


def _delete(table, row_id, label, endpoint):
    db = get_db()
    try:
        services.delete_row(db, table, row_id, label)
        db.commit()
        flash(f"{label.capitalize()} deleted.", "success")
    except ValidationError as exc:
        db.rollback()
        flash_errors(exc)
    return redirect(url_for(endpoint))


# --------------------------------------------------------------------------- customers

@bp.route("/customers")
def customers():
    q = request.args.get("q", "").strip()[:200]
    return render_template("customers/list.html", customers=services.list_customers(get_db(), q), q=q)


@bp.route("/customers/<int:customer_id>")
def customer_detail(customer_id):
    db = get_db()
    customer = services.get_customer(db, customer_id)
    licenses, _ = services.search_licenses(db, customer_id=customer_id, limit=500)
    return render_template(
        "customers/detail.html", customer=customer, licenses=licenses,
        orders=services.list_orders(db, customer_id=customer_id),
    )


@bp.route("/customers/new", methods=["GET", "POST"], defaults={"customer_id": None})
@bp.route("/customers/<int:customer_id>/edit", methods=["GET", "POST"])
def customer_form(customer_id):
    db = get_db()
    customer = services.get_customer(db, customer_id) if customer_id else None
    form = dict(customer) if customer else {}
    if request.method == "POST":
        form = request.form
        try:
            new_id = services.save_customer(db, form, customer_id)
            db.commit()
            flash("Customer saved.", "success")
            return redirect(url_for("main.customer_detail", customer_id=new_id))
        except ValidationError as exc:
            db.rollback()
            flash_errors(exc)
    return render_template("customers/form.html", form=form, customer=customer)


@bp.route("/customers/<int:customer_id>/delete", methods=["POST"])
def customer_delete(customer_id):
    return _delete("customers", customer_id, "customer", "main.customers")


# --------------------------------------------------------------------------- orders

@bp.route("/orders")
def orders():
    db = get_db()
    a = request.args
    filters = {
        "q": a.get("q", "").strip()[:200],
        "order_status": a.get("order_status", "") if a.get("order_status") in services.ORDER_STATUSES else "",
        "payment_status": a.get("payment_status", "") if a.get("payment_status") in services.PAYMENT_STATUSES else "",
        "delivery": a.get("delivery", "") if a.get("delivery") in (*services.ORDER_DELIVERY_STATES, "outstanding") else "",
        "product_id": services.parse_id(a.get("product_id")),
        "sort": a.get("sort", "newest") if a.get("sort") in services.ORDER_SORTS else "newest",
    }
    errors = []
    for key in ("date_from", "date_to"):
        filters[key] = services.parse_date(a.get(key), key.replace("_", " ").capitalize(), errors) or ""
    for message in errors:
        flash(message, "error")
    return render_template("orders/list.html", orders=services.list_orders(db, **filters), filters=filters,
                           products=services.list_products(db), q=filters["q"])


@bp.route("/orders/<int:order_id>")
def order_detail(order_id):
    db = get_db()
    order = services.get_order(db, order_id)
    licenses, _ = services.search_licenses(db, order_id=order_id, limit=500)
    return render_template(
        "orders/detail.html", order=order, licenses=licenses, items=services.list_order_items(db, order_id),
        events=services.order_history(db, order_id), products=services.list_products(db),
    )


def _order_action(order_id, action, success):
    db = get_db()
    try:
        warning = action(db)
        db.commit()
        flash(success, "success")
        if warning:
            flash(warning, "error")
    except ValidationError as exc:
        db.rollback()
        flash_errors(exc)
    return redirect(url_for("main.order_detail", order_id=order_id))


@bp.route("/orders/<int:order_id>/items", methods=["POST"])
def order_item_set(order_id):
    f = request.form
    return _order_action(order_id, lambda db: services.set_order_item(db, order_id, f.get("product_id"),
                                                                      f.get("quantity")), "Order items updated.")


@bp.route("/orders/<int:order_id>/items/<int:product_id>/delete", methods=["POST"])
def order_item_remove(order_id, product_id):
    return _order_action(order_id, lambda db: services.remove_order_item(db, order_id, product_id),
                         "Item removed.")


@bp.route("/orders/<int:order_id>/status", methods=["POST"])
def order_status(order_id):
    f = request.form
    return _order_action(order_id, lambda db: services.set_order_status(db, order_id, f.get("status", ""),
                                                                        f.get("reason", "")), "Order status updated.")


@bp.route("/orders/<int:order_id>/payment", methods=["POST"])
def order_payment(order_id):
    f = request.form
    return _order_action(
        order_id,
        lambda db: services.set_payment_status(db, order_id, f.get("payment_status", ""), f.get("reference", ""),
                                               f.get("note", "")),
        "Payment status recorded.",
    )


@bp.route("/orders/new", methods=["GET", "POST"], defaults={"order_id": None})
@bp.route("/orders/<int:order_id>/edit", methods=["GET", "POST"])
def order_form(order_id):
    db = get_db()
    order = services.get_order(db, order_id) if order_id else None
    form = dict(order) if order else {
        "order_date": services.get_today().isoformat(), "customer_id": request.args.get("customer_id", ""),
    }
    if request.method == "POST":
        form = request.form
        try:
            new_id = services.save_order(db, form, order_id)
            db.commit()
            flash("Order saved.", "success")
            return redirect(url_for("main.order_detail", order_id=new_id))
        except ValidationError as exc:
            db.rollback()
            flash_errors(exc)
    return render_template("orders/form.html", form=form, order=order, customers=services.list_customers(db),
                           products=services.list_products(db))


@bp.route("/orders/<int:order_id>/delete", methods=["POST"])
def order_delete(order_id):
    return _delete("orders", order_id, "order", "main.orders")


# --------------------------------------------------------------------------- reminder templates

@bp.route("/templates")
def templates():
    return render_template(
        "reminders/list.html", templates=services.list_templates(get_db()), placeholders=services.PLACEHOLDERS
    )


@bp.route("/templates/new", methods=["GET", "POST"], defaults={"template_id": None})
@bp.route("/templates/<int:template_id>/edit", methods=["GET", "POST"])
def template_form(template_id):
    db = get_db()
    tpl = services.get_template(db, template_id) if template_id else None
    form = dict(tpl) if tpl else {}
    if request.method == "POST":
        form = request.form
        try:
            services.save_template(db, form, template_id)
            db.commit()
            flash("Template saved.", "success")
            return redirect(url_for("main.templates"))
        except ValidationError as exc:
            db.rollback()
            flash_errors(exc)
    return render_template("reminders/form.html", form=form, tpl=tpl, placeholders=services.PLACEHOLDERS)


@bp.route("/templates/<int:template_id>/delete", methods=["POST"])
def template_delete(template_id):
    db = get_db()
    services.delete_template(db, template_id)
    db.commit()
    flash("Template deleted.", "success")
    return redirect(url_for("main.templates"))


# --------------------------------------------------------------------------- import / export

@bp.route("/data")
def data():
    return render_template("data.html", exports=csv_io.EXPORTS, columns=csv_io.IMPORT_COLUMNS,
                           required=csv_io.REQUIRED_COLUMNS, report=None)


@bp.route("/data/import", methods=["POST"])
def data_import():
    upload = request.files.get("file")
    if not upload or not upload.filename:
        flash("Choose a CSV file to import.", "error")
        return redirect(url_for("main.data"))
    if not upload.filename.lower().endswith(".csv"):
        flash("Only .csv files are supported.", "error")
        return redirect(url_for("main.data"))
    try:
        text = upload.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        flash("The file must be UTF-8 encoded CSV.", "error")
        return redirect(url_for("main.data"))
    report = csv_io.import_licenses(get_db(), text, dry_run=request.form.get("mode") != "import")
    current_app.logger.info("CSV import: %s", csv_io.import_summary(report))
    html = render_template("data.html", exports=csv_io.EXPORTS, columns=csv_io.IMPORT_COLUMNS,
                           required=csv_io.REQUIRED_COLUMNS, report=report,
                           summary=csv_io.import_summary(report))
    return secret_response(html)


@bp.route("/data/export/<entity>.csv")
def data_export(entity):
    if entity not in csv_io.EXPORTS:
        abort(404)
    body = csv_io.export_csv(get_db(), entity)
    stamp = datetime.now().strftime("%Y%m%d")
    return Response(
        body, mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="licenseledger-{entity}-{stamp}.csv"'},
    )


# Everything the account holds, for "Export all my data". Full license keys are never
# stored, so they can't be exported: licenses carry only the 4-character hint.
EXPORT_ALL_EXTRA = {
    "order_items": (
        ["order_id", "order_ref", "product_name", "quantity", "created_at"],
        "SELECT i.order_id, o.order_ref, p.name AS product_name, i.quantity, i.created_at FROM order_items i "
        "JOIN orders o ON o.id = i.order_id JOIN products p ON p.id = i.product_id "
        "WHERE i.account_id = ? ORDER BY i.id",
    ),
    "history": (
        ["event_id", "license_id", "entity_type", "entity_id", "action", "detail", "created_at"],
        "SELECT id AS event_id, license_id, entity_type, entity_id, action, detail, created_at FROM audit_events "
        "WHERE account_id = ? ORDER BY id",
    ),
    "reminder_templates": (
        ["template_id", "name", "subject", "body", "created_at"],
        "SELECT id AS template_id, name, subject, body, created_at FROM reminder_templates "
        "WHERE account_id = ? ORDER BY id",
    ),
}


@bp.route("/data/export-all.zip")
def data_export_all():
    """A ZIP of CSV files with all of the signed-in account's data (and nobody else's)."""
    db = get_db()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        account = g.account
        zf.writestr("account.csv", csv_io.rows_to_csv(
            ["business_name", "email", "created_at", "paid_until"],
            [[account["business_name"], account["email"], account["created_at"], account["paid_until"] or ""]]))
        for entity in csv_io.EXPORTS:
            zf.writestr(f"{entity}.csv", csv_io.export_csv(db, entity))
        for name, (headers, sql) in EXPORT_ALL_EXTRA.items():
            rows = [[r[h] for h in headers] for r in db.execute(sql, (services.acct(),))]
            zf.writestr(f"{name}.csv", csv_io.rows_to_csv(headers, rows))
    stamp = datetime.now().strftime("%Y%m%d")
    return Response(
        buf.getvalue(), mimetype="application/zip",
        headers={"Content-Disposition": f'attachment; filename="licenseledger-all-data-{stamp}.zip"',
                 "Cache-Control": "no-store"},
    )


@bp.route("/data/import-template.csv")
def import_template():
    return Response(
        csv_io.import_template_csv(), mimetype="text/csv",
        headers={"Content-Disposition": 'attachment; filename="licenseledger-import-template.csv"'},
    )


# --------------------------------------------------------------------------- settings & demo

@bp.route("/settings", methods=["GET", "POST"])
def settings():
    db = get_db()
    account_id = services.acct()
    if request.method == "POST":
        action = request.form.get("action")
        row = db.execute("SELECT password_hash FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if action == "business":
            errors = []
            name = validate_business_name(request.form.get("business_name", ""), errors)
            for e in errors:
                flash(e, "error")
            if not errors:
                db.execute("UPDATE accounts SET business_name = ? WHERE id = ?", (name, account_id))
                db.commit()
                flash("Business name saved.", "success")
        elif action == "email":
            errors = []
            email = validate_account_email(request.form.get("email", ""), errors)
            if not check_password_hash(row["password_hash"], request.form.get("current", "")):
                errors.insert(0, "Current password is incorrect.")
            elif not errors and db.execute("SELECT 1 FROM accounts WHERE email = ? AND id != ?",
                                           (email, account_id)).fetchone():
                errors.append("Another account already uses that email.")
            for e in errors:
                flash(e, "error")
            if not errors:
                db.execute("UPDATE accounts SET email = ? WHERE id = ?", (email, account_id))
                db.commit()
                flash("Login email changed.", "success")
        elif action == "password":
            if not check_password_hash(row["password_hash"], request.form.get("current", "")):
                flash("Current password is incorrect.", "error")
            else:
                errors = validate_new_password(request.form.get("password", ""), request.form.get("confirm", ""))
                for e in errors:
                    flash(e, "error")
                if not errors:
                    db.execute("UPDATE accounts SET password_hash = ? WHERE id = ?",
                               (generate_password_hash(request.form["password"]), account_id))
                    session["auth_version"] = bump_auth_version(db, account_id)
                    db.commit()
                    flash("Password changed. Any other signed-in sessions have been signed out.", "success")
        return redirect(url_for("main.settings"))
    account = db.execute("SELECT business_name, email FROM accounts WHERE id = ?", (account_id,)).fetchone()
    return render_template("settings.html", business_name=account["business_name"], email=account["email"],
                           counts=services.account_counts(db))


@bp.route("/settings/reset", methods=["POST"])
def reset_data():
    action = request.form.get("action")
    if request.form.get("confirm", "").strip() != "RESET" or action not in ("demo", "empty"):
        flash('Type RESET (in capitals) to confirm. Nothing was changed.', "error")
        return redirect(url_for("main.settings"))
    db = get_db()
    db.commit()
    demo.backup_database(current_app.config["DATABASE"], current_app.config["BACKUP_DIR"], services.acct())
    try:
        if action == "demo":
            demo.load_demo(db)
        else:
            demo.clear_data(db)
        db.commit()
    except Exception:
        db.rollback()
        raise
    flash(
        ("Demo data loaded." if action == "demo" else "All business data cleared.")
        + " A backup copy of your previous data was saved first.",
        "success",
    )
    return redirect(url_for("main.dashboard"))
