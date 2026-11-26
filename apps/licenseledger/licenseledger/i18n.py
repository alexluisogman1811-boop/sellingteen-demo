"""Thai versions of the built-in reminder messages.

The hosting layer translates page text when the owner picks Thai (cookie
``st_lang`` == "th"), but reminder messages are produced by this app, so the
Thai wording of the built-in templates lives here.

Rules:
- Stored template rows are never rewritten. Thai is chosen at render time only.
- A stored row is treated as a built-in only while its subject AND body still
  match one of ``db.DEFAULT_TEMPLATES`` exactly (line endings normalised).
  Any edit by the owner makes it the owner's own text, shown as written.
- Every Thai text uses exactly the same placeholders as its English original.
"""
from flask import has_request_context, request

from .db import DEFAULT_TEMPLATES

LANG_COOKIE = "st_lang"

# English (subject, body) of each built-in -> Thai (subject, body).
# Polite, gender-neutral wording (no ครับ/ค่ะ). "คุณ{customer_name}" is
# expanded by ``thai_honorific`` (space before non-Thai names).
_THAI_BY_NAME = {
    "Renewal reminder (upcoming)": (
        "ไลเซนส์ {product_name} ของคุณจะหมดอายุในวันที่ {expiration_date}",
        "เรียน คุณ{customer_name}\n\n"
        "ขอแจ้งเตือนว่าไลเซนส์ {product_name} ของคุณ "
        "(คำสั่งซื้อ {order_ref}, คีย์ลงท้ายด้วย {license_hint}) จะหมดอายุในวันที่ {expiration_date} "
        "หรืออีก {days_left} วันนับจากวันนี้\n\n"
        "หากต้องการต่ออายุ สามารถตอบกลับอีเมลนี้ได้เลย แล้วเราจะส่งรายละเอียดให้\n\n"
        "ขอบคุณ\n{business_name}",
    ),
    "Expired license follow-up": (
        "ไลเซนส์ {product_name} ของคุณหมดอายุแล้ว",
        "เรียน คุณ{customer_name}\n\n"
        "ไลเซนส์ {product_name} ของคุณ (คำสั่งซื้อ {order_ref}, คีย์ลงท้ายด้วย {license_hint}) "
        "หมดอายุแล้วเมื่อวันที่ {expiration_date}\n\n"
        "หากต้องการใช้งานต่อ เพียงตอบกลับมา แล้วเราจะจัดการเรื่องการต่ออายุให้\n\n"
        "ขอแสดงความนับถือ\n{business_name}",
    ),
}

_BUILTIN_TH = {
    (subject, body): _THAI_BY_NAME[name] for name, subject, body in DEFAULT_TEMPLATES
}

# App-generated fallback values (not user data) used in reminder_context.
THAI_FALLBACKS = {
    "order_ref": ("n/a", "-"),
    "expiration_date": ("no expiration", "ไม่มีวันหมดอายุ"),
    "business_name": ("Your seller team", "ทีมผู้ขาย"),
}


def current_lang():
    """'th' when the owner chose Thai in the hosting layer, else 'en'. Safe outside a request."""
    if has_request_context() and request.cookies.get(LANG_COOKIE) == "th":
        return "th"
    return "en"


def _norm(text):
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def thai_builtin(subject, body):
    """Thai (subject, body) if the stored text is still an unedited built-in, else None."""
    return _BUILTIN_TH.get((_norm(subject), _norm(body)))


def thai_honorific(text, customer_name):
    """Expand "คุณ{customer_name}": a space before names not starting with a Thai
    letter (e.g. "คุณ Ada"), none before Thai names ("คุณสมชาย"), and no second
    "คุณ" when the stored name already starts with it."""
    name = (customer_name or "").strip()
    if name.startswith("คุณ"):
        prefix = ""
    elif name and "฀" <= name[0] <= "๿":
        prefix = "คุณ"
    else:
        prefix = "คุณ "
    return text.replace("คุณ{customer_name}", prefix + "{customer_name}")
