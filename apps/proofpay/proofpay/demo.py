"""Demo mode: a clearly labelled sample plumbing business for sales demos.

All names are fictional, emails use the reserved example.com domain, and phone numbers
use the 555-01xx fictional range. Photos are generated placeholder illustrations.
"""
import secrets
from datetime import date, timedelta
from io import BytesIO

import click
from flask import Blueprint, abort, current_app, flash, redirect, session, url_for
from flask_login import login_user
from PIL import Image, ImageDraw
from werkzeug.datastructures import FileStorage

from .models import (Business, Customer, Invoice, InvoiceStatus, Job, JobPhoto, JobStatus,
                     User, db, utcnow)
from .storage import delete_photo_files, save_photo

bp = Blueprint("demo", __name__)

DEMO_EMAIL = "demo@proofpay.example.com"


@bp.route("/demo", methods=["POST"])
def start_demo():
    if not current_app.config.get("DEMO_MODE"):
        abort(404)
    user = db.session.scalar(db.select(User).filter_by(email=DEMO_EMAIL)) or seed_demo()
    session.clear()
    login_user(user)
    flash("You're viewing the DEMO account. All data here is sample data.", "info")
    return redirect(url_for("jobs.dashboard"))


def register_cli(app) -> None:
    @app.cli.command("demo-seed")
    def demo_seed_command():
        """Create (or reset) the demo account and sample data."""
        seed_demo()
        click.echo("Demo data ready. Start the app and click 'Try the demo'.")


def _placeholder(scene: str, after: bool) -> FileStorage:
    """Draw a labelled illustration so demos never use real customer photos.

    scene: "sink" (pipes under a sink), "heater" (water heater) or "outlet" (wall outlet).
    """
    wall = (196, 215, 230) if after else (150, 128, 108)
    img = Image.new("RGB", (1200, 900), wall)
    d = ImageDraw.Draw(img)
    if scene == "sink":
        pipe = (190, 192, 198) if after else (140, 70, 30)
        d.rectangle([150, 100, 1050, 240], fill=(235, 235, 235), outline=(90, 90, 90), width=6)
        d.rectangle([560, 240, 640, 500], fill=pipe)
        d.arc([440, 420, 760, 740], 0, 180, fill=pipe, width=80)
        d.rectangle([180, 540, 480, 620], fill=pipe)
        if not after:  # drips and a puddle
            for x, y in ((600, 520), (620, 580), (590, 640)):
                d.ellipse([x, y, x + 18, y + 30], fill=(70, 120, 200))
            d.ellipse([430, 720, 820, 770], fill=(80, 120, 180))
    elif scene == "heater":
        tank = (225, 228, 232) if after else (120, 100, 85)
        d.rounded_rectangle([420, 90, 780, 720], radius=60, fill=tank, outline=(60, 60, 60), width=6)
        d.rectangle([560, 30, 600, 100], fill=(170, 170, 175))
        d.rectangle([700, 30, 740, 100], fill=(170, 170, 175))
        if not after:  # rust streaks
            for x in (470, 540, 650, 720):
                d.line([x, 300, x - 10, 700], fill=(150, 70, 25), width=14)
    else:  # outlet
        plate = (245, 245, 240) if after else (220, 210, 190)
        d.rounded_rectangle([430, 160, 770, 680], radius=24, fill=plate, outline=(90, 90, 90), width=5)
        for top in (230, 470):
            d.rounded_rectangle([500, top, 700, top + 150], radius=30, fill=(232, 230, 222),
                                outline=(120, 120, 120), width=4)
            d.rectangle([550, top + 45, 565, top + 95], fill=(40, 40, 40))
            d.rectangle([635, top + 45, 650, top + 95], fill=(40, 40, 40))
        if not after:  # scorch mark
            d.ellipse([600, 210, 760, 330], fill=(70, 55, 45))
        else:  # GFCI test/reset buttons
            d.rectangle([560, 405, 640, 440], fill=(200, 40, 40))
    d.rectangle([0, 790, 1200, 900], fill=(20, 24, 33))
    d.text((40, 815), f"DEMO PHOTO - {'AFTER' if after else 'BEFORE'}", fill=(255, 255, 255),
           font_size=56)
    buf = BytesIO()
    img.save(buf, "JPEG", quality=85)
    buf.seek(0)
    return FileStorage(stream=buf, filename=f"{scene}-{'after' if after else 'before'}.jpg",
                       content_type="image/jpeg")


def _remove_existing_demo() -> None:
    user = db.session.scalar(db.select(User).filter_by(email=DEMO_EMAIL))
    if not user:
        return
    biz = user.business
    for job in biz.jobs:
        delete_photo_files(biz.ref, *[n for p in job.photos for n in (p.stored_name, p.thumb_name)])
    db.session.delete(user)
    db.session.commit()


def seed_demo() -> User:
    _remove_existing_demo()
    user = User(email=DEMO_EMAIL)
    user.set_password(secrets.token_urlsafe(32))  # unusable; demo login is via /demo only
    biz = Business(
        name="DEMO - Rivera Plumbing & Electric", trade="Plumbing & Electrical",
        phone="(555) 010-0142", email="office@rivera-demo.example.com", is_demo=True,
        payment_instructions="DEMO ONLY. Example: pay by bank transfer to the account on your "
                             "invoice, or by check made out to Rivera Plumbing & Electric.",
    )
    user.business = biz
    db.session.add(user)
    db.session.flush()

    # Fictional people; reserved example.com emails; 555-01xx fictional phone numbers.
    c1 = Customer(business_id=biz.id, name="Jordan Lee", email="jordan.lee@example.com",
                  phone="(555) 010-0175")
    c2 = Customer(business_id=biz.id, name="Priya Shah", email="priya.shah@example.com",
                  phone="(555) 010-0188")
    c3 = Customer(business_id=biz.id, name="Sam Okafor", email="sam.okafor@example.com",
                  phone="(555) 010-0193")
    db.session.add_all([c1, c2, c3])
    today = date.today()

    def job(customer, title, desc, addr, cents, status, notes, summary=None, due=None,
            paid=False, days_ago=0, scene=None):
        j = Job(business_id=biz.id, customer=customer, title=title, description=desc,
                address=addr, price_cents=cents, status=status, work_notes=notes,
                summary=summary, due_date=due,
                completed_at=(utcnow() - timedelta(days=days_ago))
                if status == JobStatus.COMPLETED else None)
        j.created_at = utcnow() - timedelta(days=days_ago + 1)
        j.invoice = Invoice(business_id=biz.id, amount_cents=cents)
        if paid:
            j.invoice.status = InvoiceStatus.PAID
            j.invoice.paid_via = "manual"
            j.invoice.paid_at = utcnow() - timedelta(days=max(days_ago - 3, 0))
        db.session.add(j)
        db.session.flush()
        if scene:
            for kind, after in (("before", False), ("after", True)):
                meta = save_photo(_placeholder(scene, after), biz.ref)
                db.session.add(JobPhoto(business_id=biz.id, job_id=j.id, kind=kind, **meta))
        return j

    # 1. Paid: shows the finished state.
    job(c2, "Water heater replacement",
        "Existing 40-gallon gas water heater leaking from the base. Replace with a new unit.",
        "48 Sample Avenue, Springfield", 145000, JobStatus.COMPLETED,
        notes="Drained and removed old water heater\nInstalled new 40-gallon gas water heater\n"
              "Connected gas, water and vent lines\nTested for gas leaks\n"
              "Checked hot water at all taps",
        summary="Removed the leaking 40-gallon gas water heater and installed a new 40-gallon "
                "unit. Connected the gas, water and vent lines, tested for gas leaks, and "
                "confirmed hot water at all taps.",
        due=today - timedelta(days=4), paid=True, days_ago=12, scene="heater")
    # 2. Completed but unpaid: the "payment due" case.
    job(c1, "Kitchen sink leak repair",
        "Customer reported water under the kitchen sink.",
        "12 Example Street, Springfield", 28500, JobStatus.COMPLETED,
        notes="Replaced corroded P-trap\nReplaced both supply line washers\n"
              "Ran water for 10 minutes and checked for leaks",
        summary="Replaced the corroded P-trap and both supply line washers under the kitchen "
                "sink, then ran the water for 10 minutes and checked that there were no leaks.",
        due=today + timedelta(days=14), days_ago=2, scene="sink")
    # 3. Work just finished, not yet sent: used to demo the summary + completion live.
    job(c3, "Bathroom exhaust fan and GFCI outlet",
        "Bathroom fan not working; outlet near the sink is scorched.",
        "7 Placeholder Road, Springfield", 34000, JobStatus.IN_PROGRESS,
        notes="Removed old exhaust fan\nInstalled new exhaust fan\n"
              "Replaced scorched outlet with GFCI outlet\nTested fan and GFCI outlet",
        due=today + timedelta(days=14), scene="outlet")
    db.session.commit()
    return user
