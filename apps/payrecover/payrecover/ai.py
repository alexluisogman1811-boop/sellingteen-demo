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

from flask import has_request_context, request

from .formatting import plural_days, thai_days

log = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
TIMEOUT_SECONDS = 12
MAX_LENGTH = 1500


def message_language():
    """Language for newly written reminders: "th" when the hosting layer's st_lang
    cookie chooses Thai, otherwise "en". Safe to call outside a request."""
    return "th" if has_request_context() and request.cookies.get("st_lang") == "th" else "en"


@dataclass(frozen=True)
class ReminderContext:
    customer_name: str
    invoice_number: str
    amount: str  # already formatted, e.g. "$1,250.00"
    due_date: str  # already formatted, e.g. "Oct 3, 2026"
    days_overdue: int
    stage_key: str  # friendly | second | firm | final
    business_name: str
    lang: str = "en"  # "en" or "th"; due_date should already be formatted for this language

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


# Thai versions. Gender-neutral polite wording (no ครับ/ค่ะ, no ผม/ดิฉัน; the business
# speaks as ทางเรา) so any owner can send them on LINE, SMS or email. Same facts and
# the same asks as the English templates; nothing is added.
TEMPLATES_TH = {
    "friendly": (
        "เรียน คุณ{name}\n\n"
        "ขออนุญาตแจ้งเตือนว่า ใบแจ้งหนี้เลขที่ {number} ยอดเงิน {amount} ครบกำหนดชำระเมื่อวันที่ {due} "
        "หากท่านชำระเรียบร้อยแล้ว ต้องขออภัยและโปรดละเว้นข้อความนี้\n\n"
        "ขอบคุณมาก\n{business}"
    ),
    "second": (
        "เรียน คุณ{name}\n\n"
        "ขอติดตามใบแจ้งหนี้เลขที่ {number} ยอดเงิน {amount} ซึ่งครบกำหนดชำระเมื่อวันที่ {due} "
        "รบกวนแจ้งให้ทราบว่าจะสะดวกชำระได้เมื่อใด\n\n"
        "ขอบคุณมาก\n{business}"
    ),
    "firm": (
        "เรียน คุณ{name}\n\n"
        "ใบแจ้งหนี้เลขที่ {number} ยอดเงิน {amount} เลยกำหนดชำระวันที่ {due} มาแล้ว {days} "
        "รบกวนดำเนินการชำระโดยเร็วที่สุด หรือหากใบแจ้งหนี้มีข้อผิดพลาดประการใด โปรดตอบกลับมาได้เลย\n\n"
        "ขอแสดงความนับถือ\n{business}"
    ),
    "final": (
        "เรียน คุณ{name}\n\n"
        "ขอแจ้งเตือนเป็นครั้งสุดท้ายเรื่องใบแจ้งหนี้เลขที่ {number} ยอดเงิน {amount} "
        "ซึ่งครบกำหนดชำระวันที่ {due} และขณะนี้เลยกำหนดมาแล้ว {days} "
        "รบกวนดำเนินการชำระหรือติดต่อทางเราภายในสัปดาห์นี้ เพื่อให้เรื่องนี้เรียบร้อยร่วมกัน\n\n"
        "ขอแสดงความนับถือ\n{business}"
    ),
}

MOCK_TEMPLATES_TH = {
    "friendly": (
        "สวัสดี คุณ{first}\n\n"
        "หวังว่าจะสบายดี ขอแจ้งเตือนสั้น ๆ ว่าใบแจ้งหนี้เลขที่ {number} ยอดเงิน {amount} "
        "ครบกำหนดชำระเมื่อวันที่ {due} หากสะดวกรบกวนช่วยตรวจสอบด้วย "
        "ถ้าชำระไว้แล้ว ขออภัยและโปรดละเว้นข้อความนี้\n\n"
        "ขอบคุณมาก\n{business}"
    ),
    "second": (
        "สวัสดี คุณ{first}\n\n"
        "ขอติดตามใบแจ้งหนี้เลขที่ {number} ยอดเงิน {amount} ซึ่งครบกำหนดชำระเมื่อวันที่ {due} "
        "ขณะนี้เลยกำหนดมาแล้ว {days} รบกวนแจ้งให้ทราบว่าจะสะดวกชำระได้ประมาณวันไหน\n\n"
        "ขอบคุณมาก\n{business}"
    ),
    "firm": (
        "สวัสดี คุณ{first}\n\n"
        "ใบแจ้งหนี้เลขที่ {number} ยอดเงิน {amount} ยังค้างชำระอยู่ และเลยกำหนดชำระวันที่ {due} มาแล้ว {days} "
        "รบกวนดำเนินการชำระโดยเร็วที่สุด หากมีรายการใดในใบแจ้งหนี้ที่ต้องตรวจสอบ "
        "ตอบกลับมาได้เลย ทางเรายินดีช่วยดูให้\n\n"
        "ขอแสดงความนับถือ\n{business}"
    ),
    "final": (
        "สวัสดี คุณ{first}\n\n"
        "ขอแจ้งเตือนเป็นครั้งสุดท้ายเรื่องใบแจ้งหนี้เลขที่ {number} ยอดเงิน {amount} "
        "ซึ่งครบกำหนดชำระวันที่ {due} (เลยมาแล้ว {days}) "
        "รบกวนดำเนินการชำระหรือติดต่อทางเราภายในสัปดาห์นี้ เพื่อให้เรื่องนี้เรียบร้อยร่วมกัน\n\n"
        "ขอแสดงความนับถือ\n{business}"
    ),
}


def _fill(template, ctx):
    text = _fill_raw(template, ctx)
    if ctx.lang == "th":   # "คุณ Maria" reads better than "คุณMaria" for names in English letters
        text = re.sub(r"คุณ(?=[A-Za-z])", "คุณ ", text)
    return text


def _fill_raw(template, ctx):
    return template.format(
        name=ctx.customer_name,
        first=ctx.first_name,
        number=ctx.invoice_number,
        amount=ctx.amount,
        due=ctx.due_date,
        days=thai_days(ctx.days_overdue) if ctx.lang == "th" else plural_days(ctx.days_overdue),
        business=ctx.business_name,
    )


def template_message(ctx):
    templates = TEMPLATES_TH if ctx.lang == "th" else TEMPLATES
    return _fill(templates.get(ctx.stage_key, templates["friendly"]), ctx)


def mock_message(ctx):
    """Stands in for the AI when AI_MOCK=1, so the app works with no API key."""
    templates = MOCK_TEMPLATES_TH if ctx.lang == "th" else MOCK_TEMPLATES
    return _fill(templates.get(ctx.stage_key, templates["friendly"]), ctx)


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
    # Thai equivalents, for drafts written in Thai.
    (r"ค่าปรับ|ปรับเงิน|เบี้ยปรับ", "late fee"),
    (r"ค่าธรรมเนียม|ค่าใช้จ่ายเพิ่ม|ค่าบริการเพิ่ม|เรียกเก็บเพิ่ม", "fee"),
    (r"ดอกเบี้ย", "interest"),
    (r"กฎหมาย|ทนาย|ศาล|ฟ้อง|ดำเนินคดี|คดีความ", "legal"),
    (r"ทวงหนี้|ทวงถามหนี้|ติดตามหนี้|ตามหนี้", "collections"),
    (r"เครดิตบูโร|ประวัติเครดิต|แบล็กลิสต์|แบล็คลิสต์", "credit"),
    (r"ส่วนลด|ลดราคา|ลดให้|ยกเว้นค่า", "discount"),
    (r"สัญญา|ข้อตกลง|เงื่อนไข|นโยบาย", "contract or policy"),
    (r"ได้รับ(การ)?ชำระ|ได้รับเงิน|ได้รับยอด|ขอบคุณ(ที่|สำหรับ)(การ)?ชำระ|ชำระครบแล้ว", "payment received claim"),
    (r"มิฉะนั้น|ไม่เช่นนั้น|ผลที่ตามมา|ระงับ|ยกเลิกบริการ|ข่มขู่|ขั้นเด็ดขาด", "threat"),
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

THAI_INSTRUCTION = """

Language: write the whole message in natural, polite Thai that a Thai customer would expect \
from a local service business, suitable for LINE, SMS or email. Use gender-neutral polite wording: \
no ครับ or ค่ะ, no ผม or ดิฉัน (the business refers to itself as ทางเรา). Address the customer as \
คุณ followed by their name. Copy the customer name, invoice number, amount (including its currency \
symbol) and due date exactly as given; do not translate, convert or reformat them. Every rule above \
still applies in Thai: never mention ค่าปรับ, ค่าธรรมเนียม, ดอกเบี้ย, ส่วนลด, สัญญา, ข้อตกลง, นโยบาย, \
กฎหมาย, ฟ้องร้อง, ศาล, ทวงหนี้ or เครดิตบูโร, never threaten, never add any deadline other than \
"this week" for the final stage, and never say or imply that a payment was received."""


def system_prompt(ctx):
    return SYSTEM_PROMPT + (THAI_INSTRUCTION if ctx.lang == "th" else "")


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
    if ctx.lang == "th":
        facts["language"] = "Thai"
    body = json.dumps({
        "model": config.get("AI_MODEL", "claude-haiku-5-5"),
        "max_tokens": 400,
        "system": system_prompt(ctx),
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
