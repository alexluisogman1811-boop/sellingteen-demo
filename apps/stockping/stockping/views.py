"""HTTP routes for the inventory itself.

Every route here except the public landing page needs a logged-in shop (see
require_login), and every service call gets ``account_db()``: a connection bound to that
shop's account, so a product or supplier id from another shop simply isn't found (404).
"""
import sqlite3
from datetime import date

from flask import (Blueprint, Response, abort, current_app, flash, g, redirect, render_template,
                   request, session, url_for)

from . import services as svc
from .db import account_db

bp = Blueprint("main", __name__)
PER_PAGE = 50
MOVEMENT_KINDS = {"in": "in", "out": "out", "adjust": "adjustment"}


# Pages anyone can open without logging in (everything else in this blueprint needs a login).
PUBLIC_ENDPOINTS = {"main.index"}


@bp.before_request
def require_login():
    if request.endpoint not in PUBLIC_ENDPOINTS and g.get("account") is None:
        # script_root keeps the address prefix when the app runs under one (e.g. /stockping)
        return redirect(url_for("auth.login",
                                next=(request.script_root + request.full_path).rstrip("?")))
    return None


@bp.app_context_processor
def nav_counts():
    """Number of products needing reorder, shown as a badge in the navigation."""
    if g.get("account") is None:
        return {"nav_attention": None}
    try:
        return {"nav_attention": svc.attention_count(account_db())}
    except sqlite3.Error:
        return {"nav_attention": None}


def _flash_price_warning(data):
    if data["price_cents"] < data["cost_cents"]:
        flash("Note: the selling price is below the unit cost.", "info")


@bp.app_template_global()
def page_url(page):
    args = request.args.to_dict()
    args["page"] = page
    return url_for(request.endpoint, **(request.view_args or {}), **args)


def _page():
    raw = request.args.get("page", "1")
    return max(int(raw), 1) if raw.isdigit() else 1


def _pages(total):
    return max(1, -(-total // PER_PAGE))


def _int_or_none(value):
    return int(value) if value and value.isdigit() else None


def _product_filters():
    return {
        "q": request.args.get("q", "").strip()[:100],
        "category": request.args.get("category", "").strip()[:80],
        "status": request.args.get("status", "") if request.args.get("status") in svc.STATUS_FILTERS else "",
        "supplier_id": _int_or_none(request.args.get("supplier")),
        "include_archived": request.args.get("archived") == "1",
    }


def _movement_filters():
    return {
        "q": request.args.get("q", "").strip()[:100],
        "kind": request.args.get("kind", "") if request.args.get("kind") in svc.MOVEMENT_LABELS else "",
        "date_from": request.args.get("from", "").strip()[:10],
        "date_to": request.args.get("to", "").strip()[:10],
        "product_id": _int_or_none(request.args.get("product")),
    }


def _product_or_404(product_id):
    product = svc.get_product(account_db(), product_id)
    if product is None:
        abort(404)
    return product


def _csv_response(text, filename):
    return Response(text, mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------

@bp.get("/")
def index():
    """Public landing page; a logged-in shop sees its dashboard here instead."""
    if g.get("account") is None:
        return render_template("landing.html", price=current_app.config["PRICE_TEXT"],
                               trial_days=current_app.config["TRIAL_DAYS"],
                               billing=current_app.config["BILLING_ENABLED"])
    return _dashboard()


@bp.get("/dashboard")
def dashboard():
    return _dashboard()


def _dashboard():
    db = account_db()
    recent, _ = svc.list_movements(db, limit=10)
    return render_template("dashboard.html", stats=svc.dashboard_stats(db), recent=recent,
                           attention=svc.reorder_list(db),
                           inconsistent=svc.find_inconsistencies(db), demo=svc.demo_summary(db))


# --------------------------------------------------------------------------
# Products
# --------------------------------------------------------------------------

@bp.get("/products")
def product_list():
    db = account_db()
    filters, page = _product_filters(), _page()
    products, total = svc.list_products(db, **filters, limit=PER_PAGE, offset=(page - 1) * PER_PAGE)
    return render_template("products/list.html", products=products, total=total, page=page,
                           pages=_pages(total), filters=filters, stats=svc.dashboard_stats(db),
                           categories=svc.list_categories(db), suppliers=svc.list_suppliers(db),
                           status_filters=svc.STATUS_FILTERS)


def _render_product_form(product, form, errors, status=200):
    db = account_db()
    return render_template("products/form.html", product=product, form=form, errors=errors,
                           suppliers=svc.list_suppliers(db), categories=svc.list_categories(db)), status


@bp.route("/products/new", methods=["GET", "POST"])
def product_new():
    if request.method == "POST":
        db = account_db()
        try:
            data = svc.validate_product_form(db, request.form)
            product_id = svc.create_product(db, data)
        except svc.ValidationError as exc:
            return _render_product_form(None, request.form, exc.errors, 422)
        flash(f"Product {data['sku']} created.", "success")
        _flash_price_warning(data)
        return redirect(url_for("main.product_detail", product_id=product_id))
    return _render_product_form(None, {"quantity": "0", "reorder_threshold": "5"}, {})


@bp.route("/products/<int:product_id>/edit", methods=["GET", "POST"])
def product_edit(product_id):
    product = _product_or_404(product_id)
    if request.method == "POST":
        db = account_db()
        try:
            data = svc.validate_product_form(db, request.form, product_id=product_id)
            svc.update_product(db, product_id, data)
        except svc.ValidationError as exc:
            return _render_product_form(product, request.form, exc.errors, 422)
        flash("Product updated.", "success")
        _flash_price_warning(data)
        return redirect(url_for("main.product_detail", product_id=product_id))
    form = {
        "sku": product["sku"], "name": product["name"], "category": product["category"],
        "supplier_id": str(product["supplier_id"] or ""),
        "cost": svc.money_str(product["cost_cents"]), "price": svc.money_str(product["price_cents"]),
        "reorder_threshold": str(product["reorder_threshold"]), "description": product["description"],
    }
    return _render_product_form(product, form, {})


@bp.get("/products/<int:product_id>")
def product_detail(product_id):
    product = _product_or_404(product_id)
    page = _page()
    movements, total = svc.list_movements(account_db(), product_id=product_id, limit=PER_PAGE,
                                          offset=(page - 1) * PER_PAGE)
    return render_template("products/detail.html", product=product, movements=movements,
                           total=total, page=page, pages=_pages(total))


@bp.post("/products/<int:product_id>/archive")
def product_archive(product_id):
    product = _product_or_404(product_id)
    svc.set_product_active(account_db(), product_id, False)
    flash(f"{product['sku']} archived. Its stock history is kept; you can restore it at any time.", "success")
    return redirect(url_for("main.product_detail", product_id=product_id))


@bp.post("/products/<int:product_id>/restore")
def product_restore(product_id):
    product = _product_or_404(product_id)
    svc.set_product_active(account_db(), product_id, True)
    flash(f"{product['sku']} restored.", "success")
    return redirect(url_for("main.product_detail", product_id=product_id))


@bp.post("/products/<int:product_id>/keep")
def product_keep(product_id):
    product = _product_or_404(product_id)
    svc.keep_demo_product(account_db(), product_id)
    flash(f"{product['sku']} is now a real product and will not be removed by demo resets.", "success")
    return redirect(url_for("main.product_detail", product_id=product_id))


@bp.route("/products/<int:product_id>/stock/<kind>", methods=["GET", "POST"])
def stock_movement(product_id, kind):
    if kind not in MOVEMENT_KINDS:
        abort(404)
    movement_kind = MOVEMENT_KINDS[kind]
    product = _product_or_404(product_id)
    form, errors, status = {"movement_date": date.today().isoformat()}, {}, 200
    if request.method == "POST":
        form = request.form.to_dict()
        try:
            m = svc.validate_movement_form(movement_kind, request.form)
            svc.apply_movement(account_db(), product_id, movement_kind, m["amount"],
                               m["movement_date"], m["reason"], m["notes"],
                               request_id=m["request_id"])
        except svc.ValidationError as exc:
            errors, status = exc.errors, 422
        except svc.DuplicateSubmission as exc:
            flash(str(exc), "info")
            return redirect(url_for("main.product_detail", product_id=product_id))
        except svc.StockError as exc:
            errors, status = {exc.field or "_form": str(exc)}, 422
            product = _product_or_404(product_id)   # show the current on-hand figure
        else:
            updated = _product_or_404(product_id)
            flash(f"{svc.MOVEMENT_LABELS[movement_kind]} recorded for {product['sku']}: "
                  f"{product['quantity']} → {updated['quantity']} on hand.", "success")
            if updated["status"] != "ok" and updated["status"] != product["status"]:
                flash(f"{product['sku']} is now {svc.STATUS_LABELS[updated['status']].lower()} "
                      f"(reorder threshold {updated['reorder_threshold']}).", "error")
            return redirect(url_for("main.product_detail", product_id=product_id))
    if not svc.REQUEST_ID_RE.match(form.get("request_id", "")):
        form["request_id"] = svc.new_request_id()
    reasons = {"in": svc.STOCK_IN_REASONS, "out": svc.STOCK_OUT_REASONS}.get(movement_kind, [])
    return render_template("products/stock.html", product=product, kind=kind,
                           movement_kind=movement_kind, form=form, errors=errors,
                           reasons=reasons, today=date.today().isoformat()), status


# --------------------------------------------------------------------------
# Movement history
# --------------------------------------------------------------------------

@bp.get("/movements")
def movement_list():
    filters, page = _movement_filters(), _page()
    movements, total = svc.list_movements(account_db(), **filters, limit=PER_PAGE,
                                          offset=(page - 1) * PER_PAGE)
    return render_template("movements.html", movements=movements, total=total, page=page,
                           pages=_pages(total), filters=filters)


# --------------------------------------------------------------------------
# Suppliers
# --------------------------------------------------------------------------

@bp.get("/suppliers")
def supplier_list():
    return render_template("suppliers/list.html", suppliers=svc.list_suppliers(account_db()))


@bp.route("/suppliers/new", methods=["GET", "POST"])
@bp.route("/suppliers/<int:supplier_id>/edit", methods=["GET", "POST"])
def supplier_form(supplier_id=None):
    db = account_db()
    supplier = None
    if supplier_id is not None:
        supplier = svc.get_supplier(db, supplier_id)
        if supplier is None:
            abort(404)
    form, errors, status = dict(supplier) if supplier else {}, {}, 200
    if request.method == "POST":
        form = request.form
        try:
            data = svc.validate_supplier_form(db, request.form, supplier_id=supplier_id)
            if supplier:
                svc.update_supplier(db, supplier_id, data)
            else:
                svc.create_supplier(db, data)
        except svc.ValidationError as exc:
            errors, status = exc.errors, 422
        else:
            flash(f"Supplier {data['name']} saved.", "success")
            return redirect(url_for("main.supplier_list"))
    return render_template("suppliers/form.html", supplier=supplier, form=form, errors=errors), status


@bp.post("/suppliers/<int:supplier_id>/delete")
def supplier_delete(supplier_id):
    db = account_db()
    supplier = svc.get_supplier(db, supplier_id)
    if supplier is None:
        abort(404)
    try:
        svc.delete_supplier(db, supplier_id)
    except svc.StockError as exc:
        flash(str(exc), "error")
    else:
        flash(f"Supplier {supplier['name']} deleted.", "success")
    return redirect(url_for("main.supplier_list"))


# --------------------------------------------------------------------------
# Import / export
# --------------------------------------------------------------------------

@bp.route("/import", methods=["GET", "POST"])
def import_upload():
    if request.method == "GET":
        return render_template("import/upload.html", error=None)
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return render_template("import/upload.html", error="Choose a CSV file to upload."), 422
    if not upload.filename.lower().endswith(".csv"):
        return render_template("import/upload.html", error="Only .csv files can be imported."), 422
    db = account_db()
    result = svc.parse_import(db, upload.read())
    token = svc.save_import_preview(db, result) if result["can_import"] else None
    session["import_token"] = token
    status = 200 if not result["file_errors"] and not result["counts"]["error"] else 422
    return render_template("import/preview.html", result=result, token=token,
                           filename=upload.filename), status


@bp.post("/import/confirm")
def import_confirm():
    token = request.form.get("token", "")
    if not token or token != session.get("import_token"):
        flash("That import preview is no longer available. Please upload the file again.", "error")
        return redirect(url_for("main.import_upload"))
    try:
        summary = svc.confirm_import(account_db(), token)
    except svc.StockError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.import_upload"))
    except sqlite3.Error:
        current_app.logger.exception("Import failed")
        flash("The import failed and was rolled back: no products, suppliers or stock were "
              "saved. Please upload the file again.", "error")
        return redirect(url_for("main.import_upload"))
    finally:
        session.pop("import_token", None)
    message = f"Imported {len(summary['created'])} product(s)."
    if summary["skipped"]:
        shown = ", ".join(summary["skipped"][:10]) + ("…" if len(summary["skipped"]) > 10 else "")
        message += f" Skipped {len(summary['skipped'])} existing SKU(s): {shown}."
    if summary["new_suppliers"]:
        message += " New suppliers: " + ", ".join(summary["new_suppliers"]) + "."
    flash(message, "success")
    return redirect(url_for("main.product_list"))


@bp.get("/import/template.csv")
def import_template():
    sample = svc.to_csv(svc.PRODUCT_COLUMNS, [
        ["ABC-001", "Example product", "General", "Example Supplier", "4.50", "9.99", "25", "5",
         "Optional description"]])
    return _csv_response(sample, "stockping-import-template.csv")


@bp.get("/export/products.csv")
def export_products():
    text = svc.export_products_csv(account_db(), **_product_filters())
    return _csv_response(text, f"stockping-products-{date.today().isoformat()}.csv")


@bp.get("/export/movements.csv")
def export_movements():
    text = svc.export_movements_csv(account_db(), **_movement_filters())
    return _csv_response(text, f"stockping-movements-{date.today().isoformat()}.csv")


# --------------------------------------------------------------------------
# Reorder list and demo data
# --------------------------------------------------------------------------

@bp.get("/reorder")
def reorder():
    return render_template("reorder.html", items=svc.reorder_list(account_db()),
                           today=date.today().isoformat())


@bp.get("/demo")
def demo():
    return render_template("demo.html", demo=svc.demo_summary(account_db()))


@bp.post("/demo/<action>")
def demo_action(action):
    db = account_db()
    if action not in ("load", "reset", "remove"):
        abort(404)
    if action != "load" and request.form.get("confirm") != "yes":
        flash("Tick the confirmation box to continue.", "error")
        return redirect(url_for("main.demo"))
    try:
        if action == "load":
            loaded = svc.load_demo_data(db)
            removed = None
        elif action == "reset":
            result = svc.reset_demo_data(db)
            loaded, removed = result["loaded"], result["removed"]
        else:
            loaded, removed = None, svc.remove_demo_data(db)
    except svc.StockError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.demo"))
    parts = []
    if removed:
        parts.append(f"Removed {removed['products']} demo product(s), {removed['movements']} "
                     f"movement(s) and {removed['suppliers']} demo supplier(s).")
        if removed["kept_suppliers"]:
            parts.append("Kept supplier(s) used by your real products: "
                         + ", ".join(removed["kept_suppliers"]) + ".")
    if loaded:
        parts.append(f"Loaded {loaded['created']} demo product(s).")
        if loaded["skipped"]:
            parts.append("Skipped SKU(s) already used by your records: " + ", ".join(loaded["skipped"]) + ".")
    flash(" ".join(parts) or "Done.", "success")
    return redirect(url_for("main.demo"))
