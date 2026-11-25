"""Demo data: a realistic sample job with generated placeholder photos."""
import io
import secrets
from datetime import date

import click
from flask import current_app
from PIL import Image, ImageDraw
from werkzeug.security import generate_password_hash

from . import images
from .db import get_db, now_iso

DEMO_JOB = {
    "customer_name": "Sarah Johnson",
    "customer_phone": "(555) 014-2290",
    "customer_email": "sarah.johnson@example.com",
    "title": "Kitchen Sink Repair",
    "address": "48 Willow Lane, Springfield",
    "service_type": "Plumbing",
    "technician": "Mike Torres",
    "notes": ("Customer reported a leaking connection under the kitchen sink. Replaced the damaged connector, "
              "tightened fittings, tested the water flow, and confirmed there was no visible leak."),
    "materials": "",
    "status": "completed",
}

_PALETTE = {"before": ((120, 53, 15), (180, 83, 9)), "during": ((30, 64, 175), (59, 130, 246)),
            "after": ((6, 95, 70), (16, 185, 129))}


def placeholder_image(stage, caption):
    """Draw a simple labelled placeholder photo (JPEG bytes)."""
    w, h = 1200, 900
    top, bottom = _PALETTE[stage]
    img = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / h
        draw.line([(0, y), (w, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    draw.rectangle([60, 60, w - 60, h - 60], outline=(255, 255, 255), width=6)
    draw.text((100, 100), f"{stage.upper()} PHOTO", fill=(255, 255, 255), font_size=96)
    draw.text((100, 240), caption, fill=(255, 255, 255), font_size=54)
    draw.text((100, h - 160), "Demo placeholder image", fill=(230, 230, 230), font_size=40)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    return out.getvalue(), w, h


def create_demo_job(business_id):
    db = get_db()
    ts = now_iso()
    d = DEMO_JOB
    cur = db.execute(
        "INSERT INTO jobs (business_id, customer_name, customer_phone, customer_email, title, address, "
        "service_type, job_date, technician, notes, materials, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (business_id, d["customer_name"], d["customer_phone"], d["customer_email"], d["title"], d["address"],
         d["service_type"], date.today().isoformat(), d["technician"], d["notes"], d["materials"], d["status"],
         ts, ts),
    )
    job_id = cur.lastrowid
    shots = [("before", "Leaking connector under sink"), ("during", "Connector removed"),
             ("after", "New connector, no leaks")]
    for pos, (stage, caption) in enumerate(shots, start=1):
        data, w, h = placeholder_image(stage, caption)
        key = images.new_key()
        images.save_bytes(current_app.config["UPLOAD_FOLDER"], "photos", key, "jpg", data)
        db.execute(
            "INSERT INTO photos (job_id, file_key, ext, stage, position, width, height, size_bytes, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (job_id, key, "jpg", stage, pos, w, h, len(data), ts),
        )
    db.commit()
    return job_id


def register_cli(app):
    @app.cli.command("seed-demo")
    @click.option("--email", default="demo@jobproof.test", show_default=True)
    @click.option("--password", default=None, help="Defaults to a random password that is printed once.")
    def seed_demo(email, password):
        """Create a demo account with a business profile and a sample job."""
        db = get_db()
        password = password or secrets.token_urlsafe(9)
        if db.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            raise click.ClickException(f"{email} already exists. Log in and use “Load demo job” instead.")
        ts = now_iso()
        uid = db.execute("INSERT INTO users (email, password_hash, created_at) VALUES (?,?,?)",
                         (email, generate_password_hash(password), ts)).lastrowid
        bid = db.execute(
            "INSERT INTO businesses (user_id, name, contact_name, phone, email, address, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (uid, "Torres Plumbing & Repair", "Mike Torres", "(555) 010-7788", email,
             "120 Main Street, Springfield", ts),
        ).lastrowid
        db.commit()
        create_demo_job(bid)
        click.echo(f"Demo account created.\n  Email:    {email}\n  Password: {password}")
