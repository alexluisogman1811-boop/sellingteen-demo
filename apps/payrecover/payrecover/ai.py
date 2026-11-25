"""Payment reminder drafting: Claude API, mock mode, or a deterministic template.

Every draft, whatever its source, passes the same safety check. Facts (name,
invoice number, amount, due date) always come from the database via
ReminderContext, and a draft that changes them or adds fees, threats, legal
language, discounts, policies or "payment received" claims is replaced by the
template. Logs never include invoice or customer details.
"""
import json
import logging
import re
import urllib.request
from dataclasses import dataclass

from .formatting import plural_days

log = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
TIMEOUT_SECONDS = 12
MAX_LENGTH = 1500


@dataclass(frozen=True)
class ReminderContext:
    customer_name: str
    invoice_number: str
    amount: str  # already formatted, e.g. "$1,250.00"
    due_date: str  # already formatted, e.g. "Oct 3, 2026"
    days_overdue: int
    stage_key: str  # friendly | second | firm | final
    business_name: str

    @property
    def first_name(self):
        return self.customer_name.split()[0] if self.customer_name.split() else self.customer_name


@dataclass(frozen=True)
class Draft:
    text: str
    source: str  # "ai", "mock" or "template"
    note: str = ""


class AIUnavailable(Exception):
    pass


# ---------------------------------------------------------------- templates

TEMPLATES = {
    "friendly": (
        "Hello {name},\n\n"
        "This is a friendly reminder that invoice {number} for {amount} was due on {due}. "
        "If you have already sent payment, please disregard this message.\n\n"
        "Thank you,\n{business}"
    ),
    "second": (
        "Hello {name},\n\n"
        "I'm following up on invoice {number} for {amount}, which was due on {due}. "
        "Could you let me know when I can expect payment?\n\n"
        "Thank you,\n{business}"
    ),
    "firm": (
        "Hello {name},\n\n"
        "Invoice {number} for {amount} is now {days} past its due date of {due}. "
        "Please arrange payment as soon as possible, or reply if there is a problem with the invoice.\n\n"
        "Regards,\n{business}"
    ),
    "final": (
        "Hello {name},\n\n"
        "This is my final reminder about invoice {number} for {amount}, due on {due} and now {days} overdue. "
        "Please arrange payment or contact me this week so we can resolve it.\n\n"
        "Regards,\n{business}"
    ),
}

MOCK_TEMPLATES = {
    "friendly": (
        "Hi {first},\n\n"
        "Hope all is well! Just a quick reminder that invoice {number} for {amount} was due on {due}. "
        "When you have a moment, could you take a look? If payment is already on its way, please ignore this note.\n\n"
        "Thanks so much,\n{business}"
    ),
    "second": (
        "Hi {first},\n\n"
        "I wanted to follow up on invoice {number} for {amount}. It was due on {due} and is now {days} overdue. "
        "Could you let me know when I can expect payment?\n\n"
        "Thank you,\n{business}"
    ),
    "firm": (
        "Hi {first},\n\n"
        "Invoice {number} for {amount} is still unpaid and is now {days} past its due date of {due}. "
        "Please arrange payment as soon as possible. If something on the invoice needs attention, "
        "just reply and I'll help sort it out.\n\n"
        "Regards,\n{business}"
    ),
    "final": (
        "Hi {first},\n\n"
        "This is my final reminder regarding invoice {number} for {amount}, which was due on {due} ({days} ago). "
        "Please arrange payment or get in touch with me this week so we can resolve it.\n\n"
        "Regards,\n{business}"
    ),
}


def _fill(template, ctx):
    return template.format(
        name=ctx.customer_name,
        first=ctx.first_name,
        number=ctx.invoice_number,
        amount=ctx.amount,
        due=ctx.due_date,
        days=plural_days(ctx.days_overdue),
        business=ctx.business_name,
    )


def template_message(ctx):
    return _fill(TEMPLATES.get(ctx.stage_key, TEMPLATES["friendly"]), ctx)


def mock_message(ctx):
    """Stands in for the AI when AI_MOCK=1, so the app works with no API key."""
    return _fill(MOCK_TEMPLATES.get(ctx.stage_key, MOCK_TEMPLATES["friendly"]), ctx)


# ------------------------------------------------------------ safety check

FORBIDDEN = [
    (r"\blate[\s-]*(fee|charge|payment (fee|charge))", "late fee"),
    (r"\bfees?\b|\bsurcharge", "fee"),
    (r"\binterest\b", "interest"),
    (r"\bpenalt", "penalty"),
    (r"\blegal\b|\blawyer|\battorney|\bcourt\b|\blawsuit|\bsue\b|\bsmall claims|\blitigat|\blien\b", "legal"),
    (r"\bcollections?\b", "collections"),
    (r"\bcredit (report|score|bureau|rating|history)", "credit"),
    (r"\bdiscount|\bwaive|% off|\bpercent off", "discount"),
    (r"%", "percentage"),
    (r"\bcontract\b|\bcontracts\b|\bagreement\b|\bterms\b|\bpolic(y|ies)\b", "contract or policy"),
    (r"(payment|funds) (has |have )?(been |was |were )?received|received your payment|"
     r"thank(s| you) for (your|the) payment|payment (is|was) (complete|confirmed)|already paid", "payment received claim"),
    (r"\bor else\b|\bconsequence|\bsuspend|\bterminate|\bfinal notice before|\bescalat|\bthreat", "threat"),
]
_FORBIDDEN_RE = [(re.compile(p, re.IGNORECASE), why) for p, why in FORBIDDEN]
_MONEY_RE = re.compile(r"[$€£]\s?\d[\d,]*(?:\.\d+)?|\b\d[\d,]*\.\d{2}\b")


def _normalise_money(text):
    return re.sub(r"[^\d.]", "", text)


def validate_message(text, ctx):
    """Return a list of problems; empty means the draft is safe to show."""
    problems = []
    if not text or not text.strip():
        return ["empty"]
    if len(text) > MAX_LENGTH:
        problems.append("too long")
    if ctx.invoice_number not in text:
        problems.append("invoice number missing")
    if ctx.amount not in text:
        problems.append("amount missing")
    if ctx.first_name.lower() not in text.lower():
        problems.append("customer name missing")
    expected = _normalise_money(ctx.amount)
    for found in _MONEY_RE.findall(text):
        if _normalise_money(found) != expected:
            problems.append("different amount")
            break
    for pattern, why in _FORBIDDEN_RE:
        if pattern.search(text):
            problems.append(why)
    return problems


# ------------------------------------------------------------- Claude API

SYSTEM_PROMPT = """You write short, polite payment reminder messages that a small service business \
(plumber, electrician, cleaner, landscaper, handyman, contractor) sends to a customer.

Rules you must follow:
- Use ONLY the facts provided. Copy the customer name, invoice number, amount and due date exactly as given.
- Never threaten the customer.
- Never mention late fees, other fees, interest, penalties, discounts, contracts, agreements, payment terms, \
policies, legal action, courts, collections or credit reports.
- Never say or imply that a payment was received.
- Never change the amount and never mention any other amount or percentage.
- Plain text only. No subject line. Under 90 words. Sign off with the business name.

Tone by stage:
- friendly: warm and light.
- second: polite follow-up asking when to expect payment.
- firm: clear and direct, still courteous; invite them to reply if there is a problem.
- final: serious and respectful; ask them to pay or get in touch this week."""


def call_claude(ctx, config):
    facts = {
        "stage": ctx.stage_key,
        "customer_name": ctx.customer_name,
        "invoice_number": ctx.invoice_number,
        "amount": ctx.amount,
        "due_date": ctx.due_date,
        "days_overdue": ctx.days_overdue,
        "business_name": ctx.business_name,
    }
    body = json.dumps({
        "model": config.get("AI_MODEL", "claude-haiku-5-5"),
        "max_tokens": 400,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": "Write the reminder using these facts:\n" + json.dumps(facts)}],
    }).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=body,
        method="POST",
        headers={
            "content-type": "application/json",
            "x-api-key": config["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # network, HTTP error, bad JSON
        raise AIUnavailable(type(exc).__name__) from None
    parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    text = "".join(parts).strip()
    if not text:
        raise AIUnavailable("empty response")
    return text


# --------------------------------------------------------------- entry point

def generate_reminder(ctx, config):
    """Draft a reminder. Never raises; falls back to the template."""
    if config.get("AI_MOCK"):
        text, source = mock_message(ctx), "mock"
    elif config.get("ANTHROPIC_API_KEY"):
        try:
            text, source = call_claude(ctx, config), "ai"
        except AIUnavailable as exc:
            log.warning("AI draft unavailable (%s); using template", exc)
            return Draft(template_message(ctx), "template", "AI was unavailable, so a standard template was used.")
    else:
        return Draft(template_message(ctx), "template")

    problems = validate_message(text, ctx)
    if problems:
        log.warning("AI draft rejected by safety check (%d issue(s)); using template", len(problems))
        return Draft(
            template_message(ctx),
            "template",
            "The AI draft didn't pass the safety check, so a standard template was used.",
        )
    return Draft(text.strip(), source)
