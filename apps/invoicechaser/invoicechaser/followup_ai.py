"""Payment follow-up message generation.

Safety design
-------------
* The AI never sees invoice facts, customer data or business data. It only writes
  wording that refers to facts through placeholders such as {{amount}}; the server
  fills them in from the database. So it cannot invent or leak an amount, name,
  invoice number, date or payment detail.
* AI output is rejected (and the built-in template used) if it contains digits,
  currency symbols, links, emails, unknown placeholders, or phrases that would
  introduce discounts, fees, legal claims, promises, payment instructions or
  claims about past contact.
* AI_MOCK=1 (the default) never calls an API. It returns the deterministic built-in
  templates and records source="mock" so mock output is identifiable in the DB/logs.
* Any API failure falls back to the template with a friendly notice; the technical
  error is logged server-side only.
"""
import logging
import os
import re

from flask import current_app

from .util import days_overdue, days_until_due, format_date, format_money, invoice_status, plural

log = logging.getLogger(__name__)

# The five situations, in escalation order.
TONES = {
    "reminder": "Friendly reminder",
    "due_soon": "Due soon",
    "just_overdue": "Just overdue",
    "overdue": "Several days overdue",
    "final": "Final polite reminder",
}
# Labels for tones saved by earlier versions, so old history still reads well.
LEGACY_TONES = {"friendly": "Friendly reminder", "polite": "Polite follow-up", "firm": "Firm follow-up"}
CHANNELS = {"email": "Email", "sms": "Text / chat message", "other": "Other"}
LEGACY_CHANNELS = {"phone": "Phone call"}
SOURCES = {"ai": "AI-written", "template": "Standard template", "mock": "Standard template (AI_MOCK)", "edited": "Edited"}

PLACEHOLDERS = {"greeting", "customer", "contact", "invoice_number", "amount", "due_date", "issue_date",
                "days_overdue", "due_when", "business", "signoff"}
REQUIRED_PLACEHOLDERS = {"invoice_number", "amount"}
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
_FORBIDDEN_CHARS_RE = re.compile(r"[0-9$€£¥฿₹%]|https?://|www\.|@")
# Whole-word matches so "issue" doesn't trip "sue" or "courteous" trip "court".
FORBIDDEN_RE = re.compile(
    r"\b(?:discount\w*|waiv\w*|legal\w*|lawyer\w*|attorney\w*|courts?|lawsuit\w*|sue[sd]?|suing"
    r"|collection\w*|collector\w*|credit\w*|fees?|interest|penalt\w*|charg\w*|guarantee\w*|promis\w*"
    r"|refund\w*|bank\w*|iban|swift|routing|paypal|venmo|zelle|stripe|cards?|wire|transfer\w*|cash"
    r"|cheque\w*|deadline\w*|within|immediately|suspend\w*|terminat\w*|report\w*|previous\w*|earlier"
    r"|again|third|as discussed|as agreed|per our|already reminded|last reminder|second reminder|final notice)\b",
    re.IGNORECASE,
)

EMAIL_TEMPLATES = {
    "reminder": (
        "{{greeting}},\n\n"
        "I hope all is well. Just a quick heads-up that invoice {{invoice_number}} for {{amount}} "
        "is due on {{due_date}}.\n\n"
        "If you need another copy of the invoice or have any questions about it, just let me know.\n\n"
        "Thank you,\n{{signoff}}"
    ),
    "due_soon": (
        "{{greeting}},\n\n"
        "A friendly reminder that invoice {{invoice_number}} for {{amount}} is due {{due_when}} ({{due_date}}).\n\n"
        "If payment is already on its way, thank you, and please disregard this note. "
        "If you need another copy of the invoice, just reply and I'll send it over.\n\n"
        "Best regards,\n{{signoff}}"
    ),
    "just_overdue": (
        "{{greeting}},\n\n"
        "I wanted to follow up on invoice {{invoice_number}} for {{amount}}, which was due on {{due_date}}. "
        "It may simply have slipped through the cracks, so this is just a gentle reminder.\n\n"
        "If you've already sent payment, thank you, and please ignore this message. "
        "Otherwise, could you let me know when we can expect it?\n\n"
        "Best regards,\n{{signoff}}"
    ),
    "overdue": (
        "{{greeting}},\n\n"
        "I'm following up on invoice {{invoice_number}} for {{amount}}, which was due on {{due_date}} "
        "and is now {{days_overdue}} past due.\n\n"
        "Could you please let me know when payment will be made? If there's a problem with the invoice "
        "or anything I can help clarify, I'm happy to sort it out.\n\n"
        "Thank you,\n{{signoff}}"
    ),
    "final": (
        "{{greeting}},\n\n"
        "I'm writing about invoice {{invoice_number}} for {{amount}}, which was due on {{due_date}} "
        "and is now {{days_overdue}} past due.\n\n"
        "I'd be grateful if you could arrange payment, or reply with an expected payment date, as soon as "
        "you're able. If something about the invoice needs to be resolved, please let me know so we can "
        "settle it together.\n\n"
        "Kind regards,\n{{signoff}}"
    ),
}

SMS_TEMPLATES = {
    "reminder": "{{greeting}}, a quick reminder that invoice {{invoice_number}} for {{amount}} is due on {{due_date}}. Thanks! – {{business}}",
    "due_soon": "{{greeting}}, friendly reminder that invoice {{invoice_number}} for {{amount}} is due {{due_when}}. Thanks! – {{business}}",
    "just_overdue": "{{greeting}}, just a gentle reminder that invoice {{invoice_number}} for {{amount}} was due on {{due_date}}. If it's already sent, thank you! – {{business}}",
    "overdue": "{{greeting}}, invoice {{invoice_number}} for {{amount}} is now {{days_overdue}} past due. Could you let me know when payment will be made? Thank you – {{business}}",
    "final": "{{greeting}}, invoice {{invoice_number}} for {{amount}} is {{days_overdue}} past due. I'd be grateful for payment or an expected date as soon as you're able. Thank you – {{business}}",
}

SUBJECTS = {
    "reminder": "Upcoming invoice {{invoice_number}}",
    "due_soon": "Reminder: invoice {{invoice_number}} is due {{due_when}}",
    "just_overdue": "Invoice {{invoice_number}}: payment reminder",
    "overdue": "Invoice {{invoice_number}} is past due",
    "final": "Invoice {{invoice_number}}: payment reminder",
}

AI_UNAVAILABLE = ("The AI writer isn't available right now, so we've used our standard message instead. "
                  "You can edit it before sending, or try again in a moment.")
AI_REJECTED = ("The AI suggestion didn't pass our accuracy checks, so we've used our standard message instead. "
               "You can edit it before sending.")
AI_BUSY = ("You've generated a lot of messages in a short time, so we've used our standard message. "
           "AI wording will be available again in a few minutes.")


def allowed_tones(inv):
    status = invoice_status(inv)
    if status == "overdue":
        return ["just_overdue", "overdue", "final"]
    if status == "due_soon":
        return ["reminder", "due_soon"]
    return ["reminder"]


def suggested_tone(inv):
    status = invoice_status(inv)
    if status == "due_soon":
        return "due_soon"
    if status != "overdue":
        return "reminder"
    days = days_overdue(inv)
    if days <= 3:
        return "just_overdue"
    if days < 30:
        return "overdue"
    return "final"


def tone_label(key):
    return TONES.get(key) or LEGACY_TONES.get(key, key)


def channel_label(key):
    return CHANNELS.get(key) or LEGACY_CHANNELS.get(key, key)


TITLES = {"dr", "mr", "mrs", "ms", "miss", "mx", "prof"}


def greeting_name(contact):
    """How a person would address the contact: 'Sophie Martin' -> 'Sophie', 'Dr. Anita Patel' -> 'Dr. Patel'."""
    words = (contact or "").split()
    if not words:
        return ""
    if words[0].rstrip(".").lower() in TITLES and len(words) > 1:
        return f"{words[0]} {words[-1]}"
    return words[0]


def facts_for(inv, user):
    days_left = days_until_due(inv)
    due_when = {0: "today", 1: "tomorrow"}.get(days_left) or (
        f"in {days_left} days" if days_left > 1 else f"on {format_date(inv['due_date'])}"
    )
    sender = (user["sender_name"] or "").strip()
    name = greeting_name(inv["contact_name"])
    return {
        # Without a contact person, "Hello" reads better than "Hi Northside Yoga".
        "greeting": f"Hi {name}" if name else "Hello",
        "customer": inv["customer_name"],
        "contact": name or inv["customer_name"],
        "invoice_number": inv["number"],
        "amount": format_money(inv["amount_cents"], user["currency"]),
        "due_date": format_date(inv["due_date"]),
        "issue_date": format_date(inv["issue_date"]),
        "days_overdue": plural(days_overdue(inv), "day"),
        "due_when": due_when,
        "business": user["business_name"],
        "signoff": f"{sender}\n{user['business_name']}" if sender and sender != user["business_name"] else user["business_name"],
    }


def render(template, facts):
    return _PLACEHOLDER_RE.sub(lambda m: facts.get(m.group(1), ""), template)


def subject_for(inv, user, tone):
    return render(SUBJECTS.get(tone, SUBJECTS["overdue"]), facts_for(inv, user))


def is_safe_template(text):
    """True if wording references facts only through placeholders and adds no risky claims."""
    if not text or len(text) > 3000:
        return False
    used = set(_PLACEHOLDER_RE.findall(text))
    if not used <= PLACEHOLDERS or not REQUIRED_PLACEHOLDERS <= used:
        return False
    stripped = _PLACEHOLDER_RE.sub("", text)
    if _FORBIDDEN_CHARS_RE.search(stripped) or "{" in stripped or "}" in stripped:
        return False
    return not FORBIDDEN_RE.search(" ".join(stripped.split()))


def _api_key():
    # Read once in create_app(); the environment is a fallback for local runs and tests.
    return current_app.config.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")


def ai_enabled():
    return not current_app.config["AI_MOCK"] and bool(_api_key())


SYSTEM_PROMPT = (
    "You write the wording of short, warm, professional payment follow-up messages for a small business. "
    "You do not know any facts. Refer to facts ONLY with these placeholders, written exactly: "
    "{{greeting}} (e.g. 'Hi Sam' or 'Hello'; start the message with it), {{contact}} (first name), {{invoice_number}}, {{amount}}, {{due_date}}, {{due_when}} (e.g. 'tomorrow'), "
    "{{days_overdue}} (e.g. '5 days'), {{business}}, {{signoff}} (signature block). "
    "Always include {{invoice_number}} and {{amount}}. "
    "Never write digits, currency symbols, dates, links or email addresses. Never mention or imply: discounts, "
    "fees, interest, penalties, legal action, debt collection, credit reporting, deadlines, guarantees, promises, "
    "refunds, payment methods or bank details, or any earlier messages or conversations. "
    "Return only the message text (greeting, body, sign-off using {{signoff}}). No preamble, no subject line."
)

SITUATION_HINTS = {
    "reminder": "The invoice is not due yet. Light, friendly heads-up.",
    "due_soon": "The invoice is due very soon ({{due_when}}). Friendly reminder.",
    "just_overdue": "The invoice has just become overdue. Assume it was an oversight. Gentle.",
    "overdue": "The invoice is {{days_overdue}} overdue. Polite but clear request for a payment date.",
    "final": "The invoice is well overdue. A final, still courteous and respectful request. No threats.",
}


def _call_claude(tone, channel, instruction):
    import anthropic

    client = anthropic.Anthropic(api_key=_api_key(), timeout=30.0, max_retries=1)
    length = "a short text message (under 50 words, signed with {{business}} instead of {{signoff}})" \
        if channel == "sms" else "an email body (under 140 words)"
    user_msg = f"Write {length}. Situation: {SITUATION_HINTS[tone]}"
    if instruction:
        user_msg += f"\nStyle preference from the user (wording only; never add facts): {instruction}"
    response = client.beta.messages.create(
        model=current_app.config["ANTHROPIC_MODEL"],
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": user_msg}],
    )
    if response.stop_reason in ("refusal", "max_tokens"):
        log.warning("AI follow-up stopped early: stop_reason=%s", response.stop_reason)
        return None
    return "".join(b.text for b in response.content if b.type == "text").strip()


def template_for(tone, channel):
    return (SMS_TEMPLATES if channel == "sms" else EMAIL_TEMPLATES)[tone]


def generate(inv, user, tone, channel="email", instruction="", allow_ai=True):
    """Return dict(message, tone, channel, source, notice). Never raises for AI problems."""
    if tone not in allowed_tones(inv):
        tone = suggested_tone(inv)
    if channel not in CHANNELS:
        channel = "email"
    facts = facts_for(inv, user)
    result = {"tone": tone, "channel": channel, "notice": ""}

    if current_app.config["AI_MOCK"]:
        log.debug("AI_MOCK=1: returning deterministic template for tone=%s channel=%s", tone, channel)
        return {**result, "message": render(template_for(tone, channel), facts), "source": "mock"}

    if ai_enabled():
        if not allow_ai:
            result["notice"] = AI_BUSY
        else:
            try:
                draft = _call_claude(tone, channel, (instruction or "").strip()[:300])
                if draft and is_safe_template(draft):
                    return {**result, "message": render(draft, facts), "source": "ai"}
                log.warning("AI draft rejected by safety check (tone=%s): %r", tone, (draft or "")[:500])
                result["notice"] = AI_REJECTED
            except Exception as exc:  # network, auth, rate limit, bad response: never break the page
                log.error("AI follow-up generation failed (%s): %s", type(exc).__name__, exc)
                result["notice"] = AI_UNAVAILABLE
    return {**result, "message": render(template_for(tone, channel), facts), "source": "template"}
