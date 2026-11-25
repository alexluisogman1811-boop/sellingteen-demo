"""Small helpers: time, money and quantity formatting, hashing."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def now_iso():
    return utcnow().isoformat()


def iso_in(**delta):
    return (utcnow() + timedelta(**delta)).isoformat()


def parse_iso(value):
    return datetime.fromisoformat(value)


def sha256_hex(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def cents_to_str(cents):
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"


def x100_to_str(value):
    """Format a hundredths integer (quantity or percent) without trailing zeros."""
    d = (Decimal(int(value)) / 100).normalize()
    text = format(d, "f")
    return text


def money_to_cents(d):
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fmt_datetime(value):
    if not value:
        return ""
    dt = parse_iso(value)
    return dt.strftime("%b %d, %Y %H:%M UTC")
