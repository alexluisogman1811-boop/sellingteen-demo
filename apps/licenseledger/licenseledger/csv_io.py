"""CSV export and validated license import with duplicate detection.

Exports never contain license keys, only the 4-character hint.
"""
import csv
import io

from . import security, services

MAX_IMPORT_ROWS = 5000
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

IMPORT_COLUMNS = [
    "customer_name", "customer_email", "customer_company", "product_name", "order_ref",
    "order_date", "license_type", "issue_date", "expiration_date", "status", "license_key", "notes",
    "delivery_status", "payment_status",
]
REQUIRED_COLUMNS = ["customer_email", "product_name", "license_type", "issue_date"]
TYPE_ALIASES = {
    "lifetime": "lifetime", "perpetual": "lifetime",
    "fixed": "fixed", "fixed-term": "fixed", "fixed_term": "fixed", "fixed term": "fixed",
    "subscription": "fixed",
    "manual": "manual", "manually managed": "manual", "manually-managed": "manual",
}

EXPORTS = {
    "licenses": (
        ["license_id", "customer_name", "customer_email", "customer_company", "product_name", "order_ref",
         "license_type", "issue_date", "expiration_date", "status", "effective_status", "key_hint", "notes",
         "replaces_license_id", "replaced_by_license_id", "revoked_at", "revocation_reason",
         "delivery_status", "delivered_at", "delivery_method", "delivery_reference", "created_at"],
        services.LICENSE_SELECT + " ORDER BY l.id",
        lambda r: [r["id"], r["customer_name"], r["customer_email"], r["customer_company"], r["product_name"],
                   r["order_ref"] or "", r["license_type"], r["issue_date"], r["expiration_date"] or "",
                   r["status"], r["effective_status"], r["key_hint"], r["notes"], r["replaces_id"] or "",
                   r["replaced_by_id"] or "", r["revoked_at"] or "", r["revocation_reason"] or "",
                   r["delivery_status"], r["delivered_at"] or "", r["delivery_method"], r["delivery_reference"],
                   r["created_at"]],
    ),
    "renewals": (
        ["renewal_id", "license_id", "customer_email", "product_name", "previous_expiration", "new_expiration",
         "period", "previous_status", "note", "renewed_at"],
        "SELECT r.*, c.email AS customer_email, p.name AS product_name FROM renewals r "
        "LEFT JOIN licenses l ON l.id = r.license_id LEFT JOIN customers c ON c.id = l.customer_id "
        "LEFT JOIN products p ON p.id = l.product_id WHERE r.account_id = :acct ORDER BY r.id",
        lambda r: [r["id"], r["license_id"] or "", r["customer_email"] or "", r["product_name"] or "",
                   r["previous_expiration"] or "", r["new_expiration"], r["period"], r["previous_status"],
                   r["note"], r["created_at"]],
    ),
    "customers": (
        ["customer_id", "name", "email", "company", "notes", "created_at"],
        "SELECT * FROM customers WHERE account_id = :acct ORDER BY id",
        lambda r: [r["id"], r["name"], r["email"], r["company"], r["notes"], r["created_at"]],
    ),
    "products": (
        ["product_id", "name", "sku", "default_license_type", "default_term_days", "notes", "created_at"],
        "SELECT * FROM products WHERE account_id = :acct ORDER BY id",
        lambda r: [r["id"], r["name"], r["sku"] or "", r["default_license_type"], r["default_term_days"] or "",
                   r["notes"], r["created_at"]],
    ),
    "orders": (
        ["order_id", "order_ref", "customer_name", "customer_email", "order_date", "amount", "order_status",
         "payment_status", "payment_reference", "payment_recorded_at", "seats_required", "delivered_count",
         "delivery_state", "notes", "created_at"],
        services.ORDER_QUERY + " ORDER BY os.id",
        lambda r: [r["id"], r["order_ref"], r["customer_name"], r["customer_email"], r["order_date"],
                   r["amount"], r["order_status"], r["payment_status"], r["payment_reference"],
                   r["payment_recorded_at"] or "", r["seats_required"], r["delivered_count"], r["delivery_state"],
                   r["notes"], r["created_at"]],
    ),
}


def safe_cell(value):
    """Neutralise spreadsheet formula injection. As a last line of defence, anything
    that looks like a generated license key is masked (text fields already refuse keys)."""
    if value is None:
        return ""
    if isinstance(value, str):
        value = security.KEY_PATTERN.sub("LL-[REDACTED]", value)
        if value.startswith(FORMULA_PREFIXES):
            return "'" + value
    return value


def rows_to_csv(headers, rows):
    """CSV text for any table of values (formula injection neutralised, keys masked)."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([safe_cell(v) for v in row])
    return out.getvalue()


def export_csv(db, entity):
    headers, sql, to_row = EXPORTS[entity]
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(headers)
    # Orders use positional parameters (account first); the others use named ones.
    params = (services.acct(),) if entity == "orders" else {"today": services.get_today().isoformat(),
                                                             "acct": services.acct()}
    for row in db.execute(sql, params):
        writer.writerow([safe_cell(v) for v in to_row(row)])
    return out.getvalue()


def import_template_csv():
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(IMPORT_COLUMNS)
    writer.writerow(["Ada Example", "ada@example.com", "Example Co", "Sample Plugin Pro", "ORD-1001",
                     "2026-01-15", "fixed", "2026-01-15", "2027-01-15", "active", "", "Imported from spreadsheet",
                     "delivered", "marked_paid"])
    writer.writerow(["Ben Example", "ben@example.com", "", "Sample Icon Pack", "ORD-1002",
                     "2026-02-01", "lifetime", "2026-02-01", "", "active", "", "", "pending", "awaiting"])
    return out.getvalue()


def _clean_row(raw):
    return {
        (k or "").strip().lower(): (v or "").strip() if isinstance(v, str) else ""
        for k, v in raw.items() if k is not None
    }


def import_licenses(db, text, dry_run=False):
    """Validate and import license rows.

    Invalid and duplicate rows are skipped and reported; valid rows are
    imported in one transaction. With dry_run the transaction is rolled back.
    Returns a report dict. Keys generated for rows without a license_key are
    returned in report["issued"] so they can be shown once.
    """
    report = {
        "dry_run": dry_run, "total_rows": 0, "created": 0, "errors": [], "duplicates": [],
        "fatal": None, "issued": [], "new_customers": 0, "new_products": 0, "new_orders": 0,
    }
    if "\x00" in text:
        report["fatal"] = "The file is not valid CSV (it contains NUL bytes, so it looks binary)."
        return report
    reader = csv.DictReader(io.StringIO(text))
    try:
        headers = [(h or "").strip().lower() for h in (reader.fieldnames or [])]
        missing = [c for c in REQUIRED_COLUMNS if c not in headers]
        if missing:
            report["fatal"] = "Missing required column(s): " + ", ".join(missing)
            return report
        named = [h for h in headers if h]
        if len(set(named)) != len(named):
            report["fatal"] = "The header row contains duplicate column names."
            return report
        rows = list(reader)
    except csv.Error as exc:
        report["fatal"] = f"The file is not valid CSV ({exc})."
        return report
    if len(rows) > MAX_IMPORT_ROWS:
        report["fatal"] = f"Too many rows ({len(rows)}). The limit is {MAX_IMPORT_ROWS} per file."
        return report
    report["total_rows"] = len(rows)

    seen_keys, seen_signatures = {}, {}
    db.commit()
    db.execute("BEGIN")
    try:
        for index, raw in enumerate(rows, start=2):  # row 1 is the header
            row = _clean_row(raw)
            if not any(row.values()):
                report["total_rows"] -= 1
                continue
            outcome = _import_row(db, row, index, seen_keys, seen_signatures, report)
            if outcome == "created":
                report["created"] += 1
        if dry_run:
            db.rollback()
            report["issued"] = []
        else:
            db.commit()
    except Exception:
        db.rollback()
        raise
    return report


def _import_row(db, row, index, seen_keys, seen_signatures, report):
    errors = []
    email = services.validate_email(row.get("customer_email"), errors)
    customer = db.execute("SELECT * FROM customers WHERE account_id = ? AND email = ? COLLATE NOCASE",
                          (services.acct(), email)).fetchone() if email else None
    customer_name = services.clean_text(row.get("customer_name"), "customer_name", errors)
    if not customer and not customer_name:
        errors.append("customer_name is required for a new customer.")
    product_name = services.clean_text(row.get("product_name"), "product_name", errors, required=True)
    license_type = TYPE_ALIASES.get((row.get("license_type") or "").lower())
    if not license_type:
        errors.append("license_type must be lifetime, fixed, or manual.")
    issue_date = services.parse_date(row.get("issue_date"), "issue_date", errors, required=True)
    services.parse_date(row.get("expiration_date"), "expiration_date", errors)
    order_date = services.parse_date(row.get("order_date"), "order_date", errors)
    status = (row.get("status") or "active").lower()
    if status == "replaced":
        errors.append("status 'replaced' cannot be imported (replacement links aren't in the file); use revoked.")
    elif status not in services.CREATE_STATUSES:
        errors.append("status must be active, suspended, expired, or revoked.")
    order_ref = services.clean_text(row.get("order_ref"), "order_ref", errors, max_len=100)
    key = security.normalize_key(row.get("license_key"))
    if key:
        problem = security.validate_imported_key(key)
        if problem:
            errors.append(problem)
    delivery = (row.get("delivery_status") or "pending").lower()
    if delivery not in services.DELIVERY_STATUSES:
        errors.append("delivery_status must be pending or delivered.")
    payment = (row.get("payment_status") or "not_recorded").lower()
    if payment not in services.PAYMENT_STATUSES:
        errors.append("payment_status must be one of: " + ", ".join(services.PAYMENT_STATUSES) + ".")
    order = None
    if order_ref:
        order = db.execute("SELECT * FROM orders WHERE account_id = ? AND order_ref = ? COLLATE NOCASE",
                           (services.acct(), order_ref)).fetchone()
        if order and customer and order["customer_id"] != customer["id"]:
            errors.append(f"Order {order_ref} already exists for a different customer.")
        elif order and not customer:
            errors.append(f"Order {order_ref} already exists for a different customer.")
    if errors:
        report["errors"].append((index, errors))
        return "error"

    # Duplicate detection: the same key, or the same customer + product + order + issue date.
    signature = (email, product_name.lower(), order_ref.lower(), issue_date)
    if key:
        key_hash = security.hash_key(key)
        if key_hash in seen_keys:
            report["duplicates"].append((index, f"Same license key as row {seen_keys[key_hash]}."))
            return "duplicate"
        if db.execute("SELECT 1 FROM licenses WHERE account_id = ? AND key_hash = ?",
                      (services.acct(), key_hash)).fetchone():
            report["duplicates"].append((index, "A license with this key already exists."))
            return "duplicate"
    if signature in seen_signatures:
        report["duplicates"].append((index, f"Same customer, product, order and issue date as row {seen_signatures[signature]}."))
        return "duplicate"
    existing = db.execute(
        "SELECT l.id FROM licenses l JOIN customers c ON c.id = l.customer_id "
        "JOIN products p ON p.id = l.product_id LEFT JOIN orders o ON o.id = l.order_id "
        "WHERE l.account_id = ? AND c.email = ? COLLATE NOCASE AND p.name = ? COLLATE NOCASE "
        "AND COALESCE(o.order_ref, '') = ? COLLATE NOCASE AND l.issue_date = ?",
        (services.acct(), *signature),
    ).fetchone()
    if existing:
        report["duplicates"].append((index, f"Matches existing license #{existing['id']}."))
        return "duplicate"

    db.execute("SAVEPOINT import_row")
    try:
        if not customer:
            customer_id = services.save_customer(
                db, {"name": customer_name, "email": email, "company": row.get("customer_company")}
            )
            report["new_customers"] += 1
        else:
            customer_id = customer["id"]
        product = db.execute("SELECT * FROM products WHERE account_id = ? AND name = ? COLLATE NOCASE",
                             (services.acct(), product_name)).fetchone()
        if product:
            product_id = product["id"]
        else:
            product_id = services.save_product(db, {"name": product_name, "default_license_type": license_type})
            report["new_products"] += 1
        if order_ref and not order:
            order_id = services.save_order(
                db, {"order_ref": order_ref, "customer_id": customer_id, "order_date": order_date or issue_date,
                     "payment_status": payment}
            )
            report["new_orders"] += 1
        else:
            order_id = order["id"] if order else None
        license_id, issued_key = services.create_license(
            db,
            {
                "product_id": product_id, "customer_id": customer_id, "order_id": order_id or "",
                "license_type": license_type, "issue_date": issue_date,
                "expiration_date": row.get("expiration_date"), "status": status, "notes": row.get("notes"),
                "delivery_status": delivery,
            },
            key=key or None,
            source="Imported from CSV",
        )
    except services.ValidationError as exc:
        db.execute("ROLLBACK TO import_row")
        db.execute("RELEASE import_row")
        report["errors"].append((index, exc.errors))
        return "error"
    db.execute("RELEASE import_row")
    seen_signatures[signature] = index
    seen_keys[security.hash_key(issued_key)] = index
    if not key:
        report["issued"].append({"row": index, "license_id": license_id, "key": issued_key,
                                 "customer": email, "product": product_name})
    return "created"


def import_summary(report):
    return (
        f"{report['created']} imported, {len(report['duplicates'])} duplicate(s) skipped, "
        f"{len(report['errors'])} row(s) with errors" + (" (validation only — nothing saved)" if report["dry_run"] else "")
    )
