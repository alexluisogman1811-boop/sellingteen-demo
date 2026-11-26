"""Follow-up message generation.

Two modes:
  * Mock (AI_MOCK=1, or no API key configured): deterministic templates built
    only from the stored lead fields. No network calls, no credits needed.
  * Real (AI_MOCK=0 and ANTHROPIC_API_KEY set): calls the Anthropic Messages
    API. Output is checked against the guardrails below; anything that looks
    like a price, discount or guarantee is discarded and the mock message is
    used instead.

The API key is read from config only at call time and is never logged,
returned to the browser, or included in error messages.
"""
import json
import logging
import re
import urllib.error
import urllib.request

from flask import current_app, has_request_context, request

log = logging.getLogger(__name__)

STYLES = ["friendly", "professional", "short"]

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

# Phrases a follow-up must never contain: prices, discounts, guarantees, pressure.
FORBIDDEN = re.compile(
    r"(\$|฿|€|£|¥|\b(usd|thb|eur|baht|dollars?)\b|\d+\s?%|\bdiscount|\bpromo|\bcoupon|\bdeal\b"
    r"|\bguarantee|\bfree of charge|\bfor free\b|\blimited time|\bact now|\bhurry|\blast chance|\bspecial offer)",
    re.IGNORECASE,
)

_SOURCE_PHRASES = {
    "Website": "through our website",
    "Phone": "by phone",
    "Facebook": "on Facebook",
    "LINE": "on LINE",
    "WhatsApp": "on WhatsApp",
}


LANGUAGE_COOKIE = "st_lang"


def message_language() -> str:
    """Language for newly written messages: "th" when the owner chose Thai, else "en".

    The hosting layer sets the cookie st_lang=th when Thai is selected. Safe to
    call outside a request (always "en" then). Only used at generation time, so
    a message that was already written is never changed.
    """
    if has_request_context() and request.cookies.get(LANGUAGE_COOKIE) == "th":
        return "th"
    return "en"


class GenerationResult(dict):
    """{'message': str, 'mode': 'demo' | 'ai', 'notice': str | None}"""


def ai_enabled() -> bool:
    cfg = current_app.config
    return not cfg["AI_MOCK"] and bool(cfg.get("ANTHROPIC_API_KEY"))


# Values people type when they don't know the customer's name.
_PLACEHOLDER_NAMES = {
    "unknown", "n/a", "na", "none", "-", "?", "customer", "client", "lead", "no name",
    "facebook user", "line user", "whatsapp user", "website visitor", "caller",
}


def _clean_name(name: str) -> str | None:
    name = " ".join((name or "").split())
    if not name or name.lower() in _PLACEHOLDER_NAMES or not any(ch.isalpha() for ch in name):
        return None
    return name


def _service_phrase(service: str) -> str | None:
    service = (service or "").strip().rstrip(".")
    if not service:
        return None
    # "House cleaning" -> "house cleaning", but keep "AC repair" / "LINE ads" as typed.
    if len(service) > 1 and service[0].isupper() and service[1].islower():
        service = service[0].lower() + service[1:]
    return service


# Titles people type before a name. Thai-style titles go with the given name
# ("Khun Nok"); Western titles go with the family name ("Mr. Smith").
_TITLES_GIVEN = {"khun", "k", "p", "phi", "nong"}
_TITLES_FAMILY = {"mr", "mrs", "ms", "miss", "mx", "dr", "prof", "sir", "madam"}


def _greeting_name(name: str | None) -> str | None:
    """How to address someone in 'Hi ___,' without inventing anything."""
    if not name:
        return None
    parts = name.split()
    title = parts[0].lower().rstrip(".'")
    if title in _TITLES_GIVEN | _TITLES_FAMILY:
        if len(parts) == 1:
            return None
        return f"{parts[0]} {parts[1] if title in _TITLES_GIVEN else parts[-1]}"
    return parts[0]


def mock_message(lead, style: str, business_name: str, lang: str = "en") -> str:
    """Deterministic, safe message built only from stored lead data, in English or Thai."""
    if lang == "th":
        return _mock_message_th(lead, style, business_name)
    return _mock_message_en(lead, style, business_name)


def _mock_message_en(lead, style: str, business_name: str) -> str:
    """Deterministic, safe message built only from stored lead data.

    It never refers to earlier conversations, prices, offers or agreements,
    because none of that is something a template can know is true.
    """
    name = _clean_name(lead["name"])
    first = _greeting_name(name)
    service = _service_phrase(lead["service"])
    biz = (business_name or "").strip() or "our team"
    status = lead["status"] if lead["status"] in ("New", "Contacted", "Interested", "Won", "Lost") else "Interested"
    via = _SOURCE_PHRASES.get(lead["source"])
    reached = f"reaching out {via}" if via else "getting in touch"
    about = f" about {service}" if service else ""          # "... about house cleaning"
    enquiry = f"your enquiry about {service}" if service else "your enquiry"
    help_with = f" with {service}" if service else ""        # first mention of the service
    for_it = f" for {service}" if service else ""            # "choosing us for ..."
    hi = f"Hi {first}" if first else "Hi there"

    if style == "short":
        return {
            "New": f"{hi}, thanks for {reached}{about}. Is now a good time to chat about what you need? – {biz}",
            "Contacted": f"{hi}, just following up on {enquiry}. Would you still like some help? – {biz}",
            "Interested": f"{hi}, just checking in – would you still like help{help_with}? Let me know. – {biz}",
            "Won": f"{hi}, thanks again for choosing us{for_it}. Is everything going well? – {biz}",
            "Lost": f"{hi}, thanks for considering us{for_it}. We're here if you ever need help in the future. – {biz}",
        }[status]

    if style == "professional":
        greeting = f"Hello {name}," if name else "Hello,"
        body = {
            "New": f"Thank you for {reached}{about}. I would be glad to answer any questions and understand what you need. Please let me know a convenient time to talk, or simply reply to this message.",
            "Contacted": f"I am following up on {enquiry}. Please let me know if you would still like assistance, or if you have any questions I can help with.",
            "Interested": f"I wanted to follow up on {enquiry}. Would you still like our help? I am happy to answer any questions or arrange a convenient time to discuss the next steps.",
            "Won": f"Thank you for choosing {biz}{for_it}. I wanted to check that everything is going well and see if there is anything further we can help with.",
            "Lost": f"Thank you for considering {biz}{for_it}. If your needs change in the future, please feel free to contact us at any time.",
        }[status]
        return f"{greeting}\n\n{body}\n\nKind regards,\n{biz}"

    # friendly (default)
    body = {
        "New": f"thanks so much for {reached}{about}! I wanted to follow up and see if you have any questions, or if you'd like to talk through what you need. Just reply here whenever suits you.",
        "Contacted": f"just following up on {enquiry}. Would you still like some help? Happy to answer any questions you have.",
        "Interested": f"I hope you're doing well! I wanted to check in and see whether you'd still like help{help_with}. Let me know if you have any questions or if there's a good time to talk.",
        "Won": f"thanks again for choosing {biz}{for_it}. I just wanted to check in and see how everything is going. Let us know if you need anything!",
        "Lost": f"I hope you're doing well. Thanks again for considering us{for_it}. If you ever need a hand in the future, feel free to get in touch anytime.",
    }[status]
    return f"{hi}, {body}\n\n{biz}"


# ---------------------------------------------------------------- Thai templates
# Same facts and structure as the English templates, written for LINE/Facebook
# chat. Wording is polite but gender-neutral (no ค่ะ/ครับ), so it suits any
# owner. Brand names (LINE, Facebook, WhatsApp) stay as Thai people write them.

_SOURCE_PHRASES_TH = {
    "Website": "ผ่านเว็บไซต์",
    "Phone": "ทางโทรศัพท์",
    "Facebook": "ทาง Facebook",
    "LINE": "ทาง LINE",
    "WhatsApp": "ทาง WhatsApp",
}

# Romanised titles typed before a name, and the Thai word to use instead.
_TITLES_TH = {"khun": "คุณ", "k": "คุณ", "p": "พี่", "phi": "พี่", "nong": "น้อง"}
# Titles typed in Thai script: keep them as typed, don't add another คุณ.
_THAI_SCRIPT_TITLES = ("คุณ", "พี่", "น้อง", "ป้า", "ลุง", "น้า", "เจ๊", "เฮีย", "ท่าน", "ดร.")
# Words that mean the "name" is a company or shop rather than a person: no คุณ.
_ORG_WORDS = re.compile(
    r"(\b(co|company|ltd|limited|inc|corp|llc|plc|shop|store|hotel|resort|restaurant|cafe|clinic)\b"
    r"|บริษัท|ห้างหุ้นส่วน|หจก|ร้าน|โรงแรม|รีสอร์ท|คลินิก|สำนักงาน|โรงเรียน)",
    re.IGNORECASE,
)


def _is_thai_char(ch: str) -> bool:
    return "\u0e00" <= ch <= "\u0e7f"


def _starts_thai(text: str) -> bool:
    return bool(text) and _is_thai_char(text[0])


def _j(*pieces) -> str:
    """Join phrase pieces the way Thai is written: no space between two Thai
    words ("ติดต่อมา" + "ทาง LINE"), a space next to Latin text or digits
    ("เรื่อง" + "AC repair"). Empty pieces are skipped."""
    out = ""
    for piece in pieces:
        if not piece:
            continue
        if out and not (_is_thai_char(out[-1]) and _is_thai_char(piece[0])):
            out += " "
        out += piece
    return out


def _thai_name(name: str | None, full: bool) -> str | None:
    """How to address someone in Thai: คุณ + name for a person, as typed for a business.

    full=False uses the given name (like "Hi John"); full=True the whole name
    (like "Hello John Smith"). Never invents a name.
    """
    if not name:
        return None
    if _ORG_WORDS.search(name):
        return name
    parts = name.split()
    if parts[0] in _THAI_SCRIPT_TITLES:           # "คุณ นก ใจดี" -> "คุณนก ใจดี"
        if len(parts) == 1:
            return None
        head, rest = _j(parts[0], parts[1]), parts[2:]
        return " ".join([head, *rest]) if full else head
    if name.startswith(_THAI_SCRIPT_TITLES):      # "คุณนก ใจดี": title already attached
        return name if full else parts[0]
    title = parts[0].lower().rstrip(".'")
    if title in _TITLES_GIVEN | _TITLES_FAMILY:
        if len(parts) == 1:
            return None
        rest = parts[1:] if full else ([parts[1]] if title in _TITLES_GIVEN else [parts[-1]])
        prefix = _TITLES_TH.get(title, "คุณ")
    else:
        rest = parts if full else parts[:1]
        prefix = "คุณ"
    # "คุณสมชาย" (Thai script, no space) but "คุณ John" (space before Latin letters).
    return " ".join([_j(prefix, rest[0]), *rest[1:]])


def _mock_message_th(lead, style: str, business_name: str) -> str:
    """Thai version of the template message, using exactly the same stored facts."""
    name = _clean_name(lead["name"])
    service = (lead["service"] or "").strip().rstrip(".").strip() or None
    biz_typed = (business_name or "").strip()
    biz = biz_typed or "ทีมงานของเรา"
    status = lead["status"] if lead["status"] in ("New", "Contacted", "Interested", "Won", "Lost") else "Interested"
    reached = _j("ติดต่อมา", _SOURCE_PHRASES_TH.get(lead["source"]))   # "ติดต่อมาทาง LINE"
    about = _j("เรื่อง", service) if service else None                   # "เรื่อง AC repair"
    enquiry = _j("เรื่องที่สอบถามไว้", _j("เกี่ยวกับ", service) if service else None)
    still_help = _j("ยังต้องการให้ช่วย", about, "อยู่ไหม")                # "... ให้ช่วยเรื่อง X อยู่ไหม"
    svc_ours = _j("บริการ", service, "ของเรา")                           # "บริการ X ของเรา"
    svc_biz = _j("บริการ", service, "ของ", biz_typed) if biz_typed else svc_ours
    hi = _j("สวัสดี", _thai_name(name, full=False))

    if style == "short":
        return {
            "New": f"{hi} {_j('ขอบคุณที่' + reached, about)} ตอนนี้สะดวกคุยรายละเอียดเรื่องที่ต้องการไหม – {biz}",
            "Contacted": f"{hi} {_j('ขอติดตาม' + enquiry)} ยังต้องการให้ช่วยอยู่ไหม – {biz}",
            "Interested": f"{hi} ขอสอบถามอีกครั้งว่า{still_help} สะดวกเมื่อไรแจ้งได้เลย – {biz}",
            "Won": f"{hi} ขอบคุณอีกครั้งที่เลือกใช้{svc_ours} ทุกอย่างเรียบร้อยดีไหม – {biz}",
            "Lost": f"{hi} ขอบคุณที่พิจารณา{svc_ours} หากวันไหนต้องการความช่วยเหลือ ติดต่อมาได้เสมอ – {biz}",
        }[status]

    if style == "professional":
        full_name = _thai_name(name, full=True)
        greeting = f"เรียน {full_name}" if full_name else "สวัสดี"
        body = {
            "New": f"{_j('ขอบคุณที่' + reached, about)} ยินดีตอบทุกคำถามและรับฟังความต้องการของคุณ กรุณาแจ้งเวลาที่สะดวกสำหรับการพูดคุย หรือตอบกลับข้อความนี้ได้เลย",
            "Contacted": f"ขอติดตาม{enquiry} หากยังต้องการความช่วยเหลือ หรือมีคำถามใดที่ให้เราช่วยได้ แจ้งได้เลย",
            "Interested": f"ขอติดตาม{enquiry} ยังต้องการให้เราช่วยอยู่หรือไม่ ยินดีตอบทุกคำถาม หรือนัดเวลาที่สะดวกเพื่อพูดคุยขั้นตอนต่อไป",
            "Won": f"ขอบคุณที่เลือกใช้{svc_biz} ขอสอบถามว่าทุกอย่างเรียบร้อยดีหรือไม่ และมีสิ่งใดที่เราช่วยเพิ่มเติมได้อีกบ้าง",
            "Lost": f"ขอบคุณที่พิจารณา{svc_biz} หากในอนาคตความต้องการเปลี่ยนไป ติดต่อเราได้ทุกเมื่อ",
        }[status]
        return f"{greeting}\n\n{body}\n\nขอแสดงความนับถือ\n{biz}"

    # friendly (default)
    body = {
        "New": f"{_j('ขอบคุณมากที่' + reached, about)} ถ้ามีคำถาม หรืออยากคุยรายละเอียดเรื่องที่ต้องการ ตอบกลับมาที่นี่ได้เลย สะดวกเมื่อไรก็ได้",
        "Contacted": f"ขอติดตาม{enquiry} ยังต้องการให้ช่วยอยู่ไหม มีคำถามอะไรถามได้เลย",
        "Interested": f"หวังว่าสบายดี ขอสอบถามว่า{still_help} ถ้ามีคำถาม หรือสะดวกคุยช่วงไหน แจ้งได้เลย",
        "Won": f"ขอบคุณอีกครั้งที่เลือกใช้{svc_biz} อยากสอบถามว่าทุกอย่างเป็นอย่างไรบ้าง ถ้าต้องการอะไรเพิ่มเติม บอกได้เลย",
        "Lost": f"หวังว่าสบายดี ขอบคุณอีกครั้งที่พิจารณา{svc_ours} หากวันไหนต้องการความช่วยเหลือ ติดต่อมาได้ตลอด",
    }[status]
    return f"{hi} {body}\n\n{biz}"


def _build_prompt(lead, style: str, business_name: str, latest_contact=None, lang: str = "en") -> tuple[str, str]:
    system = (
        "You write short follow-up messages that a small business owner sends to a potential customer "
        "by chat or email. Use ONLY the facts provided. Rules: never invent customer details, never "
        "mention prices or amounts, never offer or promise discounts, never make guarantees, never "
        "claim the customer agreed to anything unless the notes say so explicitly, never refer to an "
        "earlier conversation unless latest_contact describes one, no pressure or urgency tactics, no "
        "emojis, no subject line. If customer_name is missing, greet them without a name. Write in plain "
        "natural English. Output only the message text."
    )
    style_hint = {
        "friendly": "Warm and friendly, 2-4 sentences, signed with the business name.",
        "professional": "Polite and professional, starts with 'Hello <name>,' and ends with 'Kind regards,' and the business name.",
        "short": "One or two short sentences, suitable for a chat app.",
    }[style]
    if lang == "th":
        system = system.replace(
            "Write in plain natural English.",
            "LANGUAGE: Write the whole message in natural, polite Thai, the way a Thai small business writes "
            "to a customer on LINE or Facebook. Use gender-neutral polite wording: do not use ค่ะ, คะ or ครับ. "
            "Address a person as คุณ followed by their name; if customer_name is a company or shop, use it as "
            "written. Keep the business name, customer name and service exactly as given. All rules above "
            "still apply.",
        )
        style_hint = {
            "friendly": "Warm and friendly Thai, 2-4 sentences, signed with the business name.",
            "professional": "Polite, formal Thai: start with 'เรียน คุณ<name>' (or 'สวัสดี' if there is no name) "
                            "and end with 'ขอแสดงความนับถือ' and the business name.",
            "short": "One or two short Thai sentences, suitable for a chat app.",
        }[style]
    facts = {
        "business_name": business_name,
        "customer_name": _clean_name(lead["name"]) or "(not recorded)",
        "service_interested_in": lead["service"] or "(not recorded)",
        "lead_status": lead["status"],
        "lead_source": lead["source"],
        "notes": lead["notes"] or "(none)",
        "latest_contact": latest_contact or "(none recorded)",
    }
    user = f"Style: {style_hint}\n\nLead facts (JSON):\n{json.dumps(facts, ensure_ascii=False)}"
    return system, user


def _call_anthropic(system: str, user: str) -> str:
    cfg = current_app.config
    payload = json.dumps(
        {
            "model": cfg["AI_MODEL"],
            "max_tokens": 400,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        ANTHROPIC_URL,
        data=payload,
        method="POST",
        headers={
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": cfg["ANTHROPIC_API_KEY"],
        },
    )
    with urllib.request.urlopen(req, timeout=cfg.get("AI_TIMEOUT", 20)) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    return "".join(parts).strip()


# Thai equivalents, checked only for Thai messages: baht/price, discount, promotion,
# coupon, free, guarantee, limited time/quantity, last chance, special offer, hurry.
FORBIDDEN_TH = re.compile(
    r"(บาท|ราคา|ส่วนลด|ลดราคา|โปรโมชั่น|โปรโมชัน|คูปอง|ฟรี|รับประกัน|การันตี|รับรองผล|จำนวนจำกัด"
    r"|เวลาจำกัด|โอกาสสุดท้าย|ข้อเสนอพิเศษ|รีบ|ด่วน|เปอร์เซ็นต์)"
)


def is_safe_message(text: str, lang: str = "en") -> bool:
    if lang == "th" and text and FORBIDDEN_TH.search(text):
        return False
    return bool(text) and len(text) <= 1200 and not FORBIDDEN.search(text)


def generate_followup(lead, style: str, business_name: str, latest_contact=None, lang: str | None = None) -> GenerationResult:
    if style not in STYLES:
        raise ValueError("Unknown style")
    lang = lang or message_language()  # decided now, at generation time only

    if not ai_enabled():
        return GenerationResult(message=mock_message(lead, style, business_name, lang), mode="demo", notice=None)

    try:
        system, user = _build_prompt(lead, style, business_name, latest_contact, lang)
        text = _call_anthropic(system, user)
    except Exception as exc:  # noqa: BLE001 - any provider failure falls back to the template
        # Log only the exception type: messages can echo request details or the key.
        log.warning("AI request failed (%s); using template message", type(exc).__name__)
        return GenerationResult(
            message=mock_message(lead, style, business_name, lang),
            mode="demo",
            notice="AI is unavailable right now, so a template message was used.",
        )

    if not is_safe_message(text, lang):
        log.warning("AI output failed safety checks; using template message")
        return GenerationResult(
            message=mock_message(lead, style, business_name, lang),
            mode="demo",
            notice="The AI draft didn't pass our safety checks, so a template message was used.",
        )
    return GenerationResult(message=text, mode="ai", notice=None)
