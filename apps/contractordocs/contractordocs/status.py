"""Document status rules. All status is computed from stored fields at read time."""
from datetime import date, timedelta

DOC_TYPES = [
    "Certificate of Insurance",
    "Workers' Compensation",
    "W-9",
    "Business License",
    "Other",
]
DEFAULT_REQUIRED_TYPES = ["Certificate of Insurance", "Workers' Compensation", "W-9"]

EXPIRING_SOON_DAYS = 30

VALID = "valid"
EXPIRING = "expiring"
EXPIRED = "expired"
MISSING = "missing"
REVIEW = "review"

LABELS = {
    VALID: "Valid",
    EXPIRING: "Expiring soon",
    EXPIRED: "Expired",
    MISSING: "Missing",
    REVIEW: "Needs review",
}


def parse_iso(value):
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def document_status(doc, today=None):
    """Status of a single document row (dict-like)."""
    today = today or date.today()
    if doc["needs_review"]:
        return REVIEW
    if doc["no_expiration"]:
        return VALID
    exp = parse_iso(doc["expiration_date"])
    if exp is None:
        return REVIEW
    if exp < today:
        return EXPIRED
    if exp <= today + timedelta(days=EXPIRING_SOON_DAYS):
        return EXPIRING
    return VALID


def days_left(doc, today=None):
    today = today or date.today()
    exp = parse_iso(doc["expiration_date"])
    if exp is None or doc["no_expiration"]:
        return None
    return (exp - today).days


def _best(docs):
    """The reviewed document that covers the requirement furthest into the future."""
    def key(d):
        if d["no_expiration"]:
            return (1, date.max, d["uploaded_at"])
        return (0, parse_iso(d["expiration_date"]) or date.min, d["uploaded_at"])
    return max(docs, key=key)


def requirement_slots(subcontractors, documents, required_types, today=None):
    """One slot per (subcontractor, document type) that is required or has documents.

    A slot's status comes from its best reviewed document. A slot with only
    unreviewed documents is "review"; a required slot with no documents is "missing".
    Older documents superseded by a newer one do not raise alerts.
    """
    today = today or date.today()
    by_sub = {}
    for d in documents:
        by_sub.setdefault(d["subcontractor_id"], {}).setdefault(d["doc_type"] or "Unknown", []).append(d)

    slots = []
    for sub in subcontractors:
        types = by_sub.get(sub["id"], {})
        names = list(required_types) + [t for t in types if t not in required_types]
        for t in names:
            docs = types.get(t, [])
            reviewed = [d for d in docs if not d["needs_review"]]
            if reviewed:
                best = _best(reviewed)
                status = document_status(best, today)
            elif docs:
                best = max(docs, key=lambda d: d["uploaded_at"])
                status = REVIEW
            else:
                best = None
                status = MISSING
            slots.append({
                "subcontractor": sub,
                "doc_type": t,
                "required": t in required_types,
                "document": best,
                "status": status,
                "days_left": days_left(best, today) if best is not None else None,
            })
    return slots
