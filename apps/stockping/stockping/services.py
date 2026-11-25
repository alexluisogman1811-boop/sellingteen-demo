"""Inventory rules for StockPing.

Every function takes an ``AccountDB``: a database connection bound to one shop's
account (see db.py). Every query filters on ``db.account_id``, so one shop can never
read or change another shop's products, suppliers, movements or imports. Passing a
plain connection fails immediately instead of silently reaching all accounts.

Stock quantities change only through ``apply_movement`` or the opening-stock movement
written when a product is created, which keeps ``products.quantity`` equal to the sum
of that product's recorded movements.
"""
import csv
import io
import json
import re
import secrets
import sqlite3
import zipfile
from datetime import date, timedelta
from decimal import Decimal

from .db import transaction

MAX_QUANTITY = 1_000_000          # largest single movement / opening quantity
MAX_STOCK = 1_000_000_000         # largest on-hand quantity
MAX_MONEY = Decimal("10000000")   # largest unit cost or price
MAX_IMPORT_ROWS = 5000
PREVIEW_TTL = "-2 hours"

SKU_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}$")
INT_RE = re.compile(r"^[0-9]{1,12}(\.0+)?$")   # "10.0" (spreadsheet style) is a whole number
ID_RE = re.compile(r"^[0-9]{1,12}$")
MONEY_RE = re.compile(r"^(?=\.?[0-9])[0-9]{0,12}(\.[0-9]{1,2})?$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

STATUS_SQL = ("CASE WHEN p.quantity = 0 THEN 'out' "
              "WHEN p.quantity <= p.reorder_threshold THEN 'low' ELSE 'ok' END")
STATUS_LABELS = {"ok": "In stock", "low": "Low stock", "out": "Out of stock"}
STATUS_FILTERS = {"ok": "In stock", "low": "Low stock", "out": "Out of stock",
                  "attention": "Low or out of stock"}
MOVEMENT_LABELS = {"initial": "Opening stock", "in": "Stock in", "out": "Stock out",
                   "adjustment": "Adjustment"}

STOCK_IN_REASONS = ["Purchase received", "Customer return", "Transfer in", "Production", "Other"]
STOCK_OUT_REASONS = ["Sale", "Damaged", "Expired", "Sample / promotion", "Transfer out",
                     "Internal use", "Other"]

PRODUCT_COLUMNS = ["sku", "name", "category", "supplier", "cost", "price", "quantity",
                   "reorder_threshold", "description"]
REQUIRED_IMPORT_COLUMNS = ["sku", "name", "cost", "price", "quantity", "reorder_threshold"]
HEADER_ALIASES = {
    "product_sku": "sku", "product_name": "name", "product": "name",
    "supplier_name": "supplier", "cost_price": "cost", "unit_cost": "cost",
    "selling_price": "price", "sale_price": "price", "unit_price": "price",
    "qty": "quantity", "current_quantity": "quantity", "stock": "quantity",
    "reorder_level": "reorder_threshold", "reorder_point": "reorder_threshold",
    "threshold": "reorder_threshold",
}


class ValidationError(Exception):
    """User input failed validation. ``errors`` maps field name -> message."""

    def __init__(self, errors):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


class StockError(Exception):
    """An operation would break an inventory rule (e.g. negative stock)."""

    def __init__(self, message, field=None):
        super().__init__(message)
        self.field = field


class DuplicateSubmission(StockError):
    """The same stock form was submitted twice; the first one was already recorded."""


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

def _text(raw, key):
    value = raw.get(key)
    return "" if value is None else str(value).strip()


def parse_int(value, label, *, minimum=0, maximum=MAX_QUANTITY):
    s = ("" if value is None else str(value)).strip()
    if not s:
        raise ValueError(f"{label} is required.")
    if s.startswith("-"):
        raise ValueError(f"{label} cannot be negative.")
    if not INT_RE.match(s):
        raise ValueError(f"{label} must be a whole number (no decimals, letters or symbols).")
    n = int(s.split(".")[0])
    if n < minimum:
        raise ValueError(f"{label} must be at least {minimum}.")
    if n > maximum:
        raise ValueError(f"{label} must be at most {maximum:,}.")
    return n


def parse_money(value, label):
    """Return a non-negative amount as integer cents."""
    s = ("" if value is None else str(value)).strip()
    if not s:
        raise ValueError(f"{label} is required.")
    if s.startswith("-"):
        raise ValueError(f"{label} cannot be negative.")
    if not MONEY_RE.match(s):
        raise ValueError(f"{label} must be a number with at most 2 decimals, e.g. 12.50 "
                         "(no currency symbols or thousands separators).")
    amount = Decimal(s)
    if amount > MAX_MONEY:
        raise ValueError(f"{label} must be at most {MAX_MONEY:,}.")
    return int(amount * 100)


def parse_date(value, label="Date"):
    s = ("" if value is None else str(value)).strip()
    if not s:
        raise ValueError(f"{label} is required.")
    if not DATE_RE.match(s):
        raise ValueError(f"{label} must be in YYYY-MM-DD format.")
    try:
        d = date.fromisoformat(s)
    except ValueError:
        raise ValueError(f"{label} is not a valid calendar date.") from None
    if d > date.today():
        raise ValueError(f"{label} cannot be in the future.")
    if d.year < 2000:
        raise ValueError(f"{label} must be in the year 2000 or later.")
    return d.isoformat()


def money_str(cents):
    """Plain decimal string for forms and CSV, e.g. 1250 -> '12.50'."""
    return f"{cents // 100}.{cents % 100:02d}"


def _limit(errors, field, value, label, max_len, required=False):
    if required and not value:
        errors[field] = f"{label} is required."
    elif len(value) > max_len:
        errors[field] = f"{label} must be at most {max_len} characters."


# --------------------------------------------------------------------------
# Products
# --------------------------------------------------------------------------

def unescape_formula(value):
    """Undo the apostrophe that CSV export adds before =, +, -, @ so an
    export -> import round trip keeps text exactly as it was."""
    if len(value) > 1 and value[0] == "'" and value[1] in FORMULA_PREFIXES:
        return value[1:]
    return value


def canonical_category(db, category, exclude_id=None):
    """Reuse the existing spelling of a category that differs only in case."""
    if not category:
        return category
    row = db.execute("SELECT category FROM products WHERE account_id = ? "
                     "AND category = ? COLLATE NOCASE AND id IS NOT ? "
                     "GROUP BY category ORDER BY COUNT(*) DESC LIMIT 1",
                     (db.account_id, category, exclude_id)).fetchone()
    return row[0] if row else category


def clean_product_fields(raw, *, include_quantity, from_csv=False):
    """Validate the product fields shared by the form and CSV import.

    Returns ``(data, errors)``; ``data`` holds only the fields that passed."""
    data, errors = {}, {}
    sku = _text(raw, "sku")
    if not sku:
        errors["sku"] = "SKU is required."
    elif not SKU_RE.match(sku):
        errors["sku"] = ("SKU must be 1-64 characters (letters, digits, '.', '_', '-', '/'), "
                         "starting with a letter or digit.")
    data["sku"] = sku

    for field, label, max_len, required in (("name", "Name", 200, True),
                                            ("category", "Category", 80, False),
                                            ("description", "Description", 2000, False)):
        value = _text(raw, field)
        if from_csv:
            value = unescape_formula(value)
        if field != "description":
            value = " ".join(value.split())   # collapse stray internal whitespace
        _limit(errors, field, value, label, max_len, required)
        data[field] = value

    for field, label in (("cost", "Cost"), ("price", "Selling price")):
        try:
            data[f"{field}_cents"] = parse_money(raw.get(field), label)
        except ValueError as exc:
            errors[field] = str(exc)

    numbers = [("reorder_threshold", "Reorder threshold")]
    if include_quantity:
        numbers.insert(0, ("quantity", "Quantity"))
    for field, label in numbers:
        try:
            data[field] = parse_int(raw.get(field), label)
        except ValueError as exc:
            errors[field] = str(exc)
    return data, errors


def _sku_owner(db, sku, exclude_id=None):
    return db.execute(
        "SELECT id, name, is_active FROM products WHERE account_id = ? AND sku = ? AND id IS NOT ?",
        (db.account_id, sku, exclude_id)).fetchone()


def _duplicate_sku_message(owner, sku):
    archived = " (archived)" if not owner["is_active"] else ""
    return f"SKU '{sku}' is already used by '{owner['name']}'{archived}."


def validate_product_form(db, form, *, product_id=None):
    creating = product_id is None
    data, errors = clean_product_fields(form, include_quantity=creating)
    supplier_raw = _text(form, "supplier_id")
    data["supplier_id"] = None
    if supplier_raw:
        if not ID_RE.match(supplier_raw) or db.execute(
                "SELECT 1 FROM suppliers WHERE id = ? AND account_id = ?",
                (int(supplier_raw), db.account_id)).fetchone() is None:
            errors["supplier_id"] = "Choose a supplier from the list."
        else:
            data["supplier_id"] = int(supplier_raw)
    if "category" not in errors:
        data["category"] = canonical_category(db, data["category"], exclude_id=product_id)
    if "sku" not in errors:
        owner = _sku_owner(db, data["sku"], product_id)
        if owner:
            errors["sku"] = _duplicate_sku_message(owner, data["sku"])
    if errors:
        raise ValidationError(errors)
    return data


def _insert_product(db, data, *, source, is_demo, movement_date):
    qty = data.get("quantity", 0)
    cur = db.execute(
        """INSERT INTO products (account_id, sku, name, category, supplier_id, cost_cents,
                                 price_cents, quantity, reorder_threshold, description, is_demo)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (db.account_id, data["sku"], data["name"], data.get("category", ""), data.get("supplier_id"),
         data["cost_cents"], data["price_cents"], qty, data["reorder_threshold"],
         data.get("description", ""), int(is_demo)))
    product_id = cur.lastrowid
    if qty:
        db.execute(
            """INSERT INTO stock_movements (account_id, product_id, movement_type,
                   quantity_change, quantity_before, quantity_after, movement_date, reason, source)
               VALUES (?, ?, 'initial', ?, 0, ?, ?, 'Opening stock', ?)""",
            (db.account_id, product_id, qty, qty, movement_date or date.today().isoformat(),
             source))
    return product_id


def create_product(db, data, *, source="manual", is_demo=False, movement_date=None):
    try:
        with transaction(db):
            return _insert_product(db, data, source=source, is_demo=is_demo,
                                   movement_date=movement_date)
    except sqlite3.IntegrityError as exc:
        if "UNIQUE" in str(exc):
            raise ValidationError({"sku": f"SKU '{data['sku']}' already exists."}) from None
        raise


def update_product(db, product_id, data):
    """Update descriptive fields. Quantity is deliberately not editable here."""
    try:
        with transaction(db):
            cur = db.execute(
                """UPDATE products SET sku = ?, name = ?, category = ?, supplier_id = ?,
                       cost_cents = ?, price_cents = ?, reorder_threshold = ?, description = ?,
                       updated_at = datetime('now')
                   WHERE id = ? AND account_id = ?""",
                (data["sku"], data["name"], data["category"], data["supplier_id"],
                 data["cost_cents"], data["price_cents"], data["reorder_threshold"],
                 data["description"], product_id, db.account_id))
            if cur.rowcount != 1:
                raise StockError("Product not found.")
    except sqlite3.IntegrityError as exc:
        if "UNIQUE" in str(exc):
            raise ValidationError({"sku": f"SKU '{data['sku']}' already exists."}) from None
        raise


def set_product_active(db, product_id, active):
    with transaction(db):
        db.execute("UPDATE products SET is_active = ?, updated_at = datetime('now') "
                   "WHERE id = ? AND account_id = ?", (int(bool(active)), product_id, db.account_id))


def keep_demo_product(db, product_id):
    """Turn a demo product into a real one so demo resets leave it alone."""
    with transaction(db):
        db.execute("UPDATE products SET is_demo = 0, updated_at = datetime('now') "
                   "WHERE id = ? AND account_id = ?", (product_id, db.account_id))
        db.execute("""UPDATE suppliers SET is_demo = 0 WHERE is_demo = 1 AND account_id = ? AND id =
                      (SELECT supplier_id FROM products WHERE id = ? AND account_id = ?)""",
                   (db.account_id, product_id, db.account_id))


PRODUCT_SELECT = f"""
    SELECT p.*, s.name AS supplier_name, {STATUS_SQL} AS status
    FROM products p LEFT JOIN suppliers s ON s.id = p.supplier_id AND s.account_id = p.account_id
"""


def get_product(db, product_id):
    return db.execute(PRODUCT_SELECT + " WHERE p.id = ? AND p.account_id = ?",
                      (product_id, db.account_id)).fetchone()


def _like(term):
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _product_where(account_id, q="", category="", status="", supplier_id=None,
                   include_archived=False):
    where, params = ["p.account_id = ?"], [account_id]
    if not include_archived:
        where.append("p.is_active = 1")
    if q:
        where.append("(fold(p.name) LIKE fold(?) ESCAPE '\\' OR fold(p.sku) LIKE fold(?) ESCAPE '\\')")
        params += [_like(q), _like(q)]
    if category:
        where.append("p.category = ?")
        params.append(category)
    if status == "attention":
        where.append("p.quantity <= p.reorder_threshold")
    elif status in STATUS_LABELS:
        where.append(f"{STATUS_SQL} = ?")
        params.append(status)
    if supplier_id:
        where.append("p.supplier_id = ?")
        params.append(supplier_id)
    return " WHERE " + " AND ".join(where), params


def list_products(db, *, q="", category="", status="", supplier_id=None,
                  include_archived=False, limit=None, offset=0):
    where, params = _product_where(db.account_id, q, category, status, supplier_id,
                                   include_archived)
    total = db.execute(f"SELECT COUNT(*) FROM products p{where}", params).fetchone()[0]
    sql = PRODUCT_SELECT + where + " ORDER BY p.name COLLATE NOCASE, p.sku"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params = params + [limit, offset]
    return db.execute(sql, params).fetchall(), total


def list_categories(db):
    return [r[0] for r in db.execute(
        "SELECT DISTINCT category FROM products WHERE account_id = ? AND category != '' "
        "ORDER BY category COLLATE NOCASE", (db.account_id,))]


def dashboard_stats(db):
    row = db.execute(
        """SELECT COUNT(*) AS active,
                  COALESCE(SUM(quantity = 0), 0) AS out_count,
                  COALESCE(SUM(quantity > 0 AND quantity <= reorder_threshold), 0) AS low_count,
                  COALESCE(SUM(quantity > reorder_threshold), 0) AS ok_count,
                  COALESCE(SUM(quantity), 0) AS units,
                  COALESCE(SUM(quantity * cost_cents), 0) AS value_cents
           FROM products WHERE account_id = ? AND is_active = 1""", (db.account_id,)).fetchone()
    stats = dict(row)
    stats["attention"] = stats["low_count"] + stats["out_count"]
    return stats


def attention_count(db):
    """Active products at or below their reorder threshold (low or out of stock)."""
    return db.execute("SELECT COUNT(*) FROM products WHERE account_id = ? "
                      "AND is_active = 1 AND quantity <= reorder_threshold",
                      (db.account_id,)).fetchone()[0]


def reorder_list(db):
    """Active products at or below their reorder threshold (includes out of stock).

    Suggested order quantity refills to twice the threshold (minimum 1)."""
    rows = db.execute(
        PRODUCT_SELECT + """ WHERE p.account_id = ? AND p.is_active = 1
          AND p.quantity <= p.reorder_threshold
        ORDER BY s.name IS NULL, s.name COLLATE NOCASE, p.quantity = 0 DESC, p.sku""",
        (db.account_id,)).fetchall()
    result = []
    for r in rows:
        item = dict(r)
        item["supplier_name"] = r["supplier_name"] or ""
        item["suggested"] = max(2 * r["reorder_threshold"] - r["quantity"], 1)
        result.append(item)
    return result


def find_inconsistencies(db):
    """Products whose on-hand quantity differs from the sum of their movements."""
    return db.execute(
        """SELECT p.id, p.sku, p.name, p.quantity,
                  COALESCE(SUM(m.quantity_change), 0) AS ledger_quantity
           FROM products p LEFT JOIN stock_movements m
             ON m.product_id = p.id AND m.account_id = p.account_id
           WHERE p.account_id = ?
           GROUP BY p.id HAVING p.quantity != ledger_quantity""", (db.account_id,)).fetchall()


# --------------------------------------------------------------------------
# Stock movements
# --------------------------------------------------------------------------

def validate_movement_form(kind, form):
    """Validate a stock-in/out/adjustment form. Returns a dict for apply_movement."""
    errors, result = {}, {}
    if kind == "adjustment":
        try:
            result["amount"] = parse_int(form.get("counted_quantity"), "Counted quantity",
                                         maximum=MAX_STOCK)
        except ValueError as exc:
            errors["counted_quantity"] = str(exc)
        reason = _text(form, "reason")
        if len(reason) < 3:
            errors["reason"] = "A reason of at least 3 characters is required for adjustments."
        elif len(reason) > 200:
            errors["reason"] = "Reason must be at most 200 characters."
    else:
        try:
            result["amount"] = parse_int(form.get("quantity"), "Quantity", minimum=1)
        except ValueError as exc:
            errors["quantity"] = str(exc)
        reason = _text(form, "reason")
        allowed = STOCK_IN_REASONS if kind == "in" else STOCK_OUT_REASONS
        if reason not in allowed:
            errors["reason"] = "Choose a reason from the list."
    result["reason"] = reason
    notes = _text(form, "notes")
    if len(notes) > 1000:
        errors["notes"] = "Notes must be at most 1000 characters."
    elif kind != "adjustment" and reason == "Other" and not notes:
        errors["notes"] = "Add a note explaining the movement when the reason is 'Other'."
    result["notes"] = notes
    try:
        result["movement_date"] = parse_date(form.get("movement_date"))
    except ValueError as exc:
        errors["movement_date"] = str(exc)
    request_id = _text(form, "request_id")
    result["request_id"] = request_id if REQUEST_ID_RE.match(request_id) else None
    if errors:
        raise ValidationError(errors)
    return result


def new_request_id():
    return secrets.token_urlsafe(18)


def apply_movement(db, product_id, kind, amount, movement_date, reason, notes="", source="manual",
                   request_id=None):
    """Record a stock movement and update the on-hand quantity atomically.

    kind 'in' / 'out': ``amount`` is the number of units moved (>= 1).
    kind 'adjustment': ``amount`` is the physically counted quantity (>= 0); the
    difference from the recorded quantity is logged. Stock never goes negative.
    ``request_id`` is the one-time token from the form: a second submission with
    the same token raises DuplicateSubmission instead of moving stock twice.
    """
    if kind not in ("in", "out", "adjustment"):
        raise ValueError(f"unknown movement kind {kind!r}")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0 \
            or (kind != "adjustment" and amount < 1):
        raise StockError("Quantity must be a positive whole number.", "quantity")
    with transaction(db):
        if request_id and db.execute("SELECT 1 FROM stock_movements WHERE request_id = ?",
                                     (request_id,)).fetchone():
            raise DuplicateSubmission("This form was already submitted, so the repeat was ignored "
                                      "and stock was not changed again. To record another "
                                      "movement, open the stock form again.")
        row = db.execute("SELECT quantity, is_active FROM products WHERE id = ? AND account_id = ?",
                         (product_id, db.account_id)).fetchone()
        if row is None:
            raise StockError("Product not found.")
        if not row["is_active"]:
            raise StockError("This product is archived. Restore it before recording stock movements.")
        before = row["quantity"]
        if kind == "in":
            change = amount
            if before + change > MAX_STOCK:
                raise StockError(f"On-hand quantity cannot exceed {MAX_STOCK:,}.", "quantity")
        elif kind == "out":
            if amount > before:
                raise StockError(
                    f"Cannot remove {amount}: only {before} in stock. Stock cannot go negative. "
                    "If the recorded quantity is wrong, record a stock adjustment instead.",
                    "quantity")
            change = -amount
        else:
            change = amount - before
            if change == 0:
                raise StockError("The counted quantity matches the recorded quantity; "
                                 "there is nothing to adjust.", "counted_quantity")
        after = before + change
        cur = db.execute(
            "UPDATE products SET quantity = ?, updated_at = datetime('now') "
            "WHERE id = ? AND account_id = ? AND quantity = ?",
            (after, product_id, db.account_id, before))
        if cur.rowcount != 1:
            raise StockError("Stock changed while saving. Please try again.")
        return db.execute(
            """INSERT INTO stock_movements (account_id, product_id, movement_type,
                   quantity_change, quantity_before, quantity_after, movement_date, reason, notes,
                   source, request_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (db.account_id, product_id, kind, change, before, after, movement_date, reason, notes, source,
             request_id)
        ).lastrowid


def _movement_where(account_id, product_id=None, kind="", q="", date_from="", date_to=""):
    where, params = ["m.account_id = ?"], [account_id]
    if product_id:
        where.append("m.product_id = ?")
        params.append(product_id)
    if kind in MOVEMENT_LABELS:
        where.append("m.movement_type = ?")
        params.append(kind)
    if q:
        where.append("(fold(p.name) LIKE fold(?) ESCAPE '\\' OR fold(p.sku) LIKE fold(?) ESCAPE '\\')")
        params += [_like(q), _like(q)]
    if date_from and DATE_RE.match(date_from):
        where.append("m.movement_date >= ?")
        params.append(date_from)
    if date_to and DATE_RE.match(date_to):
        where.append("m.movement_date <= ?")
        params.append(date_to)
    return " WHERE " + " AND ".join(where), params


def list_movements(db, *, product_id=None, kind="", q="", date_from="", date_to="",
                   limit=None, offset=0):
    where, params = _movement_where(db.account_id, product_id, kind, q, date_from, date_to)
    base = (" FROM stock_movements m JOIN products p"
            " ON p.id = m.product_id AND p.account_id = m.account_id" + where)
    total = db.execute("SELECT COUNT(*)" + base, params).fetchone()[0]
    sql = ("SELECT m.*, p.sku, p.name AS product_name" + base +
           # Recording order, so each row's "before" is the previous row's "after"
           # even when a movement is back-dated.
           " ORDER BY m.id DESC")
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params = params + [limit, offset]
    return db.execute(sql, params).fetchall(), total


# --------------------------------------------------------------------------
# Suppliers
# --------------------------------------------------------------------------

def validate_supplier_form(db, form, *, supplier_id=None):
    data, errors = {}, {}
    for field, label, max_len, required in (("name", "Name", 120, True),
                                            ("contact_name", "Contact name", 120, False),
                                            ("email", "Email", 200, False),
                                            ("phone", "Phone", 40, False),
                                            ("notes", "Notes", 1000, False)):
        value = _text(form, field)
        _limit(errors, field, value, label, max_len, required)
        data[field] = value
    if data["email"] and "email" not in errors and not EMAIL_RE.match(data["email"]):
        errors["email"] = "Enter a valid email address."
    if data["name"] and "name" not in errors:
        if db.execute("SELECT 1 FROM suppliers WHERE account_id = ? AND name = ? AND id IS NOT ?",
                      (db.account_id, data["name"], supplier_id)).fetchone():
            errors["name"] = f"A supplier named '{data['name']}' already exists."
    if errors:
        raise ValidationError(errors)
    return data


def create_supplier(db, data, *, is_demo=False):
    with transaction(db):
        return db.execute(
            "INSERT INTO suppliers (account_id, name, contact_name, email, phone, notes, is_demo) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (db.account_id, data["name"], data.get("contact_name", ""), data.get("email", ""),
             data.get("phone", ""), data.get("notes", ""), int(is_demo))).lastrowid


def update_supplier(db, supplier_id, data):
    with transaction(db):
        db.execute(
            """UPDATE suppliers SET name = ?, contact_name = ?, email = ?, phone = ?, notes = ?,
                   updated_at = datetime('now') WHERE id = ? AND account_id = ?""",
            (data["name"], data["contact_name"], data["email"], data["phone"], data["notes"],
             supplier_id, db.account_id))


def delete_supplier(db, supplier_id):
    with transaction(db):
        count = db.execute("SELECT COUNT(*) FROM products WHERE supplier_id = ? AND account_id = ?",
                           (supplier_id, db.account_id)).fetchone()[0]
        if count:
            raise StockError(f"This supplier is assigned to {count} product(s), including "
                             "archived ones. Reassign those products first.")
        db.execute("DELETE FROM suppliers WHERE id = ? AND account_id = ?",
                   (supplier_id, db.account_id))


def get_supplier(db, supplier_id):
    return db.execute("SELECT * FROM suppliers WHERE id = ? AND account_id = ?",
                      (supplier_id, db.account_id)).fetchone()


def list_suppliers(db):
    return db.execute(
        """SELECT s.*, COUNT(p.id) AS product_count
           FROM suppliers s LEFT JOIN products p
             ON p.supplier_id = s.id AND p.account_id = s.account_id AND p.is_active = 1
           WHERE s.account_id = ?
           GROUP BY s.id ORDER BY s.name COLLATE NOCASE""", (db.account_id,)).fetchall()


# --------------------------------------------------------------------------
# CSV import
# --------------------------------------------------------------------------

def _normalise_header(name):
    key = re.sub(r"[\s\-]+", "_", (name or "").strip().lower())
    return HEADER_ALIASES.get(key, key)


def parse_import(db, raw):
    """Validate an uploaded CSV without writing anything.

    Each row gets status 'new' (will be created), 'duplicate' (SKU already in
    the database; skipped, existing product untouched) or 'error'."""
    result = {"rows": [], "file_errors": [], "warnings": [],
              "counts": {"new": 0, "duplicate": 0, "error": 0}, "can_import": False}
    if not raw or not raw.strip():
        result["file_errors"].append("The file is empty.")
        return result
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        result["file_errors"].append(
            "The file is not UTF-8 text. In Excel use 'Save As > CSV UTF-8 (Comma delimited)'.")
        return result
    if "\x00" in text:
        result["file_errors"].append(
            "This looks like a binary file (for example .xlsx), not a CSV. Export it as CSV first.")
        return result

    first_line = text.split("\n", 1)[0]
    delimiter = ","
    if "," not in first_line:
        for candidate, label in ((";", "semicolon"), ("\t", "tab")):
            if candidate in first_line:
                delimiter = candidate
                result["warnings"].append(f"Read as a {label}-separated file.")
                break
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    try:
        header = next(reader, None)
        if not header or not any(h.strip() for h in header):
            result["file_errors"].append("The first row must contain column headers.")
            return result
        columns = [_normalise_header(h) for h in header]
        seen_cols = set()
        for col in columns:
            if col and col in seen_cols:
                result["file_errors"].append(f"Column '{col}' appears more than once.")
            seen_cols.add(col)
        missing = [c for c in REQUIRED_IMPORT_COLUMNS if c not in seen_cols]
        if missing:
            result["file_errors"].append(
                "Missing required column(s): " + ", ".join(missing) +
                ". Required: " + ", ".join(REQUIRED_IMPORT_COLUMNS) + ".")
        ignored = [h.strip() for h, c in zip(header, columns) if c not in PRODUCT_COLUMNS and h.strip()]
        if ignored:
            result["warnings"].append("Ignored column(s): " + ", ".join(ignored) + ".")
        if result["file_errors"]:
            return result

        records = []
        for values in reader:
            if not any(v.strip() for v in values):
                continue
            records.append((reader.line_num, values))
            if len(records) > MAX_IMPORT_ROWS:
                result["file_errors"].append(
                    f"The file has more than {MAX_IMPORT_ROWS:,} rows. Split it into smaller files.")
                return result
    except csv.Error as exc:
        result["file_errors"].append(f"The CSV could not be read near line {reader.line_num}: {exc}.")
        return result

    if not records:
        result["file_errors"].append("The file has a header row but no product rows.")
        return result

    known_suppliers = {r["name"].lower(): r["name"] for r in db.execute(
        "SELECT name FROM suppliers WHERE account_id = ?", (db.account_id,))}
    categories = {c.lower(): c for c in reversed(list_categories(db))}
    first_seen = {}
    for line, values in records:
        raw_row = {col: values[i] for i, col in enumerate(columns)
                   if i < len(values) and col in PRODUCT_COLUMNS}
        data, field_errors = clean_product_fields(raw_row, include_quantity=True, from_csv=True)
        errors = list(field_errors.values())
        notes = []
        if len(values) > len(columns) and any(v.strip() for v in values[len(columns):]):
            errors.append("Row has more values than there are columns (check for unquoted commas).")
        category = data.get("category", "")
        if category:
            existing = categories.setdefault(category.lower(), category)
            if existing != category:
                notes.append(f"Category '{category}' will be saved as existing category '{existing}'.")
                data["category"] = existing
        supplier = " ".join(unescape_formula(_text(raw_row, "supplier")).split())
        if len(supplier) > 120:
            errors.append("Supplier must be at most 120 characters.")
        elif supplier and supplier.lower() in known_suppliers:
            supplier = known_suppliers[supplier.lower()]
        elif supplier:
            notes.append(f"New supplier '{supplier}' will be created.")

        status = "new"
        sku_key = data["sku"].lower()
        if "sku" not in field_errors:
            if sku_key in first_seen:
                errors.append(f"Duplicate SKU '{data['sku']}' in this file "
                              f"(first used on line {first_seen[sku_key]}).")
            else:
                first_seen[sku_key] = line
        owner = None if errors else db.execute(
            "SELECT name, quantity, is_active, is_demo FROM products WHERE account_id = ? AND sku = ?",
            (db.account_id, data["sku"])).fetchone()
        if errors:
            status = "error"
        elif owner:
            status = "duplicate"
            kind = " archived" if not owner["is_active"] else " demo" if owner["is_demo"] else ""
            notes = [f"SKU already exists as{kind} product '{owner['name']}' "
                     f"({owner['quantity']} on hand). This row will be skipped and the existing "
                     "product left unchanged; record stock changes with Stock in/out or Adjust."]
        result["counts"][status] += 1
        result["rows"].append({"line": line, "sku": data["sku"], "name": data.get("name", ""),
                               "data": data, "supplier": supplier, "status": status,
                               "errors": errors, "notes": notes})
    result["can_import"] = result["counts"]["error"] == 0 and result["counts"]["new"] > 0
    return result


def save_import_preview(db, result):
    token = secrets.token_urlsafe(24)
    with transaction(db):
        db.execute("DELETE FROM import_previews WHERE created_at < datetime('now', ?)", (PREVIEW_TTL,))
        db.execute("INSERT INTO import_previews (token, account_id, payload) VALUES (?, ?, ?)",
                   (token, db.account_id, json.dumps({"rows": result["rows"]})))
    return token


def confirm_import(db, token):
    """Create every 'new' row from a saved preview in one transaction."""
    with transaction(db):
        row = db.execute(
            "SELECT payload FROM import_previews WHERE token = ? AND account_id = ? "
            "AND created_at >= datetime('now', ?)", (token, db.account_id, PREVIEW_TTL)).fetchone()
        if row is None:
            raise StockError("This import preview has expired or was already used. "
                             "Please upload the file again.")
        rows = json.loads(row["payload"])["rows"]
        if any(r["status"] == "error" for r in rows):
            raise StockError("The file contains errors. Fix them and upload it again.")
        created, skipped, new_suppliers = [], [], []
        today = date.today().isoformat()
        for r in rows:
            if r["status"] != "new" or _sku_owner(db, r["sku"]):
                skipped.append(r["sku"])
                continue
            supplier_id = None
            if r["supplier"]:
                existing = db.execute("SELECT id FROM suppliers WHERE account_id = ? AND name = ?",
                                      (db.account_id, r["supplier"])).fetchone()
                if existing:
                    supplier_id = existing["id"]
                else:
                    supplier_id = create_supplier(db, {"name": r["supplier"]})
                    new_suppliers.append(r["supplier"])
            data = dict(r["data"], supplier_id=supplier_id)
            _insert_product(db, data, source="import", is_demo=False, movement_date=today)
            created.append(r["sku"])
        db.execute("DELETE FROM import_previews WHERE token = ? AND account_id = ?",
                   (token, db.account_id))
    return {"created": created, "skipped": skipped, "new_suppliers": new_suppliers}


# --------------------------------------------------------------------------
# CSV export
# --------------------------------------------------------------------------

def _csv_safe(value):
    """Neutralise spreadsheet formula injection in text cells."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def to_csv(header, rows):
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return "﻿" + out.getvalue()   # BOM so Excel reads UTF-8 correctly


def export_products_csv(db, **filters):
    rows, _ = list_products(db, **filters)
    header = PRODUCT_COLUMNS + ["status", "active"]
    return to_csv(header, [
        [_csv_safe(r["sku"]), _csv_safe(r["name"]), _csv_safe(r["category"]),
         _csv_safe(r["supplier_name"] or ""), money_str(r["cost_cents"]),
         money_str(r["price_cents"]), r["quantity"], r["reorder_threshold"],
         _csv_safe(r["description"]), r["status"], "yes" if r["is_active"] else "no"]
        for r in rows])


def export_movements_csv(db, **filters):
    rows, _ = list_movements(db, **filters)
    header = ["id", "movement_date", "recorded_at_utc", "sku", "product_name", "type",
              "quantity_change", "quantity_before", "quantity_after", "reason", "notes", "source"]
    return to_csv(header, [
        [r["id"], r["movement_date"], r["created_at"], _csv_safe(r["sku"]),
         _csv_safe(r["product_name"]), r["movement_type"], r["quantity_change"],
         r["quantity_before"], r["quantity_after"], _csv_safe(r["reason"]),
         _csv_safe(r["notes"]), r["source"]]
        for r in rows])


def export_suppliers_csv(db):
    header = ["name", "contact_name", "email", "phone", "notes", "active_products", "created_at_utc"]
    return to_csv(header, [
        [_csv_safe(r["name"]), _csv_safe(r["contact_name"]), _csv_safe(r["email"]),
         _csv_safe(r["phone"]), _csv_safe(r["notes"]), r["product_count"], r["created_at"]]
        for r in list_suppliers(db)])


def export_all_zip(db, account):
    """Everything one shop has stored, as a ZIP of CSV files ("Export all my data")."""
    account_csv = to_csv(["shop_name", "email", "created_at_utc", "paid_until"],
                         [[_csv_safe(account["shop_name"]), account["email"],
                           account["created_at"], account["paid_until"] or ""]])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("account.csv", account_csv)
        zf.writestr("products.csv", export_products_csv(db, include_archived=True))
        zf.writestr("stock_movements.csv", export_movements_csv(db))
        zf.writestr("suppliers.csv", export_suppliers_csv(db))
    return buffer.getvalue()


# --------------------------------------------------------------------------
# Demo data
# --------------------------------------------------------------------------

DEMO_SUPPLIERS = [
    {"name": "Demo Supply Co.", "contact_name": "Alex Rivera", "email": "orders@demo-supply.example",
     "phone": "555-0100", "notes": "Demo supplier"},
    {"name": "Demo Packaging Ltd.", "contact_name": "Sam Lee", "email": "sales@demo-pack.example",
     "phone": "555-0101", "notes": "Demo supplier"},
    {"name": "Demo Coffee Roasters", "contact_name": "Jordan Park",
     "email": "wholesale@demo-roast.example", "phone": "555-0102", "notes": "Demo supplier"},
]

# (sku, name, category, supplier index, cost, price, opening qty, threshold, description,
#  [(days ago, kind, amount, reason, notes)])
DEMO_PRODUCTS = [
    ("DEMO-MUG-12", "Ceramic Mug 12 oz", "Kitchenware", 0, "3.20", "9.95", 60, 15,
     "White stoneware mug.", [(20, "out", 18, "Sale", ""), (10, "out", 12, "Sale", ""),
                              (6, "in", 24, "Purchase received", "PO 1042"), (2, "out", 9, "Sale", "")]),
    ("DEMO-MUG-16", "Ceramic Mug 16 oz", "Kitchenware", 0, "3.80", "11.95", 30, 10, "",
     [(15, "out", 14, "Sale", ""), (4, "out", 9, "Sale", "")]),
    ("DEMO-BEAN-HOUSE", "House Blend Coffee Beans 1 kg", "Coffee", 2, "11.00", "24.00", 25, 8,
     "Medium roast.", [(12, "out", 10, "Sale", ""), (5, "out", 9, "Sale", ""), (1, "out", 6, "Sale", "")]),
    ("DEMO-BEAN-DECAF", "Decaf Coffee Beans 500 g", "Coffee", 2, "7.50", "15.50", 12, 5, "",
     [(8, "out", 4, "Sale", "")]),
    ("DEMO-FILTER-100", "Paper Filters (100 pack)", "Coffee Accessories", 1, "1.10", "3.50", 80, 20, "",
     [(14, "out", 30, "Sale", ""), (3, "out", 35, "Sale", "")]),
    ("DEMO-BOX-SM", "Shipping Box Small", "Packaging", 1, "0.45", "0.00", 200, 50,
     "Internal supply, not for sale.", [(9, "out", 120, "Internal use", ""),
                                         (7, "in", 100, "Purchase received", ""),
                                         (2, "out", 60, "Internal use", "")]),
    ("DEMO-TAPE-48", "Packing Tape 48 mm", "Packaging", 1, "1.90", "0.00", 10, 4, "",
     [(6, "out", 10, "Internal use", "")]),
    ("DEMO-TOTE-NAT", "Canvas Tote Bag", "Merchandise", 0, "2.75", "14.00", 40, 10, "",
     [(11, "out", 6, "Sale", ""),
      (3, "adjustment", 31, "Stock count correction: 3 damaged bags found", "Quarterly count")]),
    ("DEMO-GRIND-HAND", "Hand Coffee Grinder", "Coffee Accessories", 2, "18.00", "42.00", 6, 3, "",
     [(7, "out", 3, "Sale", "")]),
    ("DEMO-SCOOP", "Stainless Coffee Scoop", "Coffee Accessories", None, "0.80", "4.50", 50, 10, "", []),
]


def demo_summary(db):
    row = db.execute(
        """SELECT
             (SELECT COUNT(*) FROM products WHERE account_id = :a AND is_demo = 1) AS products,
             (SELECT COUNT(*) FROM stock_movements m JOIN products p ON p.id = m.product_id
                WHERE p.account_id = :a AND p.is_demo = 1) AS movements,
             (SELECT COUNT(*) FROM stock_movements m JOIN products p ON p.id = m.product_id
                WHERE p.account_id = :a AND p.is_demo = 1 AND m.source != 'demo') AS user_movements,
             (SELECT COUNT(*) FROM suppliers WHERE account_id = :a AND is_demo = 1) AS suppliers,
             (SELECT COUNT(*) FROM products WHERE account_id = :a AND is_demo = 0) AS real_products""",
        {"a": db.account_id}).fetchone()
    summary = dict(row)
    summary["kept_suppliers"] = [r["name"] for r in db.execute(
        """SELECT name FROM suppliers s WHERE s.account_id = ? AND s.is_demo = 1 AND EXISTS
             (SELECT 1 FROM products p WHERE p.supplier_id = s.id AND p.is_demo = 0)
           ORDER BY name""", (db.account_id,))]
    return summary


def load_demo_data(db):
    with transaction(db):
        if db.execute("SELECT 1 FROM products WHERE account_id = ? AND is_demo = 1 LIMIT 1",
                      (db.account_id,)).fetchone():
            raise StockError("Demo data is already loaded. Use 'Reset demo data' to restore it.")
        supplier_ids = []
        for s in DEMO_SUPPLIERS:
            existing = db.execute("SELECT id FROM suppliers WHERE account_id = ? AND name = ?",
                                  (db.account_id, s["name"])).fetchone()
            supplier_ids.append(existing["id"] if existing else create_supplier(db, s, is_demo=True))
        created, skipped = 0, []
        today = date.today()
        opening = (today - timedelta(days=30)).isoformat()
        for sku, name, category, sup, cost, price, qty, threshold, desc, moves in DEMO_PRODUCTS:
            if _sku_owner(db, sku):
                skipped.append(sku)
                continue
            data = {"sku": sku, "name": name, "category": category,
                    "supplier_id": supplier_ids[sup] if sup is not None else None,
                    "cost_cents": parse_money(cost, "Cost"), "price_cents": parse_money(price, "Price"),
                    "quantity": qty, "reorder_threshold": threshold, "description": desc}
            pid = _insert_product(db, data, source="demo", is_demo=True, movement_date=opening)
            for days_ago, kind, amount, reason, notes in moves:
                apply_movement(db, pid, kind, amount, (today - timedelta(days=days_ago)).isoformat(),
                               reason, notes, source="demo")
            created += 1
    return {"created": created, "skipped": skipped}


def remove_demo_data(db):
    """Delete demo products, their movements and unused demo suppliers.

    Real records are never deleted: a demo supplier that a real product uses is
    converted into a real supplier instead, and the database triggers reject any
    attempt to delete real products or their movements."""
    with transaction(db):
        a = db.account_id
        kept = [r["name"] for r in db.execute(
            """SELECT name FROM suppliers s WHERE s.account_id = ? AND s.is_demo = 1 AND EXISTS
                 (SELECT 1 FROM products p WHERE p.supplier_id = s.id AND p.is_demo = 0)""", (a,))]
        db.execute("""UPDATE suppliers SET is_demo = 0 WHERE account_id = ? AND is_demo = 1 AND EXISTS
                        (SELECT 1 FROM products p WHERE p.supplier_id = suppliers.id AND p.is_demo = 0)""",
                   (a,))
        movements = db.execute("DELETE FROM stock_movements WHERE account_id = ? AND product_id IN "
                               "(SELECT id FROM products WHERE account_id = ? AND is_demo = 1)",
                               (a, a)).rowcount
        products = db.execute("DELETE FROM products WHERE account_id = ? AND is_demo = 1",
                              (a,)).rowcount
        suppliers = db.execute("DELETE FROM suppliers WHERE account_id = ? AND is_demo = 1",
                               (a,)).rowcount
    return {"products": products, "movements": movements, "suppliers": suppliers,
            "kept_suppliers": kept}


def reset_demo_data(db):
    with transaction(db):
        removed = remove_demo_data(db)
        loaded = load_demo_data(db)
    return {"removed": removed, "loaded": loaded}
