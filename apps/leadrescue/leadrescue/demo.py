"""Sample data. Every record created here is flagged is_demo=True and its
names carry a "(DEMO)" marker so it can never be mistaken for a real customer.
Phone numbers use the reserved 555-01xx range and emails use example.com."""
from datetime import date, datetime, time, timedelta, timezone
from .dates import local_today
from decimal import Decimal

from .extensions import db
from .models import Activity, Business, Lead, User

DEMO_LEADS = [
    # first, last, city, state, source, status, job, value, last_contact_offset, next_follow_offset
    ("Alice", "Sample (DEMO)", "Springfield", "OH", "referral", "estimate_sent", "Full reroof - architectural shingles", 14200, -5, -1),
    ("Bob", "Example (DEMO)", "Springfield", "OH", "google", "new", "Leak repair", 850, None, 0),
    ("Carmen", "Placeholder (DEMO)", "Fairview", "OH", "website", "inspection_scheduled", "Storm damage inspection", 9800, -2, 2),
    ("Dev", "Testcase (DEMO)", "Riverton", "OH", "yard_sign", "contacted", "Gutter + fascia replacement", 3200, -9, -3),
    ("Erin", "Mockup (DEMO)", "Fairview", "OH", "lead_service", "estimate_sent", "Full reroof - metal", 27500, -12, -6),
    ("Frank", "Specimen (DEMO)", "Lakeside", "OH", "door_knock", "negotiating", "Reroof + skylight", 18900, -1, 1),
    ("Grace", "Trial (DEMO)", "Riverton", "OH", "repeat", "won", "Chimney flashing", 1450, -20, None),
    ("Hank", "Dummy (DEMO)", "Lakeside", "OH", "facebook", "lost", "Full reroof", 12600, -30, None),
    ("Ivy", "Fictional (DEMO)", "Springfield", "OH", "referral", "estimate_sent", "Flat roof coating", 6400, -3, 0),
    ("Jon", "Madeup (DEMO)", "Fairview", "OH", "google", "new", "Missing shingles repair", 600, None, None),
]


def add_demo_leads(business: Business, user: User | None) -> int:
    today = local_today()
    count = 0
    for i, (first, last, city, state, source, status, job, value, last_off, next_off) in enumerate(DEMO_LEADS):
        lead = Lead(
            business_id=business.id,
            first_name=first,
            last_name=last,
            phone=f"(555) 555-01{i:02d}",
            email=f"{first.lower()}.demo@example.com",
            address=f"{100 + i * 11} Demo Street",
            city=city,
            state=state,
            zip_code="00000",
            source=source,
            status=status,
            job_type=job,
            estimated_value=Decimal(value),
            last_contact_date=today + timedelta(days=last_off) if last_off is not None else None,
            next_follow_up_date=today + timedelta(days=next_off) if next_off is not None else None,
            notes="DEMO DATA - fictional customer for trying out LeadRescue. Safe to delete.",
            assigned_to_id=user.id if user else None,
            created_by_id=user.id if user else None,
            is_demo=True,
        )
        db.session.add(lead)
        db.session.flush()
        db.session.add(Activity(business_id=business.id, lead_id=lead.id, user_id=user.id if user else None,
                                type="created", body="Demo lead created."))
        if last_off is not None:
            db.session.add(Activity(
                business_id=business.id, lead_id=lead.id, user_id=user.id if user else None, type="call",
                body="DEMO: Called homeowner, discussed options.",
                occurred_at=datetime.combine(today + timedelta(days=last_off), time(10, 0), tzinfo=timezone.utc),
            ))
        count += 1
    db.session.commit()
    return count


def remove_demo_leads(business: Business) -> int:
    leads = db.session.execute(
        db.select(Lead).where(Lead.business_id == business.id, Lead.is_demo.is_(True))
    ).scalars().all()
    for lead in leads:
        db.session.delete(lead)
    db.session.commit()
    return len(leads)
