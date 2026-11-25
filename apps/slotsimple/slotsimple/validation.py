"""Input validation helpers. Each returns a cleaned value or raises ValueError
with a message that is safe to show to the user."""
import re
import unicodedata
from decimal import Decimal, InvalidOperation

EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")


def clean_text(value, label, max_len, required=False, multiline=False):
    value = (value or "").strip()
    allowed = {"\n", "\r", "\t"} if multiline else set()
    value = "".join(
        ch for ch in value
        if ch in allowed or unicodedata.category(ch)[0] != "C"
    )
    if multiline:
        value = value.replace("\r\n", "\n").replace("\r", "\n")
    if required and not value:
        raise ValueError(f"{label} is required.")
    if len(value) > max_len:
        raise ValueError(f"{label} must be at most {max_len} characters.")
    return value


def clean_name(value):
    name = " ".join(clean_text(value, "Name", 100, required=True).split())
    if len(name) < 2 or not any(ch.isalpha() for ch in name):
        raise ValueError("Please enter your name (at least 2 characters, including letters).")
    return name


def clean_email(value, required=True):
    value = (value or "").strip()
    if not value and not required:
        return ""
    if not value:
        raise ValueError("Email is required.")
    if len(value) > 254 or not EMAIL_RE.match(value):
        raise ValueError("Please enter a valid email address, like name@example.com.")
    local, _, domain = value.rpartition("@")
    if len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in local:
        raise ValueError("Please enter a valid email address, like name@example.com.")
    return f"{local}@{domain.lower()}"


def clean_int(value, label, minimum, maximum):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a whole number.") from None
    if not minimum <= number <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}.")
    return number


def clean_price(value):
    """Parse '45', '45.5' or '45.50' into integer cents."""
    raw = (value or "").strip().replace(",", "")
    try:
        amount = Decimal(raw or "0")
    except InvalidOperation:
        raise ValueError("Price must be a number like 45 or 45.00.") from None
    if not amount.is_finite() or amount < 0 or amount > 100000:
        raise ValueError("Price must be between 0 and 100000.")
    if amount != amount.quantize(Decimal("0.01")):
        raise ValueError("Price can have at most two decimal places.")
    return int(amount * 100)


REF_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def normalize_reference(value):
    raw = re.sub(r"[^A-Z0-9]", "", (value or "").upper())
    if len(raw) == 12 and raw.startswith("SS") and all(c in REF_ALPHABET for c in raw[2:]):
        return f"SS-{raw[2:7]}-{raw[7:]}"
    return None
