"""Builds the *only* facts a message draft may use.

The draft generator never reads the lead object directly. It reads a
DraftContext containing verified, normalised facts. Anything that is missing,
malformed or suspicious is left out (and reported as a warning) instead of
being guessed. Free-text fields that customers or staff typed (notes, job type)
are never copied into a message. The job type is only *classified*.

Deliberately excluded: estimated value (an internal number, not a quote),
next follow-up date (an internal reminder, not an appointment), notes,
address, and anything about warranties, prices or policies.
"""
import re
from dataclasses import dataclass, field
from datetime import date

from ..dates import local_today

from .services import ServiceMatch, classify_service

# A plausible first name: letters plus internal space, hyphen, apostrophe or period.
_NAME_RE = re.compile(r"^[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'’.\- ]{0,39}$")
_SUSPICIOUS_NAME_WORDS = {
    "ignore", "instruction", "instructions", "system", "prompt", "assistant", "previous",
    "discount", "free", "guarantee", "warranty", "price", "offer", "admin", "test", "demo",
}

STALENESS_UNKNOWN = "unknown"
STALENESS_RECENT = "recent"      # 0-3 days
STALENESS_WEEK = "week"          # 4-13 days
STALENESS_STALE = "stale"        # 14+ days


@dataclass(frozen=True)
class DraftContext:
    channel: str                      # "text" | "email"
    status: str                       # lead status key
    greeting_name: str | None         # safe first name, or None -> "Hi there"
    business_name: str | None
    sender_name: str | None
    service: ServiceMatch
    days_since_contact: int | None
    staleness: str
    has_phone: bool
    has_email: bool
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def facts(self) -> list[tuple[str, str]]:
        """Human-readable list of facts the draft was allowed to use."""
        items = [
            ("Customer first name", self.greeting_name or "(not used — missing or unclear)"),
            ("Lead status", self.status.replace("_", " ")),
            ("Service", self.service.label if self.service.supported else "(general wording — service not recognised)"),
            ("Last contact", f"{self.days_since_contact} day(s) ago" if self.days_since_contact is not None else "(unknown)"),
            ("Your business", self.business_name or "(missing)"),
            ("Signed by", self.sender_name or "(not signed)"),
        ]
        return items


def safe_first_name(raw: str | None) -> tuple[str | None, str | None]:
    """Return (name, warning). Never raises; never returns text that isn't a plain name."""
    value = (raw or "").strip()
    if not value:
        return None, "No customer first name on file, so the draft says “Hi there”."
    if not _NAME_RE.match(value):
        return None, "The customer name contains unusual characters, so it was left out. Check the lead record."
    words = value.replace("-", " ").split()
    if len(words) > 2 or any(w.lower().strip(".'’") in _SUSPICIOUS_NAME_WORDS for w in words):
        return None, "The customer name doesn't look like a name, so it was left out. Check the lead record."
    return value, None


def _clean_label(raw: str | None, max_len: int) -> str | None:
    value = " ".join((raw or "").split())
    if not value or len(value) > max_len or any(ch in value for ch in "<>{}[]$\\"):
        return None
    return value


def build_context(lead, channel: str, business_name: str | None, sender_name: str | None,
                  today: date | None = None) -> DraftContext:
    today = today or local_today()
    warnings: list[str] = []

    name, warn = safe_first_name(lead.first_name)
    if warn:
        warnings.append(warn)

    service = classify_service(lead.job_type)
    if service.warning:
        warnings.append(service.warning)

    days = None
    staleness = STALENESS_UNKNOWN
    if lead.last_contact_date is None:
        warnings.append("No last-contact date, so the draft doesn't refer to a previous conversation's timing.")
    elif lead.last_contact_date > today:
        warnings.append("Last-contact date is in the future, so it was ignored. Fix the date on the lead.")
    else:
        days = (today - lead.last_contact_date).days
        staleness = STALENESS_RECENT if days <= 3 else STALENESS_WEEK if days <= 13 else STALENESS_STALE

    has_phone = bool((lead.phone or "").strip())
    has_email = bool((lead.email or "").strip())
    if channel == "text" and not has_phone:
        warnings.append("No phone number on file. Add one before you can text this customer.")
    if channel == "email" and not has_email:
        warnings.append("No email address on file. Add one before you can email this customer.")

    business = _clean_label(business_name, 80)
    if business is None:
        warnings.append("Business name is missing or unusual, so the draft doesn't name your company.")
    sender = _clean_label(sender_name, 60)

    return DraftContext(
        channel=channel,
        status=lead.status,
        greeting_name=name,
        business_name=business,
        sender_name=sender,
        service=service,
        days_since_contact=days,
        staleness=staleness,
        has_phone=has_phone,
        has_email=has_email,
        warnings=tuple(warnings),
    )
