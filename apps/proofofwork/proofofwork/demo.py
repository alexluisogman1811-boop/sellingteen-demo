"""Demo data and safe reset, always for ONE account. Reset only touches that account's business data and its own
photo files; users and every other account are left alone."""
import io
import random
from datetime import date, timedelta

from PIL import Image, ImageDraw

from . import storage
from .db import get_db
from .services import log_event
from .utils import now_iso

BUSINESS_TABLES = ("job_events", "share_links", "photos", "job_items", "jobs", "template_items", "templates",
                   "properties", "customers")

TEMPLATES = [
    ("Standard office clean", "Weekly clean for small offices.",
     [("Empty all bins and replace liners", 1), ("Vacuum carpets and mop hard floors", 1),
      ("Wipe desks and shared surfaces", 1), ("Clean and restock kitchen", 1),
      ("Clean and restock washrooms", 1), ("Wipe internal glass partitions", 0)]),
    ("End of tenancy deep clean", "Full property clean between tenants.",
     [("Degrease oven, hob and extractor", 1), ("Clean inside all cupboards and drawers", 1),
      ("Descale bathroom fixtures", 1), ("Wash skirting boards and doors", 1),
      ("Clean windows inside", 1), ("Steam clean carpets", 0)]),
    ("HVAC filter maintenance", "Quarterly filter swap and visual inspection.",
     [("Isolate unit power", 1), ("Replace air filters", 1), ("Clean condensate drain", 1),
      ("Inspect belts and fittings", 1), ("Record pressure readings", 0)]),
]

CUSTOMERS = [
    ("Harbour Dental Group", "Priya Nair", "priya@harbourdental.example", "555-0101",
     [("Harbour Dental – Main St", "14 Main Street, Springfield", "Alarm code held by office manager"),
      ("Harbour Dental – Riverside", "2 River Walk, Springfield", "")]),
    ("Oakview Lettings", "Sam Turner", "sam@oakview.example", "555-0144",
     [("Flat 3B, Elm Court", "3B Elm Court, Springfield", "Keys from lockbox – ask office for code")]),
    ("Northgate Business Centre", "Jo Alvarez", "facilities@northgate.example", "555-0190",
     [("Northgate – Building A", "100 Northgate Road, Springfield", "Sign in at reception")]),
]


def _demo_image(label, after, seed):
    rng = random.Random(seed)
    base = (226, 238, 233) if after else (150, 132, 104)
    img = Image.new("RGB", (960, 720), base)
    draw = ImageDraw.Draw(img)
    draw.rectangle([60, 420, 900, 680], fill=(250, 250, 248) if after else (176, 160, 130))
    draw.rectangle([120, 120, 420, 380], outline=(90, 110, 100), width=6)
    if not after:
        for _ in range(140):
            x, y, r = rng.randint(40, 920), rng.randint(40, 680), rng.randint(4, 22)
            draw.ellipse([x - r, y - r, x + r, y + r], fill=(95 + rng.randint(0, 30), 78, 55))
    draw.rectangle([0, 0, 960, 70], fill=(31, 79, 70) if after else (92, 64, 51))
    draw.text((24, 24), f"{'AFTER' if after else 'BEFORE'} - {label} (demo image)", fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return buf.getvalue()


def _insert_demo(db, written, account_id):
    now = now_iso()
    today = date.today()
    template_ids = []
    for name, desc, tasks in TEMPLATES:
        cur = db.execute("INSERT INTO templates (account_id, name, description, created_at) VALUES (?, ?, ?, ?)",
                         (account_id, name, desc, now))
        template_ids.append(cur.lastrowid)
        db.executemany("INSERT INTO template_items (account_id, template_id, position, label, required)"
                       " VALUES (?, ?, ?, ?, ?)",
                       [(account_id, cur.lastrowid, pos, label, req) for pos, (label, req) in enumerate(tasks)])
    props = []
    for name, contact, email, phone, properties in CUSTOMERS:
        cur = db.execute("INSERT INTO customers (account_id, name, contact_name, email, phone, created_at)"
                         " VALUES (?, ?, ?, ?, ?, ?)",
                         (account_id, name + " (Demo)", contact, email, phone, now))  # demo records are always labelled
        cid = cur.lastrowid
        for pname, address, access in properties:
            pcur = db.execute("INSERT INTO properties (account_id, customer_id, name, address, access_notes, created_at)"
                              " VALUES (?, ?, ?, ?, ?, ?)", (account_id, cid, pname, address, access, now))
            props.append((cid, pcur.lastrowid))

    # (title, property index, template index, worker, day offset, start, end, status, item states)
    jobs = [
        ("Weekly clean", 0, 0, "Alex Kim", 0, "08:00", "", "in_progress", ["done", "done", "done"]),
        ("Weekly clean", 1, 0, "Maria Lopez", 0, "13:00", "", "scheduled", []),
        ("HVAC filter swap Q4", 3, 2, "Dev Patel", 0, "10:30", "", "scheduled", []),
        ("Move-out deep clean", 2, 1, "Maria Lopez", -1, "09:00", "15:30", "awaiting_review",
         ["done", "done", "done", "done", "incomplete", "done"]),
        ("Weekly clean", 0, 0, "Alex Kim", -7, "08:00", "10:15", "completed",
         ["done", "done", "done", "done", "done", "done"]),
        ("HVAC filter swap Q3", 3, 2, "Dev Patel", -90, "10:30", "12:00", "completed",
         ["done", "done", "done", "done", "incomplete"]),
        ("Weekly clean", 1, 0, "Maria Lopez", 7, "13:00", "", "scheduled", []),
    ]
    reasons = {
        "Clean windows inside": "Bedroom window locked; no key available on site.",
        "Record pressure readings": "Gauge missing from van; to be recorded next visit.",
    }
    for idx, (title, pidx, tidx, worker, offset, start, end, status, states) in enumerate(jobs):
        cid, pid = props[pidx]
        completed_at = now if status == "completed" else None
        cur = db.execute(
            "INSERT INTO jobs (account_id, title, customer_id, property_id, template_id, assigned_worker,"
            " appointment_date, start_time, end_time, status, internal_notes, report_notes, reviewed_by, completed_at,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (account_id, "Demo – " + title, cid, pid, template_ids[tidx], worker, (today + timedelta(days=offset)).isoformat(), start, end,
             status, "Demo job. Park in visitor bay.",
             "All agreed areas cleaned. Please report any issues within 48 hours." if states else "",
             "Demo Manager" if status == "completed" else "", completed_at, now, now))
        job_id = cur.lastrowid
        log_event(db, job_id, "Demo Manager", "created", "Demo data (not a real job)")
        if status != "scheduled":
            log_event(db, job_id, worker, "start", "Demo data")
        if status in ("awaiting_review", "completed"):
            log_event(db, job_id, worker, "submit", "In progress → Awaiting review")
        if status == "completed":
            log_event(db, job_id, "Demo Manager", "approve", "Awaiting review → Completed")
        db.execute("INSERT INTO job_items (account_id, job_id, position, label, required)"
                   " SELECT account_id, ?, position, label, required FROM template_items WHERE template_id = ?",
                   (job_id, template_ids[tidx]))
        items = db.execute("SELECT id, label FROM job_items WHERE job_id = ? ORDER BY position", (job_id,)).fetchall()
        for item, state in zip(items, states):
            reason = reasons.get(item["label"], "Not accessible on the day.") if state == "incomplete" else ""
            db.execute("UPDATE job_items SET state = ?, reason = ?, updated_by = ?, updated_at = ? WHERE id = ?",
                       (state, reason, worker, now, item["id"]))
        if status in ("in_progress", "awaiting_review", "completed"):
            area = "Kitchen" if tidx != 2 else "Plant room unit"
            kinds = ["before"] if status == "in_progress" else ["before", "after"]
            for kind in kinds:
                meta = storage.save_image_bytes(_demo_image(area, kind == "after", idx), "demo.jpg")
                written.append(meta["stored_name"])
                db.execute("INSERT INTO photos (account_id, job_id, kind, label, caption, stored_name, width, height,"
                           " byte_size, uploaded_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (account_id, job_id, kind, area, "Demo photo" if kind == "before" else "", meta["stored_name"],
                            meta["width"], meta["height"], meta["byte_size"], worker, now))


def seed_demo(account_id):
    """Insert all demo rows into one account in one transaction; on failure nothing is kept, including photo files."""
    db = get_db()
    written = []
    try:
        with db:
            _insert_demo(db, written, account_id)
    except Exception:
        for name in written:
            storage.delete_stored(name)
        raise


def reset_data(account_id, with_demo=False):
    db = get_db()
    stored = [r[0] for r in db.execute("SELECT stored_name FROM photos WHERE account_id = ?", (account_id,))]
    with db:
        for table in BUSINESS_TABLES:
            db.execute(f"DELETE FROM {table} WHERE account_id = ?", (account_id,))  # table names are constants above
    for name in stored:
        storage.delete_stored(name)
    if with_demo:
        seed_demo(account_id)
