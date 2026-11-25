"""Database models.

Every business-owned row carries business_id so queries can be scoped by tenant.
URLs never use the integer primary keys: owners see `ref`, customers see `share_token`.
"""
import secrets
from datetime import datetime, timezone
from decimal import Decimal

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_ref() -> str:
    return secrets.token_urlsafe(9)  # 12 chars, used in owner URLs


def new_share_token() -> str:
    return secrets.token_urlsafe(24)  # 32 chars, used in customer URLs


class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    # Bumped on logout so Flask-Login's alternative id invalidates old sessions.
    session_version = db.Column(db.Integer, nullable=False, default=1)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    business = db.relationship("Business", back_populates="owner", uselist=False,
                               cascade="all, delete-orphan")

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    def get_id(self) -> str:
        return f"{self.id}:{self.session_version}"


class Business(db.Model):
    __tablename__ = "businesses"
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"),
                         nullable=False, unique=True, index=True)
    ref = db.Column(db.String(32), unique=True, nullable=False, default=new_ref)
    name = db.Column(db.String(120), nullable=False)
    trade = db.Column(db.String(60), nullable=True)
    phone = db.Column(db.String(40), nullable=True)
    email = db.Column(db.String(255), nullable=True)
    # Shown on the customer payment page while no online provider is connected.
    payment_instructions = db.Column(db.Text, nullable=True)
    is_demo = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    # Pro is paid up to and including this date (set by `flask activate`). NULL = never paid.
    paid_until = db.Column(db.Date, nullable=True)

    owner = db.relationship("User", back_populates="business")
    customers = db.relationship("Customer", back_populates="business",
                                cascade="all, delete-orphan")
    jobs = db.relationship("Job", back_populates="business", cascade="all, delete-orphan")


class Customer(db.Model):
    __tablename__ = "customers"
    id = db.Column(db.Integer, primary_key=True)
    business_id = db.Column(db.Integer, db.ForeignKey("businesses.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    ref = db.Column(db.String(32), unique=True, nullable=False, default=new_ref)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), nullable=True)
    phone = db.Column(db.String(40), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    business = db.relationship("Business", back_populates="customers")
    jobs = db.relationship("Job", back_populates="customer")

    __table_args__ = (db.Index("ix_customers_business_email", "business_id", "email"),)


class JobStatus:
    DRAFT = "draft"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ALL = (DRAFT, IN_PROGRESS, COMPLETED)


class Job(db.Model):
    __tablename__ = "jobs"
    id = db.Column(db.Integer, primary_key=True)
    business_id = db.Column(db.Integer, db.ForeignKey("businesses.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id", ondelete="RESTRICT"),
                            nullable=False, index=True)
    ref = db.Column(db.String(32), unique=True, nullable=False, default=new_ref)
    share_token = db.Column(db.String(64), unique=True, nullable=False, default=new_share_token)

    title = db.Column(db.String(160), nullable=False)
    description = db.Column(db.Text, nullable=True)
    address = db.Column(db.String(255), nullable=True)
    price_cents = db.Column(db.Integer, nullable=False, default=0)
    due_date = db.Column(db.Date, nullable=True)
    status = db.Column(db.String(20), nullable=False, default=JobStatus.DRAFT, index=True)
    work_notes = db.Column(db.Text, nullable=True)
    # AI output is only a suggestion; `summary` is what the owner approved for the customer.
    summary = db.Column(db.Text, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    business = db.relationship("Business", back_populates="jobs")
    customer = db.relationship("Customer", back_populates="jobs")
    photos = db.relationship("JobPhoto", back_populates="job", cascade="all, delete-orphan",
                             order_by="JobPhoto.created_at")
    invoice = db.relationship("Invoice", back_populates="job", uselist=False,
                              cascade="all, delete-orphan")

    @property
    def price(self) -> Decimal:
        return Decimal(self.price_cents) / 100

    @property
    def is_completed(self) -> bool:
        return self.status == JobStatus.COMPLETED

    @property
    def is_paid(self) -> bool:
        return bool(self.invoice and self.invoice.is_paid)

    @property
    def display_status(self) -> str:
        """The single status label shown everywhere (dashboard, detail, customer page)."""
        if self.is_paid:
            return "Paid"
        if self.is_completed:
            return "Payment Due" if self.price_cents > 0 else "Completed"
        if self.status == JobStatus.IN_PROGRESS:
            return "In Progress"
        return "Draft"

    @property
    def work_items(self) -> list[str]:
        """Work notes split into bullet lines for display."""
        lines = [ln.strip(" -*•\t") for ln in (self.work_notes or "").splitlines()]
        return [ln for ln in lines if ln]

    def photos_of(self, kind: str) -> list["JobPhoto"]:
        return [p for p in self.photos if p.kind == kind]


class PhotoKind:
    BEFORE = "before"
    AFTER = "after"
    GENERAL = "general"
    ALL = (BEFORE, AFTER, GENERAL)


class JobPhoto(db.Model):
    __tablename__ = "job_photos"
    id = db.Column(db.Integer, primary_key=True)
    business_id = db.Column(db.Integer, db.ForeignKey("businesses.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    job_id = db.Column(db.Integer, db.ForeignKey("jobs.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    ref = db.Column(db.String(32), unique=True, nullable=False, default=new_ref)
    kind = db.Column(db.String(10), nullable=False, default=PhotoKind.GENERAL)
    # Server-generated names only; the user's original filename is never used on disk.
    stored_name = db.Column(db.String(80), nullable=False)
    thumb_name = db.Column(db.String(80), nullable=False)
    content_type = db.Column(db.String(40), nullable=False)
    size_bytes = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    job = db.relationship("Job", back_populates="photos")


class InvoiceStatus:
    UNPAID = "unpaid"
    PAID = "paid"


class Invoice(db.Model):
    """One invoice per job. Payment state lives here, never on the job itself."""
    __tablename__ = "invoices"
    id = db.Column(db.Integer, primary_key=True)
    business_id = db.Column(db.Integer, db.ForeignKey("businesses.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    job_id = db.Column(db.Integer, db.ForeignKey("jobs.id", ondelete="CASCADE"),
                       nullable=False, unique=True, index=True)
    amount_cents = db.Column(db.Integer, nullable=False, default=0)
    currency = db.Column(db.String(3), nullable=False, default="USD")
    status = db.Column(db.String(10), nullable=False, default=InvoiceStatus.UNPAID, index=True)
    # How it was paid: "manual" = owner confirmed it; future providers record their own name.
    paid_via = db.Column(db.String(30), nullable=True)
    provider_reference = db.Column(db.String(120), nullable=True)
    paid_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    job = db.relationship("Job", back_populates="invoice")

    @property
    def is_paid(self) -> bool:
        return self.status == InvoiceStatus.PAID
