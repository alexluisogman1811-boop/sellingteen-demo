"""Demo data and safe reset (backup first, keep login and templates)."""
import os
import sqlite3
from datetime import datetime, timedelta

from . import services
from .db import DATA_TABLES

DEMO_PRODUCTS = [
    {"name": "PixelForge Pro", "sku": "PF-PRO", "default_license_type": "fixed", "default_term_days": "365",
     "notes": "Annual desktop app license"},
    {"name": "Icon Atlas", "sku": "ICON-ATLAS", "default_license_type": "lifetime", "notes": "One-time purchase"},
    {"name": "Docs Theme Starter", "sku": "DOCS-THEME", "default_license_type": "fixed", "default_term_days": "90",
     "notes": "Quarterly update access"},
    {"name": "Agency Plugin Bundle", "sku": "AGENCY-BNDL", "default_license_type": "manual",
     "notes": "Custom agreements, managed by hand"},
]

DEMO_CUSTOMERS = [
    {"name": "Alex Rivera", "email": "alex.rivera@example.com", "company": "Rivera Studio"},
    {"name": "Priya Natarajan", "email": "priya@example.org", "company": ""},
    {"name": "Sam Okafor", "email": "sam.okafor@example.net", "company": "Okafor Digital"},
    {"name": "Mei Chen", "email": "mei.chen@example.com", "company": "Northwind Agency"},
    {"name": "Jordan Blake", "email": "jordan@example.com", "company": ""},
    {"name": "Lena Fischer", "email": "lena.fischer@example.de", "company": "Fischer & Co"},
]

# (customer, product, order ref, issue offset days, type, expiration offset or None, status, note,
#  payment status, delivered?)
DEMO_LICENSES = [
    (0, 0, "DEMO-1001", -358, "fixed", 7, "active", "Asked about team pricing at renewal.", "marked_paid", True),
    (1, 0, "DEMO-1002", -340, "fixed", 25, "active", "", "marked_paid", True),
    (2, 1, "DEMO-1003", -200, "lifetime", None, "active", "", "marked_paid", True),
    (3, 3, "DEMO-1004", -120, "manual", 60, "active", "Agency agreement reviewed quarterly.", "not_required", True),
    (4, 2, "DEMO-1005", -100, "fixed", -10, "active", "Did not respond to first reminder.", "marked_paid", True),
    (5, 0, "DEMO-1006", -400, "fixed", -35, "active", "", "marked_paid", True),
    (0, 1, "DEMO-1007", -60, "lifetime", None, "active", "", "marked_paid", True),
    (2, 2, "DEMO-1008", -2, "fixed", 88, "active", "Waiting for bank transfer.", "awaiting", False),
    (3, 0, "DEMO-1009", -1, "fixed", 364, "active", "", "marked_paid", False),
    (1, 2, "DEMO-1010", -80, "fixed", 10, "suspended", "Payment dispute — on hold.", "awaiting", False),
]


def account_backup_prefix(account_id):
    return f"licenseledger-account{int(account_id)}-"


def backup_database(db_path, backup_dir, account_id):
    """Save a copy of ONE account's data (taken with SQLite's online backup API,
    then every other account's rows are removed from the copy)."""
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = os.path.join(backup_dir, f"{account_backup_prefix(account_id)}{stamp}.db")
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        dst.execute("PRAGMA foreign_keys = OFF")
        for table in DATA_TABLES + ["reminder_templates"]:
            dst.execute(f"DELETE FROM {table} WHERE account_id != ?", (account_id,))
        dst.execute("DELETE FROM accounts WHERE id != ?", (account_id,))
        dst.execute("DELETE FROM settings")
        dst.commit()
        dst.execute("VACUUM")
    except Exception:
        dst.close()
        os.remove(target)
        raise
    finally:
        src.close()
    dst.close()
    return target


def delete_account_backups(backup_dir, account_id):
    """Remove the reset backups of one account (used when the account is deleted)."""
    if not os.path.isdir(backup_dir):
        return 0
    prefix = account_backup_prefix(account_id)
    removed = 0
    for name in os.listdir(backup_dir):
        if name.startswith(prefix) and name.endswith(".db"):
            os.remove(os.path.join(backup_dir, name))
            removed += 1
    return removed


def clear_data(db):
    """Delete the signed-in account's business data (never other accounts')."""
    account_id = services.acct()
    for table in DATA_TABLES:  # children first, so foreign keys are satisfied
        db.execute(f"DELETE FROM {table} WHERE account_id = ?", (account_id,))


def load_demo(db):
    """Replace all business data with demo records dated relative to today."""
    clear_data(db)
    today = services.get_today()
    product_ids = [services.save_product(db, p) for p in DEMO_PRODUCTS]
    customer_ids = [services.save_customer(db, c) for c in DEMO_CUSTOMERS]
    license_ids = []
    for cust, prod, ref, issue_off, ltype, exp_off, status, note, payment, delivered in DEMO_LICENSES:
        issue = (today + timedelta(days=issue_off)).isoformat()
        order_id = services.save_order(
            db, {"order_ref": ref, "customer_id": customer_ids[cust], "order_date": issue,
                 "amount": "USD 49.00", "notes": "Demo order", "payment_status": payment,
                 "payment_reference": "DEMO-INVOICE" if payment == "marked_paid" else "",
                 "item_product_id": product_ids[prod], "item_quantity": 1}
        )
        lic_id, _key = services.create_license(
            db,
            {
                "product_id": product_ids[prod], "customer_id": customer_ids[cust], "order_id": order_id,
                "license_type": ltype, "issue_date": issue,
                "expiration_date": (today + timedelta(days=exp_off)).isoformat() if exp_off is not None else "",
                "status": status, "notes": note,
            },
            source="Demo data",
        )
        if delivered:
            services.mark_delivered(db, lic_id, "email", f"Demo email for {ref}")
        license_ids.append(lic_id)

    # A little history: a renewal, a replacement, a revocation and a note.
    services.renew_license(db, license_ids[8], days=30, note="Goodwill extension after support issue.")
    services.replace_license(db, license_ids[6], "Customer reported key shared publicly by mistake.")
    services.revoke_license(db, license_ids[5], "Refund issued; license revoked.")
    refunded_order = services.get_license(db, license_ids[5])["order_id"]
    services.set_payment_status(db, refunded_order, "refunded", note="Refund issued by owner (demo).")
    services.set_order_status(db, refunded_order, "cancelled", "Refunded (demo).")
    services.set_order_status(db, services.get_license(db, license_ids[2])["order_id"], "completed")
    services.add_note(db, license_ids[0], "Customer prefers email reminders two weeks before expiry.")
    return len(license_ids)
