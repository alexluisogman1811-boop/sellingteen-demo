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

from flask import current_app

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


def mock_message(lead, style: str, business_name: str) -> str:
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


def _build_prompt(lead, style: str, business_name: str, latest_contact=None) -> tuple[str, str]:
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


def is_safe_message(text: str) -> bool:
    return bool(text) and len(text) <= 1200 and not FORBIDDEN.search(text)


def generate_followup(lead, style: str, business_name: str, latest_contact=None) -> GenerationResult:
    if style not in STYLES:
        raise ValueError("Unknown style")

    if not ai_enabled():
        return GenerationResult(message=mock_message(lead, style, business_name), mode="demo", notice=None)

    try:
        system, user = _build_prompt(lead, style, business_name, latest_contact)
        text = _call_anthropic(system, user)
    except Exception as exc:  # noqa: BLE001 - any provider failure falls back to the template
        # Log only the exception type: messages can echo request details or the key.
        log.warning("AI request failed (%s); using template message", type(exc).__name__)
        return GenerationResult(
            message=mock_message(lead, style, business_name),
            mode="demo",
            notice="AI is unavailable right now, so a template message was used.",
        )

    if not is_safe_message(text):
        log.warning("AI output failed safety checks; using template message")
        return GenerationResult(
            message=mock_message(lead, style, business_name),
            mode="demo",
            notice="The AI draft didn't pass our safety checks, so a template message was used.",
        )
    return GenerationResult(message=text, mode="ai", notice=None)
