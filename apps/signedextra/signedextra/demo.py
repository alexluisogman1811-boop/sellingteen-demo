"""Demo data. Everything created here is fictional and labeled as such in the UI."""
import math
import struct
import zlib
from decimal import Decimal

from flask import current_app, g
from werkzeug.datastructures import MultiDict
from werkzeug.security import generate_password_hash

from .db import get_db
from .security import audit
from .signing import prepare_notice, record_signature
from .tickets_service import create_ticket, get_job, get_ticket
from .util import now_iso
from .validation import Form

DEMO_ACCOUNTS = [
    ("owner", "owner@demo.signedextra.test", "demo-owner-pass"),
    ("office", "office@demo.signedextra.test", "demo-office-pass"),
    ("foreman", "foreman@demo.signedextra.test", "demo-foreman-pass"),
]


def demo_signature_png(width=240, height=60):
    """A small valid PNG with a wavy line, standing in for a drawn signature."""
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            on_line = abs(y - (height // 2 + int(10 * math.sin(x / 12)))) <= 1
            row += bytes([0, 0, 0, 255]) if on_line else bytes([255, 255, 255, 0])
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


def seed_demo():
    db = get_db()
    if db.execute("SELECT 1 FROM users WHERE email = ?", (DEMO_ACCOUNTS[0][1],)).fetchone():
        return DEMO_ACCOUNTS
    now = now_iso()
    cid = db.execute(
        "INSERT INTO companies (name, address, phone, email, is_demo, created_at) VALUES (?, ?, ?, ?, 1, ?)",
        ("Demo Electric (sample data)", "123 Example St, Sampletown", "555-0100", "office@demo.signedextra.test",
         now)).lastrowid
    users = {}
    for role, email, password in DEMO_ACCOUNTS:
        name = {"owner": "Demo Owner", "office": "Demo Office", "foreman": "Demo Foreman"}[role]
        users[role] = db.execute(
            "INSERT INTO users (company_id, name, email, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (cid, name, email, generate_password_hash(password), role, now)).lastrowid
    rates = {}
    for label, cents in (("Journeyman", 9500), ("Apprentice", 6000), ("Foreman", 11000)):
        rates[label] = db.execute("INSERT INTO labor_rates (company_id, label, rate_cents) VALUES (?, ?, ?)",
                                  (cid, label, cents)).lastrowid
    gc = db.execute("INSERT INTO customers (company_id, name, address, notes, created_at) VALUES (?, ?, ?, ?, ?)",
                    (cid, "Sample General Contractor (fictional)", "1 Demo Plaza",
                     "Demo only: T&M markup 15%, written notice within 48 hours.", now)).lastrowid
    db.execute("INSERT INTO customer_contacts (company_id, customer_id, name, title, email, phone, receives_notices)"
               " VALUES (?, ?, ?, ?, ?, ?, 1)", (cid, gc, "Sam Superintendent (demo)", "Superintendent",
                                                 "super@demo-gc.test", "555-0101"))
    job_id = db.execute(
        "INSERT INTO jobs (company_id, customer_id, name, address, gc_reference, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)", (cid, gc, "Demo Office Fit-Out, Suite 200", "1 Demo Plaza", "DEMO-PO-001",
                                       now)).lastrowid
    db.execute("INSERT INTO job_members (job_id, user_id) VALUES (?, ?)", (job_id, users["foreman"]))
    audit(cid, "demo.seeded", actor="system", user_id=None)
    db.commit()

    company = db.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone()
    owner = db.execute("SELECT * FROM users WHERE id = ?", (users["owner"],)).fetchone()
    with current_app.test_request_context("/"):
        g.user, g.company = owner, company
        job = get_job(job_id, cid)

        def make(desc, who, role, labor, material):
            lines = [{"kind": "labor", "labor_rate_id": rates[k], "description": "", "qty": Decimal(q),
                      "unit": "hr", "rate": None} for k, q in labor]
            lines += [{"kind": "material", "labor_rate_id": None, "description": d, "qty": Decimal(q),
                       "unit": u, "rate": Decimal(r)} for d, q, u, r in material]
            fields = {"description": desc, "requested_by_name": who, "requested_by_role": role,
                      "work_date": now[:10]}
            company_now = db.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone()
            return create_ticket(company_now, job, fields, lines, include_prices=True)

        make("Add two dedicated 20A circuits for relocated copier room (demo).", "Sam Superintendent (demo)",
             "Superintendent", [("Journeyman", "6"), ("Apprentice", "6")],
             [("12/2 MC cable", "150", "ft", "1.10"), ("20A breaker", "2", "ea", "14.00")])
        signed_id = make("Relocate 4 receptacles after wall layout change (demo).", "Sam Superintendent (demo)",
                         "Superintendent", [("Journeyman", "4")], [("Duplex receptacle", "4", "ea", "3.50")])
        notice_id = make("Re-pull circuit damaged by drywall crew (demo).", "Sam Superintendent (demo)",
                         "Superintendent", [("Journeyman", "3"), ("Foreman", "1")],
                         [("12/2 MC cable", "60", "ft", "1.10")])

        form = Form(MultiDict({"signer_name": "Sam Superintendent (demo)", "signer_title": "Superintendent"}))
        form.text("signer_name", "Name")
        form.text("signer_title", "Title")
        record_signature(get_ticket(signed_id, cid), "on_device", "signed", form, demo_signature_png())
        prepare_notice(get_ticket(notice_id, cid), company, job, "refused",
                       "Demo: superintendent said the damage was not their trade's fault.")
    return DEMO_ACCOUNTS
