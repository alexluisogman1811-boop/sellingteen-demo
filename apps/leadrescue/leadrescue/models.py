"""Database models.

Every tenant-owned row carries ``business_id``. Application code must reach
tenant data only through the helpers in ``app.tenancy`` which always filter by
the current user's active business.
"""
import secrets
from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db


def utcnow():
    return datetime.now(timezone.utc)


ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"
ROLES = (ROLE_OWNER, ROLE_ADMIN, ROLE_MEMBER)
ROLE_RANK = {ROLE_MEMBER: 1, ROLE_ADMIN: 2, ROLE_OWNER: 3}

LEAD_STATUSES = [
    ("new", "New"),
    ("contacted", "Contacted"),
    ("inspection_scheduled", "Inspection scheduled"),
    ("estimate_sent", "Estimate sent"),
    ("negotiating", "Negotiating"),
    ("won", "Won"),
    ("lost", "Lost"),
]
STATUS_LABELS = dict(LEAD_STATUSES)
CLOSED_STATUSES = ("won", "lost")

LEAD_SOURCES = [
    ("referral", "Referral"),
    ("website", "Website"),
    ("google", "Google / Maps"),
    ("facebook", "Facebook"),
    ("yard_sign", "Yard sign"),
    ("door_knock", "Door knocking"),
    ("lead_service", "Lead service (Angi, HomeAdvisor...)"),
    ("repeat", "Repeat customer"),
    ("other", "Other"),
]
SOURCE_LABELS = dict(LEAD_SOURCES)

ACTIVITY_TYPES = [
    ("note", "Note"),
    ("call", "Phone call"),
    ("text", "Text message"),
    ("email", "Email"),
    ("visit", "Site visit / inspection"),
    ("estimate", "Estimate sent"),
]
CONTACT_ACTIVITY_TYPES = ("call", "text", "email", "visit", "estimate")
SYSTEM_ACTIVITY_TYPES = ("created", "status_change", "imported", "updated")
ACTIVITY_LABELS = dict(ACTIVITY_TYPES) | {
    "created": "Lead created",
    "status_change": "Status changed",
    "imported": "Imported",
    "updated": "Details updated",
}


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(254), unique=True, nullable=False, index=True)
    full_name = db.Column(db.String(120), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_active_flag = db.Column(db.Boolean, nullable=False, default=True)
    failed_logins = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime(timezone=True))
    # Part of every session id; rotating it (on logout) invalidates all existing sessions.
    session_token = db.Column(db.String(64))
    # When the user accepted the Terms and Privacy Notice at signup (consent record).
    terms_accepted_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    memberships = db.relationship("Membership", back_populates="user", cascade="all, delete-orphan")

    def get_id(self):
        return f"{self.id}.{self.session_token}"

    def ensure_session_token(self) -> None:
        if not self.session_token:
            self.rotate_session_token()

    def rotate_session_token(self) -> None:
        self.session_token = secrets.token_urlsafe(32)

    def set_password(self, password: str) -> None:
        # Werkzeug defaults to scrypt with a random per-password salt.
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    @property
    def is_active(self):
        return self.is_active_flag


class Business(db.Model):
    __tablename__ = "businesses"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    is_demo = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    # Subscription: the trial runs from created_at; paid_until is set by `flask activate`.
    paid_until = db.Column(db.Date)
    # IANA time zone for "today" (overdue / due-today). None = the app default (APP_TIMEZONE).
    timezone = db.Column(db.String(64))

    memberships = db.relationship("Membership", back_populates="business", cascade="all, delete-orphan")
    leads = db.relationship("Lead", cascade="all, delete-orphan", lazy="dynamic")
    invitations = db.relationship("Invitation", back_populates="business", cascade="all, delete-orphan", lazy="dynamic")
    audit_events = db.relationship("AuditEvent", cascade="all, delete-orphan", lazy="dynamic")
    password_resets = db.relationship("PasswordReset", cascade="all, delete-orphan", lazy="dynamic")


class Membership(db.Model):
    __tablename__ = "memberships"
    __table_args__ = (db.UniqueConstraint("user_id", "business_id", name="uq_membership_user_business"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    business_id = db.Column(db.Integer, db.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True)
    role = db.Column(db.String(20), nullable=False, default=ROLE_MEMBER)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    user = db.relationship("User", back_populates="memberships")
    business = db.relationship("Business", back_populates="memberships")


class Invitation(db.Model):
    """Single-use join link. Only a SHA-256 hash of the token is stored."""

    __tablename__ = "invitations"

    id = db.Column(db.Integer, primary_key=True)
    business_id = db.Column(db.Integer, db.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = db.Column(db.String(64), unique=True, nullable=False)
    role = db.Column(db.String(20), nullable=False, default=ROLE_MEMBER)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    used_at = db.Column(db.DateTime(timezone=True))
    used_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    business = db.relationship("Business", back_populates="invitations")


class Lead(db.Model):
    __tablename__ = "leads"
    __table_args__ = (
        db.Index("ix_leads_business_status", "business_id", "status"),
        db.Index("ix_leads_business_followup", "business_id", "next_follow_up_date"),
    )

    id = db.Column(db.Integer, primary_key=True)
    business_id = db.Column(db.Integer, db.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True)

    first_name = db.Column(db.String(80), nullable=False)
    last_name = db.Column(db.String(80), nullable=False, default="")
    phone = db.Column(db.String(30), nullable=False, default="")
    email = db.Column(db.String(254), nullable=False, default="")
    address = db.Column(db.String(200), nullable=False, default="")
    city = db.Column(db.String(80), nullable=False, default="")
    state = db.Column(db.String(2), nullable=False, default="")
    zip_code = db.Column(db.String(10), nullable=False, default="")

    source = db.Column(db.String(30), nullable=False, default="other")
    status = db.Column(db.String(30), nullable=False, default="new")
    job_type = db.Column(db.String(120), nullable=False, default="")
    estimated_value = db.Column(db.Numeric(12, 2))
    last_contact_date = db.Column(db.Date)
    next_follow_up_date = db.Column(db.Date)
    notes = db.Column(db.Text, nullable=False, default="")

    assigned_to_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    is_demo = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    assigned_to = db.relationship("User", foreign_keys=[assigned_to_id])
    activities = db.relationship(
        "Activity",
        back_populates="lead",
        cascade="all, delete-orphan",
        order_by="Activity.occurred_at.desc()",
    )
    drafts = db.relationship(
        "MessageDraft",
        back_populates="lead",
        cascade="all, delete-orphan",
        order_by="MessageDraft.created_at.desc()",
    )

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def status_label(self):
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def source_label(self):
        return SOURCE_LABELS.get(self.source, self.source)

    @property
    def is_open(self):
        return self.status not in CLOSED_STATUSES


DRAFT_CHANNELS = [("text", "Text message"), ("email", "Email")]
DRAFT_STATUS_DRAFT = "draft"
DRAFT_STATUS_APPROVED = "approved"
DRAFT_STATUS_SENT = "sent"
DRAFT_STATUS_DISCARDED = "discarded"


class MessageDraft(db.Model):
    """A customer message prepared for the owner to review.

    Drafts are never sent by LeadRescue. An owner must approve a draft before it
    can be copied or marked as sent (by hand), and any edit after approval sends
    it back to "draft".
    """

    __tablename__ = "message_drafts"

    id = db.Column(db.Integer, primary_key=True)
    business_id = db.Column(db.Integer, db.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True)
    lead_id = db.Column(db.Integer, db.ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True)
    channel = db.Column(db.String(10), nullable=False, default="text")
    subject = db.Column(db.String(150), nullable=False, default="")
    body = db.Column(db.Text, nullable=False)
    provider = db.Column(db.String(40), nullable=False, default="template")
    warnings = db.Column(db.Text, nullable=False, default="")
    status = db.Column(db.String(20), nullable=False, default=DRAFT_STATUS_DRAFT)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    approved_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    approved_at = db.Column(db.DateTime(timezone=True))
    sent_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    lead = db.relationship("Lead", back_populates="drafts")
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    approved_by = db.relationship("User", foreign_keys=[approved_by_id])

    @property
    def warning_list(self):
        return [w for w in self.warnings.split("\n") if w]

    @property
    def channel_label(self):
        return dict(DRAFT_CHANNELS).get(self.channel, self.channel)


class Activity(db.Model):
    __tablename__ = "activities"

    id = db.Column(db.Integer, primary_key=True)
    business_id = db.Column(db.Integer, db.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True)
    lead_id = db.Column(db.Integer, db.ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    type = db.Column(db.String(20), nullable=False)
    body = db.Column(db.Text, nullable=False, default="")
    occurred_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    lead = db.relationship("Lead", back_populates="activities")
    user = db.relationship("User")

    @property
    def type_label(self):
        return ACTIVITY_LABELS.get(self.type, self.type)


AUDIT_ACTIONS = {
    "csv_export": "Exported leads (CSV)",
    "csv_import": "Imported leads (CSV)",
    "workspace_export": "Downloaded full workspace export",
    "lead_delete": "Deleted a lead",
    "activity_delete": "Deleted a timeline entry",
    "demo_remove": "Removed demo leads",
    "member_remove": "Removed a team member",
    "password_reset_link": "Created a password reset link",
}


class AuditEvent(db.Model):
    """Who exported, imported or deleted what, and when. Visible to the workspace owner.

    Details hold counts and ids only, never customer names or contact details, so deleting
    a lead really removes its personal data."""

    __tablename__ = "audit_events"

    id = db.Column(db.Integer, primary_key=True)
    business_id = db.Column(db.Integer, db.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    user_name = db.Column(db.String(120), nullable=False, default="")
    action = db.Column(db.String(40), nullable=False)
    detail = db.Column(db.String(300), nullable=False, default="")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)

    @property
    def action_label(self):
        return AUDIT_ACTIONS.get(self.action, self.action)


class PasswordReset(db.Model):
    """One-time, time-limited password reset link created by a workspace owner (or by
    support with `flask reset-link`). Only a SHA-256 hash of the token is stored."""

    __tablename__ = "password_resets"

    id = db.Column(db.Integer, primary_key=True)
    token_hash = db.Column(db.String(64), unique=True, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # The workspace whose owner created it (None when created by support from the command line).
    business_id = db.Column(db.Integer, db.ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    used_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    user = db.relationship("User", foreign_keys=[user_id])
