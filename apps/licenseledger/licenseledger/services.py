"""Domain logic: validation, CRUD, license lifecycle, search, reminders.

All SQL here uses bound parameters; user input is never interpolated into
query strings. Functions take an open sqlite3 connection and leave
committing to the caller.
"""
import calendar
import re
import sqlite3
from datetime import date, datetime, timedelta

from flask import current_app, g, has_app_context

from . import security
from .db import now_iso

LICENSE_TYPES = {
    "lifetime": "Lifetime",
    "fixed": "Fixed term",
    "manual": "Manually managed",
}
STATUS_LABELS = {
    "active": "Active",
    "suspended": "Suspended",
    "expired": "Expired",
    "revoked": "Revoked",
    "replaced": "Replaced",
}
CREATE_STATUSES = ("active", "suspended", "expired", "revoked")
TERMINAL_STATUSES = ("revoked", "replaced")

ORDER_STATUSES = {"open": "Open", "completed": "Completed", "cancelled": "Cancelled"}
# Payment status is always recorded by the owner. LicenseLedger has no payment
# integration and never verifies that money was received; labels say so.
PAYMENT_STATUSES = {
    "not_recorded": "Not recorded",
    "awaiting": "Awaiting payment",
    "marked_paid": "Marked paid by owner",
    "refunded": "Marked refunded by owner",
    "not_required": "No payment required",
}
PAYMENT_OK_FOR_DELIVERY = ("marked_paid", "not_required")
DELIVERY_STATUSES = {"pending": "Delivery pending", "delivered": "Delivered"}
DELIVERY_METHODS = {
    "email": "Email",
    "download": "Download page / store",
    "message": "Chat or message",
    "other": "Other",
}
ORDER_DELIVERY_STATES = {
    "nothing": "Nothing to deliver",
    "pending": "Delivery pending",
    "partial": "Partly delivered",
    "delivered": "Delivered",
    "cancelled": "Cancelled",
}
AMOUNT_RE = re.compile(
    r"^(?:[A-Z]{3} ?|[$€£¥] ?)?\d{1,3}(?:,?\d{3})*(?:\.\d{1,2})?(?: ?[A-Z]{3})?$"
)
MAX_ORDER_DAYS_AHEAD = 30
MAX_QUANTITY = 1000

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
NAME_MAX = 200
TEXT_MAX = 4000
MAX_TERM_DAYS = 36500
MAX_TERM_MONTHS = 1200
# Dates outside this range are almost always typos (e.g. 0202 or 20260)
# and would otherwise overflow date arithmetic during renewals.
MIN_YEAR = 1970
MAX_YEAR = 2200

# A fixed-term license whose expiration date has passed is reported as
# expired without needing a write. Manual licenses only change status
# when the owner changes it.
EFFECTIVE_STATUS_SQL = (
    "(CASE WHEN l.status = 'active' AND l.license_type = 'fixed' "
    "AND l.expiration_date IS NOT NULL AND l.expiration_date < :today "
    "THEN 'expired' ELSE l.status END)"
)

LICENSE_SELECT = f"""
SELECT l.id, l.product_id, l.customer_id, l.order_id, l.key_hint, l.license_type,
       l.issue_date, l.expiration_date, l.status, l.replaces_id, l.replaced_by_id,
       l.notes, l.created_at, l.updated_at, l.revoked_at, l.revocation_reason,
       l.delivery_status, l.delivered_at, l.delivery_method, l.delivery_reference,
       {EFFECTIVE_STATUS_SQL} AS effective_status,
       p.name AS product_name, p.sku AS product_sku,
       c.name AS customer_name, c.email AS customer_email, c.company AS customer_company,
       o.order_ref AS order_ref, o.order_status AS order_status, o.payment_status AS payment_status
FROM licenses l
JOIN products p ON p.id = l.product_id
JOIN customers c ON c.id = l.customer_id
LEFT JOIN orders o ON o.id = l.order_id
WHERE l.account_id = :acct
"""


class ValidationError(Exception):
    def __init__(self, errors):
        if isinstance(errors, str):
            errors = [errors]
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


class NotFound(Exception):
    pass


# --------------------------------------------------------------------------- helpers

def acct():
    """The signed-in seller's account id. Every query in this module is filtered
    by it; with no account in context nothing runs (never "all accounts")."""
    account_id = g.get("account_id") if has_app_context() else None
    if not account_id:
        raise RuntimeError("No account in context")
    return account_id


def get_today():
    if has_app_context():
        override = current_app.config.get("TODAY")
        if override:
            return date.fromisoformat(override) if isinstance(override, str) else override
    return date.today()


def parse_date(value, label, errors, required=False):
    value = (value or "").strip()
    if not value:
        if required:
            errors.append(f"{label} is required.")
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        errors.append(f"{label} must be a valid date (YYYY-MM-DD).")
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        errors.append(f"{label} must be a valid date (YYYY-MM-DD).")
        return None
    if not MIN_YEAR <= parsed.year <= MAX_YEAR:
        errors.append(f"{label} must be between {MIN_YEAR} and {MAX_YEAR}.")
        return None
    return parsed.isoformat()


def add_months(start, months):
    """Calendar-month arithmetic; the day is clamped (Jan 31 + 1 month = Feb 28/29)."""
    total = start.month - 1 + months
    year, month = start.year + total // 12, total % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def shift_date(start, days=0, months=0):
    """Add a term to a date, refusing results outside the supported range."""
    try:
        result = add_months(start, months) if months else start
        result = result + timedelta(days=days)
    except (OverflowError, ValueError):
        raise ValidationError(f"The resulting date would be after {MAX_YEAR}.")
    if result.year > MAX_YEAR:
        raise ValidationError(f"The resulting date would be after {MAX_YEAR}.")
    return result


def parse_period(value):
    """Parse a renewal period like '30d', '12m' or a bare number of days. Returns (days, months)."""
    value = str(value or "").strip().lower()
    m = re.fullmatch(r"(\d{1,5})\s*([dm]?)", value)
    if not m:
        return None, None
    number, unit = int(m.group(1)), m.group(2) or "d"
    return (None, number) if unit == "m" else (number, None)


def describe_period(days=None, months=None):
    if months:
        if months % 12 == 0:
            years = months // 12
            return f"+{years} year{'s' if years != 1 else ''}"
        return f"+{months} month{'s' if months != 1 else ''}"
    if days:
        return f"+{days} day{'s' if days != 1 else ''}"
    return "custom date"


CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
CARD_CANDIDATE_RE = re.compile(r"(?<![\d-])(?:\d{4}[ -]?){3}\d{1,7}(?![\d-])|(?<![\d-])\d{4}[ -]?\d{6}[ -]?\d{5}(?![\d-])")


def luhn_ok(digits):
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def looks_like_card_number(text):
    """True if text contains a 13–19 digit, Luhn-valid number (payment card data must never be stored)."""
    for match in CARD_CANDIDATE_RE.finditer(text or ""):
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and luhn_ok(digits):
            return True
    return False


def clean_text(value, label, errors, required=False, max_len=NAME_MAX, allow_card_like=False):
    """Validate free text. Rejects control characters, anything that looks like
    a generated license key (keys must never be stored in readable fields) and,
    unless allowed, payment-card-like numbers."""
    value = (value or "").strip()
    if required and not value:
        errors.append(f"{label} is required.")
    elif len(value) > max_len:
        errors.append(f"{label} must be at most {max_len} characters.")
    elif CONTROL_CHARS_RE.search(value):
        errors.append(f"{label} contains invalid control characters.")
    elif security.KEY_PATTERN.search(value):
        errors.append(f"{label} appears to contain a license key. Keys must not be stored in text fields.")
    elif not allow_card_like and looks_like_card_number(value):
        errors.append(f"{label} appears to contain a payment card number. Card data must not be stored.")
    return value


def parse_int(value, label, errors, required=False, minimum=1, maximum=MAX_TERM_DAYS):
    value = str(value if value is not None else "").strip()
    if not value:
        if required:
            errors.append(f"{label} is required.")
        return None
    try:
        number = int(value)
    except ValueError:
        errors.append(f"{label} must be a whole number.")
        return None
    if number < minimum or number > maximum:
        errors.append(f"{label} must be between {minimum} and {maximum}.")
        return None
    return number


def parse_id(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def escape_like(value):
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def days_until(iso_date, today=None):
    if not iso_date:
        return None
    today = today or get_today()
    return (date.fromisoformat(iso_date) - today).days


def audit(db, action, detail="", license_id=None, entity_type="license", entity_id=None):
    if entity_type == "license" and entity_id is None:
        entity_id = license_id
    db.execute(
        "INSERT INTO audit_events (account_id, license_id, entity_type, entity_id, action, detail, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (acct(), license_id, entity_type, entity_id, action, detail[:TEXT_MAX], now_iso()),
    )


def delete_row(db, table, row_id, label):
    if table not in ("products", "customers", "orders"):
        raise ValueError("unsupported table")
    row = db.execute(f"SELECT id FROM {table} WHERE id = ? AND account_id = ?", (row_id, acct())).fetchone()
    if not row:
        raise NotFound(label)
    try:
        db.execute(f"DELETE FROM {table} WHERE id = ? AND account_id = ?", (row_id, acct()))
    except sqlite3.IntegrityError:
        raise ValidationError(
            f"This {label} still has linked records (orders or licenses). "
            "Remove or reassign them first."
        )
    audit(db, "deleted", f"Deleted {label} #{row_id}", entity_type=label, entity_id=row_id)


# --------------------------------------------------------------------------- products

def get_product(db, product_id):
    row = db.execute("SELECT * FROM products WHERE id = ? AND account_id = ?", (product_id, acct())).fetchone()
    if not row:
        raise NotFound("product")
    return row


def list_products(db):
    return db.execute(
        "SELECT p.*, (SELECT COUNT(*) FROM licenses l WHERE l.product_id = p.id) AS license_count "
        "FROM products p WHERE p.account_id = ? ORDER BY p.name COLLATE NOCASE", (acct(),)
    ).fetchall()


def save_product(db, form, product_id=None):
    errors = []
    name = clean_text(form.get("name"), "Product name", errors, required=True)
    sku = clean_text(form.get("sku"), "SKU", errors, max_len=64) or None
    license_type = (form.get("default_license_type") or "fixed").strip()
    if license_type not in LICENSE_TYPES:
        errors.append("Default license type is not valid.")
    term = parse_int(form.get("default_term_days"), "Default term (days)", errors)
    notes = clean_text(form.get("notes"), "Notes", errors, max_len=TEXT_MAX)
    if license_type == "lifetime":
        term = None

    if name:
        clash = db.execute(
            "SELECT id FROM products WHERE account_id = ? AND name = ? COLLATE NOCASE AND id IS NOT ?",
            (acct(), name, product_id)
        ).fetchone()
        if clash:
            errors.append("A product with this name already exists.")
    if sku:
        clash = db.execute(
            "SELECT id FROM products WHERE account_id = ? AND sku = ? COLLATE NOCASE AND id IS NOT ?",
            (acct(), sku, product_id)
        ).fetchone()
        if clash:
            errors.append("A product with this SKU already exists.")
    if errors:
        raise ValidationError(errors)

    if product_id is None:
        cur = db.execute(
            "INSERT INTO products (account_id, name, sku, default_license_type, default_term_days, notes, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (acct(), name, sku, license_type, term, notes, now_iso()),
        )
        product_id = cur.lastrowid
        audit(db, "created", f"Product '{name}' created", entity_type="product", entity_id=product_id)
    else:
        get_product(db, product_id)
        db.execute(
            "UPDATE products SET name = ?, sku = ?, default_license_type = ?, default_term_days = ?, "
            "notes = ? WHERE id = ? AND account_id = ?",
            (name, sku, license_type, term, notes, product_id, acct()),
        )
        audit(db, "updated", f"Product '{name}' updated", entity_type="product", entity_id=product_id)
    return product_id


# --------------------------------------------------------------------------- customers

def get_customer(db, customer_id):
    row = db.execute("SELECT * FROM customers WHERE id = ? AND account_id = ?", (customer_id, acct())).fetchone()
    if not row:
        raise NotFound("customer")
    return row


def list_customers(db, q=""):
    sql = (
        "SELECT c.*, "
        "(SELECT COUNT(*) FROM licenses l WHERE l.customer_id = c.id) AS license_count, "
        "(SELECT COUNT(*) FROM orders o WHERE o.customer_id = c.id) AS order_count "
        "FROM customers c WHERE c.account_id = ?"
    )
    params = [acct()]
    if q:
        like = f"%{escape_like(q.strip())}%"
        sql += (
            " AND (c.name LIKE ? ESCAPE '\\' OR c.email LIKE ? ESCAPE '\\' "
            "OR c.company LIKE ? ESCAPE '\\')"
        )
        params += [like, like, like]
    sql += " ORDER BY c.name COLLATE NOCASE"
    return db.execute(sql, params).fetchall()


def validate_email(email, errors):
    email = (email or "").strip().lower()
    if not email:
        errors.append("Email is required.")
    elif len(email) > 254 or not EMAIL_RE.match(email):
        errors.append("Email address is not valid.")
    return email


def save_customer(db, form, customer_id=None):
    errors = []
    name = clean_text(form.get("name"), "Customer name", errors, required=True)
    email = validate_email(form.get("email"), errors)
    company = clean_text(form.get("company"), "Company", errors)
    notes = clean_text(form.get("notes"), "Notes", errors, max_len=TEXT_MAX)
    if email and not errors:
        clash = db.execute(
            "SELECT id FROM customers WHERE account_id = ? AND email = ? COLLATE NOCASE AND id IS NOT ?",
            (acct(), email, customer_id)
        ).fetchone()
        if clash:
            errors.append("A customer with this email already exists.")
    if errors:
        raise ValidationError(errors)

    if customer_id is None:
        cur = db.execute(
            "INSERT INTO customers (account_id, name, email, company, notes, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (acct(), name, email, company, notes, now_iso()),
        )
        customer_id = cur.lastrowid
        audit(db, "created", f"Customer '{name}' created", entity_type="customer", entity_id=customer_id)
    else:
        get_customer(db, customer_id)
        db.execute(
            "UPDATE customers SET name = ?, email = ?, company = ?, notes = ? WHERE id = ? AND account_id = ?",
            (name, email, company, notes, customer_id, acct()),
        )
        audit(db, "updated", f"Customer '{name}' updated", entity_type="customer", entity_id=customer_id)
    return customer_id


# --------------------------------------------------------------------------- orders

# Seats required = sum of item quantities, or (for orders without items) the
# number of live licenses. Revoked/replaced licenses never count as delivered.
_ORDER_INNER = """
SELECT o.*, c.name AS customer_name, c.email AS customer_email,
       (SELECT COUNT(*) FROM licenses l WHERE l.order_id = o.id) AS license_count,
       (SELECT COUNT(*) FROM licenses l WHERE l.order_id = o.id
            AND l.status NOT IN ('revoked', 'replaced')) AS live_licenses,
       (SELECT COUNT(*) FROM licenses l WHERE l.order_id = o.id
            AND l.status NOT IN ('revoked', 'replaced') AND l.delivery_status = 'delivered') AS delivered_count,
       (SELECT COALESCE(SUM(i.quantity), 0) FROM order_items i WHERE i.order_id = o.id) AS seats_ordered
FROM orders o JOIN customers c ON c.id = o.customer_id
WHERE o.account_id = ?
"""
_ORDER_WITH_STATE = f"""
SELECT ox.*,
       CASE WHEN ox.seats_ordered > 0 THEN ox.seats_ordered ELSE ox.live_licenses END AS seats_required,
       CASE
         WHEN ox.order_status = 'cancelled' THEN 'cancelled'
         WHEN (CASE WHEN ox.seats_ordered > 0 THEN ox.seats_ordered ELSE ox.live_licenses END) = 0 THEN 'nothing'
         WHEN ox.delivered_count >= (CASE WHEN ox.seats_ordered > 0 THEN ox.seats_ordered ELSE ox.live_licenses END)
              THEN 'delivered'
         WHEN ox.delivered_count > 0 THEN 'partial'
         ELSE 'pending'
       END AS delivery_state
FROM ({_ORDER_INNER}) ox
"""
ORDER_QUERY = f"SELECT * FROM ({_ORDER_WITH_STATE}) os"
ORDER_SORTS = {
    "newest": "os.order_date DESC, os.id DESC",
    "oldest": "os.order_date ASC, os.id ASC",
    "customer": "os.customer_name COLLATE NOCASE, os.id",
    "ref": "os.order_ref COLLATE NOCASE",
}


def get_order(db, order_id):
    row = db.execute(ORDER_QUERY + " WHERE os.id = ?", (acct(), order_id)).fetchone()
    if not row:
        raise NotFound("order")
    return row


def list_orders(db, q="", customer_id=None, order_status="", payment_status="", delivery="",
                date_from="", date_to="", product_id=None, sort="newest"):
    where, params = [], [acct()]
    if q:
        like = f"%{escape_like(q.strip())}%"
        where.append(
            "(os.order_ref LIKE ? ESCAPE '\\' OR os.customer_name LIKE ? ESCAPE '\\' "
            "OR os.customer_email LIKE ? ESCAPE '\\')"
        )
        params += [like, like, like]
    if customer_id:
        where.append("os.customer_id = ?")
        params.append(customer_id)
    if order_status in ORDER_STATUSES:
        where.append("os.order_status = ?")
        params.append(order_status)
    if payment_status in PAYMENT_STATUSES:
        where.append("os.payment_status = ?")
        params.append(payment_status)
    if delivery == "outstanding":
        where.append("os.delivery_state IN ('pending', 'partial')")
    elif delivery in ORDER_DELIVERY_STATES:
        where.append("os.delivery_state = ?")
        params.append(delivery)
    for bound, op in ((date_from, ">="), (date_to, "<=")):
        if bound:
            where.append(f"os.order_date {op} ?")
            params.append(bound)
    if product_id:
        where.append(
            "(EXISTS (SELECT 1 FROM order_items i WHERE i.order_id = os.id AND i.product_id = ?) "
            "OR EXISTS (SELECT 1 FROM licenses l WHERE l.order_id = os.id AND l.product_id = ?))"
        )
        params += [product_id, product_id]
    sql = ORDER_QUERY + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY " + ORDER_SORTS.get(sort, ORDER_SORTS["newest"])
    return db.execute(sql, params).fetchall()


def validate_amount(value, errors):
    value = re.sub(r"\s+", " ", (value or "").strip())
    if value and (len(value) > 30 or not AMOUNT_RE.match(value)):
        errors.append("Amount must look like 49.00, USD 49.00, $49 or 1,250.00 EUR.")
    return value


def save_order(db, form, order_id=None):
    """Create or edit an order's basic fields. Status and payment changes go
    through set_order_status / set_payment_status so they are audited."""
    errors = []
    order_ref = clean_text(form.get("order_ref"), "Order reference", errors, required=True, max_len=100,
                           allow_card_like=True)
    customer_id = parse_id(form.get("customer_id"))
    if not customer_id or not db.execute(
        "SELECT 1 FROM customers WHERE id = ? AND account_id = ?", (customer_id, acct())
    ).fetchone():
        errors.append("Choose a valid customer.")
    order_date = parse_date(form.get("order_date"), "Order date", errors, required=True)
    if order_date and order_date > (get_today() + timedelta(days=MAX_ORDER_DAYS_AHEAD)).isoformat():
        errors.append(f"Order date cannot be more than {MAX_ORDER_DAYS_AHEAD} days in the future.")
    amount = validate_amount(form.get("amount"), errors)
    notes = clean_text(form.get("notes"), "Notes", errors, max_len=TEXT_MAX)
    if order_ref:
        clash = db.execute(
            "SELECT id FROM orders WHERE account_id = ? AND order_ref = ? COLLATE NOCASE AND id IS NOT ?",
            (acct(), order_ref, order_id)
        ).fetchone()
        if clash:
            errors.append("An order with this reference already exists.")
    if order_id is not None and customer_id:
        mismatched = db.execute(
            "SELECT 1 FROM licenses WHERE account_id = ? AND order_id = ? AND customer_id != ?",
            (acct(), order_id, customer_id)
        ).fetchone()
        if mismatched:
            errors.append("This order has licenses for another customer; reassign them before changing the customer.")

    payment_status = payment_reference = None
    item_product = item_qty = None
    if order_id is None:
        payment_status = (form.get("payment_status") or "awaiting").strip()
        if payment_status not in PAYMENT_STATUSES:
            errors.append("Choose a valid payment status.")
        payment_reference = clean_text(form.get("payment_reference"), "Payment reference", errors, max_len=100)
        if str(form.get("item_product_id") or "").strip():
            item_product = parse_id(form.get("item_product_id"))
            if not item_product or not db.execute(
                "SELECT 1 FROM products WHERE id = ? AND account_id = ?", (item_product, acct())
            ).fetchone():
                errors.append("Choose a valid product for the order item.")
            item_qty = parse_int(form.get("item_quantity") or "1", "Quantity", errors, maximum=MAX_QUANTITY)
    if errors:
        raise ValidationError(errors)

    if order_id is None:
        ts = now_iso()
        cur = db.execute(
            "INSERT INTO orders (account_id, order_ref, customer_id, order_date, amount, notes, created_at, "
            "order_status, payment_status, payment_reference, payment_recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)",
            (acct(), order_ref, customer_id, order_date, amount, notes, ts, payment_status, payment_reference,
             ts if payment_status in ("marked_paid", "refunded") else None),
        )
        order_id = cur.lastrowid
        audit(db, "created",
              f"Order '{order_ref}' created; payment: {PAYMENT_STATUSES[payment_status].lower()}",
              entity_type="order", entity_id=order_id)
        if item_product:
            set_order_item(db, order_id, item_product, item_qty)
    else:
        get_order(db, order_id)
        db.execute(
            "UPDATE orders SET order_ref = ?, customer_id = ?, order_date = ?, amount = ?, notes = ? "
            "WHERE id = ? AND account_id = ?",
            (order_ref, customer_id, order_date, amount, notes, order_id, acct()),
        )
        audit(db, "updated", f"Order '{order_ref}' updated", entity_type="order", entity_id=order_id)
    return order_id


def list_order_items(db, order_id):
    """Items with how many live and delivered licenses are assigned to each."""
    return db.execute(
        "SELECT i.*, p.name AS product_name, p.sku AS product_sku, "
        "(SELECT COUNT(*) FROM licenses l WHERE l.order_id = i.order_id AND l.product_id = i.product_id "
        " AND l.status NOT IN ('revoked', 'replaced')) AS assigned, "
        "(SELECT COUNT(*) FROM licenses l WHERE l.order_id = i.order_id AND l.product_id = i.product_id "
        " AND l.status NOT IN ('revoked', 'replaced') AND l.delivery_status = 'delivered') AS delivered "
        "FROM order_items i JOIN products p ON p.id = i.product_id WHERE i.order_id = ? AND i.account_id = ? "
        "ORDER BY p.name",
        (order_id, acct()),
    ).fetchall()


def _live_licenses_for(db, order_id, product_id, exclude_id=None):
    return db.execute(
        "SELECT COUNT(*) FROM licenses WHERE account_id = ? AND order_id = ? AND product_id = ? AND id IS NOT ? "
        "AND status NOT IN ('revoked', 'replaced')",
        (acct(), order_id, product_id, exclude_id),
    ).fetchone()[0]


def set_order_item(db, order_id, product_id, quantity):
    """Add a product to an order or change its quantity (seats)."""
    order = get_order(db, order_id)
    errors = []
    if order["order_status"] != "open":
        errors.append(f"Items can only be changed on open orders (this one is {order['order_status']}).")
    product = db.execute("SELECT name FROM products WHERE id = ? AND account_id = ?",
                         (parse_id(product_id), acct())).fetchone()
    if not product:
        errors.append("Choose a valid product.")
    quantity = parse_int(quantity, "Quantity", errors, required=True, maximum=MAX_QUANTITY)
    if not errors:
        assigned = _live_licenses_for(db, order_id, int(product_id))
        if quantity < assigned:
            errors.append(f"{assigned} license(s) for this product are already assigned; quantity can't be lower.")
    if errors:
        raise ValidationError(errors)
    existing = db.execute(
        "SELECT quantity FROM order_items WHERE order_id = ? AND product_id = ? AND account_id = ?",
        (order_id, int(product_id), acct())
    ).fetchone()
    if existing:
        db.execute("UPDATE order_items SET quantity = ? WHERE order_id = ? AND product_id = ? AND account_id = ?",
                   (quantity, order_id, int(product_id), acct()))
        detail = f"Item {product['name']}: quantity {existing['quantity']} → {quantity}"
    else:
        db.execute("INSERT INTO order_items (account_id, order_id, product_id, quantity, created_at) "
                   "VALUES (?, ?, ?, ?, ?)", (acct(), order_id, int(product_id), quantity, now_iso()))
        detail = f"Item added: {product['name']} × {quantity}"
    audit(db, "item", detail, entity_type="order", entity_id=order_id)


def remove_order_item(db, order_id, product_id):
    order = get_order(db, order_id)
    item = db.execute(
        "SELECT i.quantity, p.name FROM order_items i JOIN products p ON p.id = i.product_id "
        "WHERE i.order_id = ? AND i.product_id = ? AND i.account_id = ?", (order_id, product_id, acct()),
    ).fetchone()
    if not item:
        raise NotFound("order item")
    if order["order_status"] != "open":
        raise ValidationError("Items can only be changed on open orders.")
    if _live_licenses_for(db, order_id, product_id):
        raise ValidationError("Licenses are assigned for this product. Revoke or move them before removing the item.")
    db.execute("DELETE FROM order_items WHERE order_id = ? AND product_id = ? AND account_id = ?",
               (order_id, product_id, acct()))
    audit(db, "item", f"Item removed: {item['name']} × {item['quantity']}", entity_type="order", entity_id=order_id)


ORDER_TRANSITIONS = {
    ("open", "completed"), ("open", "cancelled"), ("completed", "open"), ("cancelled", "open"),
}


def set_order_status(db, order_id, new_status, reason=""):
    order = get_order(db, order_id)
    errors = []
    reason = clean_text(reason, "Reason", errors, max_len=TEXT_MAX)
    if new_status not in ORDER_STATUSES:
        errors.append("Choose a valid order status.")
    elif new_status == order["order_status"]:
        errors.append(f"The order is already {new_status}.")
    elif (order["order_status"], new_status) not in ORDER_TRANSITIONS:
        errors.append(f"An order can't go from {order['order_status']} to {new_status}; reopen it first.")
    elif new_status == "completed":
        if order["delivery_state"] not in ("delivered",):
            errors.append("All licenses on the order must be marked delivered before it can be completed.")
        if order["payment_status"] not in PAYMENT_OK_FOR_DELIVERY:
            errors.append("Record the payment as paid (or 'no payment required') before completing the order.")
    elif new_status in ("cancelled", "open") and not reason:
        errors.append("Give a reason for cancelling or reopening the order.")
    if errors:
        raise ValidationError(errors)
    db.execute("UPDATE orders SET order_status = ? WHERE id = ? AND account_id = ?", (new_status, order_id, acct()))
    detail = f"Order status {order['order_status']} → {new_status}"
    if reason:
        detail += f". {reason}"
    audit(db, "order_status", detail, entity_type="order", entity_id=order_id)
    warning = None
    if new_status == "cancelled" and order["delivered_count"]:
        warning = (f"{order['delivered_count']} delivered license(s) on this order are still active. "
                   "Revoke them if the customer should lose access.")
    return warning


def set_payment_status(db, order_id, new_status, reference="", note=""):
    """Record what the owner knows about payment. This never verifies a payment."""
    order = get_order(db, order_id)
    errors = []
    reference = clean_text(reference, "Payment reference", errors, max_len=100)
    note = clean_text(note, "Note", errors, max_len=TEXT_MAX)
    if new_status not in PAYMENT_STATUSES or new_status == "not_recorded":
        errors.append("Choose a valid payment status.")
    elif new_status == order["payment_status"]:
        errors.append(f"Payment is already recorded as '{PAYMENT_STATUSES[new_status]}'.")
    elif new_status == "refunded" and not note:
        errors.append("Add a note explaining the refund.")
    if errors:
        raise ValidationError(errors)
    db.execute(
        "UPDATE orders SET payment_status = ?, payment_reference = ?, payment_recorded_at = ? "
        "WHERE id = ? AND account_id = ?",
        (new_status, reference or order["payment_reference"], now_iso(), order_id, acct()),
    )
    detail = (f"Payment {PAYMENT_STATUSES[order['payment_status']].lower()} → "
              f"{PAYMENT_STATUSES[new_status].lower()} (recorded by owner, not verified by LicenseLedger)")
    if reference:
        detail += f"; reference {reference}"
    if note:
        detail += f". {note}"
    audit(db, "payment", detail, entity_type="order", entity_id=order_id)
    if new_status == "refunded" and order["live_licenses"]:
        return "This order still has live licenses. Revoke them if the refund should end access."
    return None


def order_history(db, order_id, limit=200):
    """Audit events for the order itself and for every license assigned to it."""
    return db.execute(
        "SELECT a.*, p.name AS product_name, c.name AS customer_name FROM audit_events a "
        "LEFT JOIN licenses l ON l.id = a.license_id LEFT JOIN products p ON p.id = l.product_id "
        "LEFT JOIN customers c ON c.id = l.customer_id "
        "WHERE a.account_id = ? AND ((a.entity_type = 'order' AND a.entity_id = ?) "
        "OR a.license_id IN (SELECT id FROM licenses WHERE order_id = ? AND account_id = ?)) "
        "ORDER BY a.id DESC LIMIT ?",
        (acct(), order_id, order_id, acct(), limit),
    ).fetchall()


# --------------------------------------------------------------------------- licenses: read

def get_license(db, license_id):
    row = db.execute(
        LICENSE_SELECT + " AND l.id = :id", {"id": license_id, "today": get_today().isoformat(), "acct": acct()}
    ).fetchone()
    if not row:
        raise NotFound("license")
    return row


def find_license_by_key(db, key):
    key = security.normalize_key(key)
    if not key:
        return None
    return db.execute(
        LICENSE_SELECT + " AND l.key_hash = :h",
        {"h": security.hash_key(key), "today": get_today().isoformat(), "acct": acct()},
    ).fetchone()


SORTS = {
    "newest": "l.created_at DESC, l.id DESC",
    "expiring": "l.expiration_date IS NULL, l.expiration_date ASC, l.id",
    "customer": "c.name COLLATE NOCASE, l.id",
    "product": "p.name COLLATE NOCASE, l.id",
}


def search_licenses(db, q="", status="", license_type="", product_id=None, customer_id=None,
                    order_id=None, expiry="", window_days=30, sort="newest", limit=50, offset=0, delivery=""):
    """Search licenses. Returns (rows, total_count).

    expiry: "" (any), "expiring" (active, expires within window_days) or
    "expired" (effective status expired).
    """
    today = get_today()
    params = {"today": today.isoformat(), "acct": acct()}
    where = []
    q = (q or "").strip()
    if q:
        # Key lookup is deliberately not part of this GET search: keys must never
        # travel in URLs (history, access logs). Use find_license_by_key via POST.
        params["q"] = f"%{escape_like(q)}%"
        where.append(
            "(c.name LIKE :q ESCAPE '\\' OR c.email LIKE :q ESCAPE '\\' OR c.company LIKE :q ESCAPE '\\' "
            "OR o.order_ref LIKE :q ESCAPE '\\' OR p.name LIKE :q ESCAPE '\\' OR p.sku LIKE :q ESCAPE '\\')"
        )
    if status in STATUS_LABELS:
        params["status"] = status
        where.append(f"{EFFECTIVE_STATUS_SQL} = :status")
    if license_type in LICENSE_TYPES:
        params["ltype"] = license_type
        where.append("l.license_type = :ltype")
    for col, value in (("product_id", product_id), ("customer_id", customer_id), ("order_id", order_id)):
        if value:
            params[col] = value
            where.append(f"l.{col} = :{col}")
    if delivery in DELIVERY_STATUSES:
        params["delivery"] = delivery
        where.append("l.delivery_status = :delivery AND l.status NOT IN ('revoked', 'replaced')")
    if expiry == "expiring":
        params["until"] = (today + timedelta(days=window_days)).isoformat()
        where.append(
            f"l.expiration_date IS NOT NULL AND l.expiration_date >= :today "
            f"AND l.expiration_date <= :until AND {EFFECTIVE_STATUS_SQL} = 'active'"
        )
    elif expiry == "expired":
        where.append(f"{EFFECTIVE_STATUS_SQL} = 'expired'")

    clause = (" AND " + " AND ".join(where)) if where else ""
    base_from = LICENSE_SELECT.split("FROM licenses l", 1)[1]
    total = db.execute("SELECT COUNT(*) FROM licenses l " + base_from + clause, params).fetchone()[0]
    order = SORTS.get(sort, SORTS["newest"])
    params.update(limit=int(limit), offset=int(offset))
    rows = db.execute(
        LICENSE_SELECT + clause + f" ORDER BY {order} LIMIT :limit OFFSET :offset", params
    ).fetchall()
    return rows, total


def dashboard_data(db, window_days=30):
    today = get_today()
    params = {"today": today.isoformat(), "acct": acct()}
    counts = {s: 0 for s in STATUS_LABELS}
    for row in db.execute(
        f"SELECT {EFFECTIVE_STATUS_SQL} AS s, COUNT(*) AS n FROM licenses l WHERE l.account_id = :acct GROUP BY s",
        params
    ):
        counts[row["s"]] = row["n"]
    upcoming, upcoming_total = search_licenses(
        db, expiry="expiring", window_days=window_days, sort="expiring", limit=10
    )
    params["since"] = (today - timedelta(days=30)).isoformat()
    recently_expired = db.execute(
        LICENSE_SELECT + f" AND {EFFECTIVE_STATUS_SQL} = 'expired' "
        "AND (l.expiration_date IS NULL OR l.expiration_date >= :since) "
        "ORDER BY l.expiration_date DESC LIMIT 10",
        params,
    ).fetchall()
    recent, _ = search_licenses(db, sort="newest", limit=8)
    totals = {
        "products": db.execute("SELECT COUNT(*) FROM products WHERE account_id = ?", (acct(),)).fetchone()[0],
        "customers": db.execute("SELECT COUNT(*) FROM customers WHERE account_id = ?", (acct(),)).fetchone()[0],
        "orders": db.execute("SELECT COUNT(*) FROM orders WHERE account_id = ?", (acct(),)).fetchone()[0],
        "licenses": sum(counts.values()),
        "lifetime": db.execute(
            "SELECT COUNT(*) FROM licenses WHERE account_id = ? AND license_type = 'lifetime' AND status = 'active'",
            (acct(),),
        ).fetchone()[0],
    }
    return {
        "counts": counts,
        "totals": totals,
        "upcoming": upcoming,
        "upcoming_total": upcoming_total,
        "recently_expired": recently_expired,
        "recent": recent,
        "events": list_audit(db, limit=10),
        "orders_outstanding": list_orders(db, order_status="open", delivery="outstanding", sort="oldest"),
        "awaiting_payment": db.execute(
            "SELECT COUNT(*) FROM orders WHERE account_id = ? AND order_status = 'open' "
            "AND payment_status IN ('awaiting', 'not_recorded')", (acct(),)
        ).fetchone()[0],
        "window_days": window_days,
    }


def list_audit(db, license_id=None, limit=100):
    sql = (
        "SELECT a.*, p.name AS product_name, c.name AS customer_name "
        "FROM audit_events a "
        "LEFT JOIN licenses l ON l.id = a.license_id "
        "LEFT JOIN products p ON p.id = l.product_id "
        "LEFT JOIN customers c ON c.id = l.customer_id "
        "WHERE a.account_id = ?"
    )
    params = [acct()]
    if license_id is not None:
        sql += " AND a.license_id = ?"
        params.append(license_id)
    sql += " ORDER BY a.id DESC LIMIT ?"
    params.append(limit)
    return db.execute(sql, params).fetchall()


# --------------------------------------------------------------------------- licenses: write

def _license_fields(db, form, errors, existing=None):
    """Validate the editable license fields shared by create, edit and import."""
    product_id = parse_id(form.get("product_id"))
    product = db.execute(
        "SELECT * FROM products WHERE id = ? AND account_id = ?", (product_id, acct())
    ).fetchone() if product_id else None
    if not product:
        errors.append("Choose a valid product.")
    customer_id = parse_id(form.get("customer_id"))
    if not customer_id or not db.execute(
        "SELECT 1 FROM customers WHERE id = ? AND account_id = ?", (customer_id, acct())
    ).fetchone():
        errors.append("Choose a valid customer.")
        customer_id = None
    order_id = parse_id(form.get("order_id")) if str(form.get("order_id") or "").strip() else None
    if order_id:
        order = db.execute("SELECT customer_id FROM orders WHERE id = ? AND account_id = ?",
                           (order_id, acct())).fetchone()
        if not order:
            errors.append("Choose a valid order.")
        elif customer_id and order["customer_id"] != customer_id:
            errors.append("The selected order belongs to a different customer.")

    license_type = (form.get("license_type") or "").strip()
    if not license_type and product:
        license_type = product["default_license_type"]
    if license_type not in LICENSE_TYPES:
        errors.append("License type must be lifetime, fixed, or manual.")

    issue_date = parse_date(form.get("issue_date"), "Issue date", errors, required=True)
    expiration_date = parse_date(form.get("expiration_date"), "Expiration date", errors)
    term_days = parse_int(form.get("term_days"), "Term (days)", errors)

    if license_type == "lifetime":
        if expiration_date:
            errors.append("Lifetime licenses cannot have an expiration date.")
        expiration_date = None
    elif license_type == "fixed" and not expiration_date:
        term_days = term_days or (product["default_term_days"] if product else None)
        if term_days and issue_date:
            try:
                expiration_date = shift_date(date.fromisoformat(issue_date), days=term_days).isoformat()
            except ValidationError as exc:
                errors.extend(exc.errors)
        elif issue_date:
            errors.append("Fixed-term licenses need an expiration date or a term length.")
    if issue_date and expiration_date and expiration_date < issue_date:
        errors.append("Expiration date cannot be before the issue date.")

    notes = clean_text(form.get("notes"), "Notes", errors, max_len=TEXT_MAX)
    if order_id and product and not errors:
        _check_order_assignment(db, order_id, product, existing, errors)
    return {
        "product_id": product_id,
        "customer_id": customer_id,
        "order_id": order_id,
        "license_type": license_type,
        "issue_date": issue_date,
        "expiration_date": expiration_date,
        "notes": notes,
        "product_name": product["name"] if product else "",
    }


def _check_order_assignment(db, order_id, product, existing, errors):
    """A license may only be put on an order that is open, and — when the order
    lists its items — only for a product on the order and within its quantity."""
    unchanged = existing is not None and existing["order_id"] == order_id and existing["product_id"] == product["id"]
    if unchanged:
        return
    order = db.execute("SELECT order_ref, order_status FROM orders WHERE id = ? AND account_id = ?",
                       (order_id, acct())).fetchone()
    if order["order_status"] != "open":
        errors.append(f"Order {order['order_ref']} is {order['order_status']}; licenses can only be added to open orders.")
        return
    items = db.execute("SELECT product_id, quantity FROM order_items WHERE order_id = ? AND account_id = ?",
                       (order_id, acct())).fetchall()
    if not items:
        return  # order without itemised products: any product may be assigned
    item = next((i for i in items if i["product_id"] == product["id"]), None)
    if not item:
        errors.append(f"{product['name']} is not on order {order['order_ref']}. Add it to the order's items first.")
        return
    assigned = _live_licenses_for(db, order_id, product["id"], exclude_id=existing["id"] if existing else None)
    if assigned >= item["quantity"]:
        errors.append(
            f"Order {order['order_ref']} already has {assigned} of {item['quantity']} license(s) for "
            f"{product['name']}. Increase the quantity first if the customer bought more seats."
        )


def _unique_code(db):
    for _ in range(25):
        code = security.generate_license_code()
        if not db.execute("SELECT 1 FROM licenses WHERE account_id = ? AND key_hash = ?",
                          (acct(), security.hash_key(code))).fetchone():
            return code
    raise RuntimeError("Could not generate a unique license code")  # pragma: no cover


def _insert_license(db, values, key=None):
    """Insert a license row. With key=None a fresh code is generated; if the
    UNIQUE(key_hash) constraint still fires (e.g. a concurrent insert) a new
    code is drawn. Returns (license_id, plaintext_key)."""
    for _ in range(5):
        code = key or _unique_code(db)
        row = dict(values, account_id=acct(), key_hash=security.hash_key(code), key_hint=security.key_hint(code))
        cols = ", ".join(row)
        try:
            cur = db.execute(
                f"INSERT INTO licenses ({cols}) VALUES ({', '.join('?' * len(row))})", list(row.values())
            )
        except sqlite3.IntegrityError as exc:
            if "key_hash" not in str(exc):
                raise
            if key:
                raise ValidationError("A license with this key already exists.")
            continue
        return cur.lastrowid, code
    raise RuntimeError("Could not store a unique license code")  # pragma: no cover


def find_duplicate_license(db, fields):
    """An existing license with the same product, customer, order and issue date."""
    return db.execute(
        "SELECT id FROM licenses WHERE account_id = ? AND product_id = ? AND customer_id = ? AND order_id IS ? "
        "AND issue_date = ? ORDER BY id LIMIT 1",
        (acct(), fields["product_id"], fields["customer_id"], fields["order_id"], fields["issue_date"]),
    ).fetchone()


def _normalise_initial_status(status, fields, errors):
    """Reject contradictory status/type combinations at creation."""
    if status not in CREATE_STATUSES:
        if status == "replaced":
            errors.append("Status 'replaced' can only be set by the Replace action; use revoked instead.")
        else:
            errors.append("Status must be active, suspended, expired, or revoked.")
        return status
    if status == "expired":
        if fields["license_type"] == "lifetime":
            errors.append("Lifetime licenses cannot be expired.")
        elif fields["license_type"] == "fixed":
            exp = fields["expiration_date"]
            if exp and exp < get_today().isoformat():
                return "active"  # fixed-term expiry is derived from the date
            if exp:
                errors.append("A fixed-term license with a future expiration date cannot be marked expired.")
    return status


def create_license(db, form, key=None, source="Issued manually"):
    """Create a license. Returns (license_id, plaintext_key).

    If ``key`` is given it is treated as an existing key (e.g. from an import);
    otherwise a new random code is generated. Only the hash and hint are stored.
    Creating a second license with the same product, customer, order and issue
    date requires form["allow_duplicate"] (e.g. an extra seat).
    """
    errors = []
    fields = _license_fields(db, form, errors)
    status = _normalise_initial_status((form.get("status") or "active").strip().lower(), fields, errors)
    if key is not None and (key := security.normalize_key(key)):
        problem = security.validate_imported_key(key)
        if problem:
            errors.append(problem)
        elif db.execute("SELECT 1 FROM licenses WHERE account_id = ? AND key_hash = ?",
                        (acct(), security.hash_key(key))).fetchone():
            errors.append("A license with this key already exists.")
    else:
        key = None
    # Only imports of already-fulfilled records may start as delivered; in the
    # app, delivery is always recorded with mark_delivered after sending.
    delivery = (form.get("delivery_status") or "pending").strip().lower()
    if delivery not in DELIVERY_STATUSES:
        errors.append("Delivery status must be pending or delivered.")
    if not errors and not form.get("allow_duplicate"):
        dup = find_duplicate_license(db, fields)
        if dup:
            errors.append(
                f"Possible duplicate: license #{dup['id']} already has the same product, customer, order and "
                "issue date. Tick “additional seat” to create another one anyway."
            )
    if errors:
        raise ValidationError(errors)

    ts = now_iso()
    values = {
        "product_id": fields["product_id"], "customer_id": fields["customer_id"], "order_id": fields["order_id"],
        "license_type": fields["license_type"], "issue_date": fields["issue_date"],
        "expiration_date": fields["expiration_date"], "status": status, "notes": fields["notes"],
        "created_at": ts, "updated_at": ts,
    }
    if status == "revoked":
        values.update(revoked_at=ts, revocation_reason=f"Recorded as revoked ({source.lower()})")
    if delivery == "delivered":
        values.update(delivery_status="delivered", delivered_at=ts, delivery_method="other",
                      delivery_reference=f"Recorded as already delivered ({source.lower()})")
    license_id, key = _insert_license(db, values, key)
    expiry = f", expires {fields['expiration_date']}" if fields["expiration_date"] else ""
    audit(
        db, "created",
        f"{source}: {LICENSE_TYPES[fields['license_type']]} license for {fields['product_name']}{expiry}",
        license_id=license_id,
    )
    if status != "active":
        audit(db, "status", f"Initial status set to {status}", license_id=license_id)
    return license_id, key


def update_license(db, license_id, form):
    current = get_license(db, license_id)
    errors = []
    if current["status"] in TERMINAL_STATUSES:
        # Revoked and replaced licenses are locked so their terms can't be
        # rewritten after the fact; only internal notes may change.
        form = {**{k: current[k] for k in ("product_id", "customer_id", "order_id", "license_type",
                                           "issue_date", "expiration_date")},
                "notes": form.get("notes", current["notes"])}
        form = {k: ("" if v is None else v) for k, v in form.items()}
    fields = _license_fields(db, form, errors, existing=current)
    if errors:
        raise ValidationError(errors)
    labels = {
        "product_id": "product", "customer_id": "customer", "order_id": "order",
        "license_type": "type", "issue_date": "issue date", "expiration_date": "expiration",
        "notes": "notes",
    }
    changes = []
    for col, label in labels.items():
        if current[col] != fields[col]:
            if col == "notes":
                changes.append("notes updated")
            elif col.endswith("_id"):
                changes.append(f"{label} changed")
            else:
                changes.append(f"{label}: {current[col] or '—'} → {fields[col] or '—'}")
    if not changes:
        return False
    db.execute(
        "UPDATE licenses SET product_id = ?, customer_id = ?, order_id = ?, license_type = ?, "
        "issue_date = ?, expiration_date = ?, notes = ?, updated_at = ? WHERE id = ? AND account_id = ?",
        (
            fields["product_id"], fields["customer_id"], fields["order_id"], fields["license_type"],
            fields["issue_date"], fields["expiration_date"], fields["notes"], now_iso(), license_id, acct(),
        ),
    )
    audit(db, "edited", "; ".join(changes), license_id=license_id)
    return True


def renew_license(db, license_id, days=None, new_expiration=None, note="", months=None):
    """Extend a fixed-term or manual license and record it in the renewal history.

    Use ``days`` or calendar ``months`` (extension starts from the current end
    date, or today if that has passed), or an explicit ``new_expiration``.
    """
    lic = get_license(db, license_id)
    errors = []
    if lic["license_type"] == "lifetime":
        errors.append("Lifetime licenses do not expire and cannot be renewed.")
    if lic["status"] in TERMINAL_STATUSES:
        errors.append(f"A {lic['status']} license cannot be renewed.")
    note = clean_text(note, "Note", errors, max_len=TEXT_MAX)
    if errors:
        raise ValidationError(errors)

    today = get_today()
    current = date.fromisoformat(lic["expiration_date"]) if lic["expiration_date"] else None
    period = "custom date"
    if (new_expiration or "").strip():
        new_iso = parse_date(new_expiration, "New expiration date", errors)
        if new_iso and current and new_iso <= current.isoformat():
            errors.append("The new expiration date must be after the current one.")
        if new_iso and new_iso < today.isoformat():
            errors.append("The new expiration date cannot be in the past.")
    else:
        new_iso = None
        if months not in (None, ""):
            months = parse_int(months, "Renewal length (months)", errors, maximum=MAX_TERM_MONTHS)
            days = None
        else:
            months = None
            days = parse_int(days, "Renewal length (days)", errors, required=True)
        if days or months:
            base = max(today, current) if current else today
            try:
                new_iso = shift_date(base, days=days or 0, months=months or 0).isoformat()
            except ValidationError as exc:
                errors.extend(exc.errors)
            period = describe_period(days, months)
    if errors:
        raise ValidationError(errors)

    new_status = "active" if lic["status"] == "expired" else lic["status"]
    ts = now_iso()
    db.execute(
        "UPDATE licenses SET expiration_date = ?, status = ?, updated_at = ? WHERE id = ? AND account_id = ?",
        (new_iso, new_status, ts, license_id, acct()),
    )
    db.execute(
        "INSERT INTO renewals (account_id, license_id, previous_expiration, new_expiration, period, "
        "previous_status, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (acct(), license_id, lic["expiration_date"], new_iso, period, lic["effective_status"], note, ts),
    )
    detail = f"Expiration {lic['expiration_date'] or 'none'} → {new_iso}"
    if note:
        detail += f". {note}"
    audit(db, "renewed", detail, license_id=license_id)
    return new_iso


def revoke_license(db, license_id, reason):
    lic = get_license(db, license_id)
    errors = []
    if lic["status"] in TERMINAL_STATUSES:
        errors.append(f"This license is already {lic['status']}.")
    reason = clean_text(reason, "Revocation reason", errors, required=True, max_len=TEXT_MAX)
    if errors:
        raise ValidationError(errors)
    ts = now_iso()
    db.execute(
        "UPDATE licenses SET status = 'revoked', revoked_at = ?, revocation_reason = ?, updated_at = ? "
        "WHERE id = ? AND account_id = ?",
        (ts, reason, ts, license_id, acct()),
    )
    audit(db, "revoked", f"Revoked (was {lic['effective_status']}). {reason}", license_id=license_id)


def set_license_status(db, license_id, new_status, reason=""):
    lic = get_license(db, license_id)
    errors = []
    allowed = ["active", "suspended"] + (["expired"] if lic["license_type"] == "manual" else [])
    if lic["status"] in TERMINAL_STATUSES:
        errors.append(f"A {lic['status']} license cannot change status.")
    elif new_status not in allowed:
        errors.append("That status change is not allowed for this license.")
    elif new_status == lic["status"]:
        errors.append(f"The license is already {new_status}.")
    reason = clean_text(reason, "Reason", errors, max_len=TEXT_MAX)
    if errors:
        raise ValidationError(errors)
    db.execute(
        "UPDATE licenses SET status = ?, updated_at = ? WHERE id = ? AND account_id = ?",
        (new_status, now_iso(), license_id, acct())
    )
    detail = f"Status {lic['status']} → {new_status}"
    if reason:
        detail += f". {reason}"
    audit(db, "status", detail, license_id=license_id)


def replace_license(db, license_id, reason):
    """Retire a license and issue a new key with the same terms.

    The new license is issued today and keeps the old end date, so the
    customer's paid term is unchanged. Expired licenses must be renewed first.
    Returns (new_license_id, new_plaintext_key).
    """
    lic = get_license(db, license_id)
    errors = []
    today = get_today().isoformat()
    if lic["status"] in TERMINAL_STATUSES:
        errors.append(f"A {lic['status']} license cannot be replaced.")
    elif lic["order_status"] == "cancelled":
        errors.append(f"Order {lic['order_ref']} is cancelled; revoke the license instead of replacing it.")
    elif lic["effective_status"] == "expired" or (lic["expiration_date"] and lic["expiration_date"] < today):
        errors.append("This license has expired. Renew it first, then replace the key.")
    reason = clean_text(reason, "Replacement reason", errors, required=True, max_len=TEXT_MAX)
    if errors:
        raise ValidationError(errors)

    ts = now_iso()
    new_id, key = _insert_license(db, {
        "product_id": lic["product_id"], "customer_id": lic["customer_id"], "order_id": lic["order_id"],
        "license_type": lic["license_type"], "issue_date": today, "expiration_date": lic["expiration_date"],
        "status": "suspended" if lic["status"] == "suspended" else "active", "replaces_id": license_id,
        "notes": f"Replacement for license #{license_id}", "created_at": ts, "updated_at": ts,
    })
    db.execute(
        "UPDATE licenses SET status = 'replaced', replaced_by_id = ?, updated_at = ? WHERE id = ? AND account_id = ?",
        (new_id, ts, license_id, acct()),
    )
    audit(db, "replaced", f"Replaced by license #{new_id} (key ending {lic['key_hint']} retired). {reason}",
          license_id=license_id)
    audit(db, "created", f"Replacement for license #{license_id}; same terms, new key (delivery pending). {reason}",
          license_id=new_id)
    return new_id, key


def mark_delivered(db, license_id, method, reference="", allow_unpaid=False):
    """Record that the owner sent this license to the customer.

    Guards against duplicate delivery (already delivered), delivering retired
    or suspended licenses, delivering on cancelled orders, and — unless
    explicitly allowed — delivering before payment is recorded.
    """
    lic = get_license(db, license_id)
    errors = []
    if lic["delivery_status"] == "delivered":
        raise ValidationError(
            f"License #{license_id} was already marked delivered on {lic['delivered_at']}. "
            "Nothing was changed. Use 'Reopen delivery' first if it must be sent again."
        )
    if lic["status"] in TERMINAL_STATUSES:
        errors.append(f"A {lic['status']} license must not be delivered.")
    elif lic["status"] == "suspended":
        errors.append("This license is suspended. Reactivate it before delivering.")
    if lic["order_id"]:
        if lic["order_status"] == "cancelled":
            errors.append(f"Order {lic['order_ref']} is cancelled.")
        elif lic["payment_status"] not in PAYMENT_OK_FOR_DELIVERY and not allow_unpaid:
            errors.append(
                f"Payment for order {lic['order_ref']} is '{PAYMENT_STATUSES[lic['payment_status']]}'. "
                "Record the payment first, or tick 'deliver before payment is recorded'."
            )
    if method not in DELIVERY_METHODS:
        errors.append("Choose how the license was delivered.")
    reference = clean_text(reference, "Delivery reference", errors, max_len=200)
    if reference and lic and security.hash_key(reference) == db.execute(
        "SELECT key_hash FROM licenses WHERE id = ? AND account_id = ?", (license_id, acct())
    ).fetchone()[0]:
        errors.append("The delivery reference must not be the license key itself.")
    if errors:
        raise ValidationError(errors)
    ts = now_iso()
    cur = db.execute(
        "UPDATE licenses SET delivery_status = 'delivered', delivered_at = ?, delivery_method = ?, "
        "delivery_reference = ?, updated_at = ? WHERE id = ? AND account_id = ? AND delivery_status = 'pending'",
        (ts, method, reference, ts, license_id, acct()),
    )
    if cur.rowcount != 1:  # lost a race with another submit
        raise ValidationError(f"License #{license_id} was already marked delivered. Nothing was changed.")
    detail = f"Delivered by {DELIVERY_METHODS[method].lower()}"
    if reference:
        detail += f" (reference: {reference})"
    if lic["order_id"] and lic["payment_status"] not in PAYMENT_OK_FOR_DELIVERY:
        detail += f". Delivered before payment was recorded ({PAYMENT_STATUSES[lic['payment_status']].lower()})"
    audit(db, "delivered", detail, license_id=license_id)


def reopen_delivery(db, license_id, reason):
    """Mark a delivered license as pending again (e.g. the email bounced)."""
    lic = get_license(db, license_id)
    errors = []
    if lic["delivery_status"] != "delivered":
        errors.append("This license is not marked delivered.")
    reason = clean_text(reason, "Reason", errors, required=True, max_len=TEXT_MAX)
    if errors:
        raise ValidationError(errors)
    db.execute(
        "UPDATE licenses SET delivery_status = 'pending', delivered_at = NULL, delivery_method = '', "
        "delivery_reference = '', updated_at = ? WHERE id = ? AND account_id = ?", (now_iso(), license_id, acct()),
    )
    audit(db, "delivery_reopened",
          f"Delivery reopened (was delivered {lic['delivered_at']} by {lic['delivery_method'] or 'unknown'}). {reason}",
          license_id=license_id)


def add_note(db, license_id, note):
    get_license(db, license_id)
    errors = []
    note = clean_text(note, "Note", errors, required=True, max_len=TEXT_MAX)
    if errors:
        raise ValidationError(errors)
    audit(db, "note", note, license_id=license_id)


def license_has_history(db, lic):
    """Licenses that were renewed, replaced, or revoked are kept for the audit trail."""
    if lic["replaces_id"] or lic["replaced_by_id"] or lic["status"] in TERMINAL_STATUSES:
        return True
    return db.execute("SELECT 1 FROM renewals WHERE license_id = ? AND account_id = ?",
                      (lic["id"], acct())).fetchone() is not None


def delete_license(db, license_id):
    lic = get_license(db, license_id)
    if license_has_history(db, lic):
        raise ValidationError(
            "This license has renewal, replacement, or revocation history and is kept for the audit trail. "
            "Revoke it instead of deleting it."
        )
    db.execute("DELETE FROM licenses WHERE id = ? AND account_id = ?", (license_id, acct()))
    audit(
        db, "deleted",
        f"Deleted license #{license_id} ({lic['product_name']} for {lic['customer_email']})",
        entity_type="license", entity_id=license_id,
    )


def license_chain(db, license_id):
    """Ids of this license and every license it replaced, oldest first."""
    chain, current, seen = [], license_id, set()
    while current and current not in seen:
        seen.add(current)
        chain.append(current)
        row = db.execute("SELECT replaces_id FROM licenses WHERE id = ? AND account_id = ?",
                         (current, acct())).fetchone()
        current = row["replaces_id"] if row else None
    return list(reversed(chain))


def list_renewals(db, license_ids):
    if not license_ids:
        return []
    marks = ",".join("?" * len(license_ids))
    return db.execute(
        f"SELECT * FROM renewals WHERE account_id = ? AND license_id IN ({marks}) ORDER BY id DESC",
        [acct(), *license_ids]
    ).fetchall()


# --------------------------------------------------------------------------- reminder templates

PLACEHOLDERS = {
    "customer_name": "Customer's name",
    "customer_email": "Customer's email",
    "product_name": "Product name",
    "order_ref": "Order reference",
    "issue_date": "Issue date",
    "expiration_date": "Expiration date",
    "days_left": "Days until expiration (negative if past)",
    "license_hint": "Last 4 characters of the key (never the full key)",
    "business_name": "Your business name (Settings)",
}
PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")


def list_templates(db):
    return db.execute("SELECT * FROM reminder_templates WHERE account_id = ? ORDER BY id", (acct(),)).fetchall()


def get_template(db, template_id):
    row = db.execute("SELECT * FROM reminder_templates WHERE id = ? AND account_id = ?",
                     (template_id, acct())).fetchone()
    if not row:
        raise NotFound("template")
    return row


def save_template(db, form, template_id=None):
    errors = []
    name = clean_text(form.get("name"), "Template name", errors, required=True)
    subject = clean_text(form.get("subject"), "Subject", errors, required=True, max_len=300)
    body = clean_text(form.get("body"), "Body", errors, required=True, max_len=TEXT_MAX)
    unknown = sorted(
        {m for m in PLACEHOLDER_RE.findall(subject + body) if m not in PLACEHOLDERS}
    )
    if unknown:
        errors.append("Unknown placeholders: " + ", ".join("{" + u + "}" for u in unknown))
    if name:
        clash = db.execute(
            "SELECT id FROM reminder_templates WHERE account_id = ? AND name = ? COLLATE NOCASE AND id IS NOT ?",
            (acct(), name, template_id)
        ).fetchone()
        if clash:
            errors.append("A template with this name already exists.")
    if errors:
        raise ValidationError(errors)
    if template_id is None:
        cur = db.execute(
            "INSERT INTO reminder_templates (account_id, name, subject, body, created_at) VALUES (?, ?, ?, ?, ?)",
            (acct(), name, subject, body, now_iso()),
        )
        return cur.lastrowid
    get_template(db, template_id)
    db.execute(
        "UPDATE reminder_templates SET name = ?, subject = ?, body = ? WHERE id = ? AND account_id = ?",
        (name, subject, body, template_id, acct()),
    )
    return template_id


def reminder_context(db, lic):
    left = days_until(lic["expiration_date"])
    return {
        "customer_name": lic["customer_name"],
        "customer_email": lic["customer_email"],
        "product_name": lic["product_name"],
        "order_ref": lic["order_ref"] or "n/a",
        "issue_date": lic["issue_date"],
        "expiration_date": lic["expiration_date"] or "no expiration",
        "days_left": "" if left is None else str(left),
        "license_hint": lic["key_hint"],
        "business_name": account_business_name(db) or "Your seller team",
    }


def render_reminder(db, tpl, lic, lang=None):
    """(subject, body) for a stored template filled in for a license.

    English (and any edited/user-created template) is rendered exactly as stored.
    In Thai mode an unedited built-in is rendered from its Thai version instead;
    the stored row is never changed.
    """
    from . import i18n

    thai = i18n.thai_builtin(tpl["subject"], tpl["body"]) if (lang or i18n.current_lang()) == "th" else None
    ctx = reminder_context(db, lic)
    if thai is None:
        return render_placeholders(tpl["subject"], ctx), render_placeholders(tpl["body"], ctx)
    for field, (en_value, th_value) in i18n.THAI_FALLBACKS.items():
        if ctx[field] == en_value:
            ctx[field] = th_value
    subject, body = (i18n.thai_honorific(t, ctx["customer_name"]) for t in thai)
    return render_placeholders(subject, ctx), render_placeholders(body, ctx)


def delete_template(db, template_id):
    get_template(db, template_id)
    db.execute("DELETE FROM reminder_templates WHERE id = ? AND account_id = ?", (template_id, acct()))


def account_business_name(db):
    row = db.execute("SELECT business_name FROM accounts WHERE id = ?", (acct(),)).fetchone()
    return row["business_name"] if row else ""


def account_counts(db):
    return {t: db.execute(f"SELECT COUNT(*) FROM {t} WHERE account_id = ?", (acct(),)).fetchone()[0]
            for t in ("products", "customers", "orders", "licenses")}


def render_placeholders(text, context):
    # Plain substitution only: unlike str.format there is no attribute or index access.
    return PLACEHOLDER_RE.sub(lambda m: str(context.get(m.group(1), m.group(0))), text)
