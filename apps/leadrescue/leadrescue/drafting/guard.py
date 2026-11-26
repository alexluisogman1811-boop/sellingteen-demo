"""Content checks for message drafts.

``find_issues`` looks for claims a draft must never invent: prices, discounts,
dates/times/appointments, guarantees/warranties, company policies, links,
contact details, leftover template placeholders and signs of prompt-injection
leakage.

* Machine-generated text (template or AI) with any issue is **rejected**.
* Text the owner typed is allowed (they know their prices) but every issue is
  shown as a reminder to double-check before approval.
"""
import re
from dataclasses import dataclass

from .context import DraftContext


@dataclass(frozen=True)
class Issue:
    code: str
    message: str


_CHECKS = [
    ("price", "mentions a price or dollar amount",
     r"\$\s*\d|\b\d[\d,]*(\.\d+)?\s*(dollars|usd|bucks)\b|\b\d+\s*k\b"),
    ("discount", "mentions a discount, deal or free offer",
     r"\b\d+\s*%|\bpercent\b|\bdiscount|\bdeal\b|\bcoupon|\bpromo|\bsale\b|\bfor free\b|\bfree of charge\b|"
     r"\bfree (inspection|estimate|quote|roof|repair|upgrade)|\bno charge\b|\bspecial offer|\brebate"),
    ("guarantee", "mentions a guarantee, warranty or promise",
     r"\bguarantee|\bwarrant|\blifetime\b|\bpromise|\bensure you\b|\b100\s*%"),
    ("policy", "mentions a company policy",
     r"\bpolic(y|ies)\b|\bterms and conditions\b|\brefund"),
    ("date_time", "mentions a specific date, day or time",
     r"\b(mon|tues|wednes|thurs|fri|satur|sun)day\b|\btomorrow\b|\btonight\b|\bnext week\b|"
     r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b|"
     r"\b\d{1,2}[/-]\d{1,2}([/-]\d{2,4})?\b|\b\d{1,2}(:\d{2})?\s*(a\.?m\.?|p\.?m\.?)\b|\bat \d{1,2}(:\d{2})?\b"),
    ("appointment", "claims an appointment or visit is booked",
     r"\b(you are|you're|we are|we're|i am|i'm) (booked|scheduled|confirmed)|\bappointment (is|has been) "
     r"(set|booked|confirmed|scheduled)|\bsee you (on|at)\b|\bconfirmed for\b"),
    ("insurance", "makes a claim about insurance coverage",
     r"\binsurance (will|should|is going to) (cover|pay)|\bcovered by (your )?insurance|\bdeductible\b"),
    ("link", "contains a link",
     r"https?://|\bwww\.|\b[a-z0-9-]+\.(com|net|org|io|co|us)\b"),
    ("contact", "contains a phone number or email address",
     r"\b\d{3}[\s.\-)]*\d{3}[\s.\-]*\d{4}\b|[\w.+-]+@[\w-]+\.[\w.]+"),
    ("placeholder", "has an unfilled placeholder",
     r"\{[^}]*\}|\[[A-Z _]+\]|<[a-z_ ]+>|\bTODO\b|\bXXX\b"),
    ("injection", "contains instruction-like text (possible prompt injection)",
     r"\bignore (all |any )?(previous|prior|above)|\bas an ai\b|\blanguage model\b|\bsystem prompt\b|"
     r"\bdisregard\b|\bnew instructions?\b|</?(system|assistant|user)>"),
]
_COMPILED = [(code, msg, re.compile(rx, re.IGNORECASE)) for code, msg, rx in _CHECKS]

# The same claims written in Thai (Thai drafts, or Thai text an owner typed).
# These only match Thai script, so English text is checked exactly as before.
_CHECKS_TH = [
    ("price", r"\d[\d,]*(\.\d+)?\s*บาท|฿|ราคา\s*\d|ค่าบริการ\s*\d"),
    ("discount", r"ส่วนลด|ลดราคา|ลด\s*\d|โปรโมชั่น|โปรโมชัน|ฟรี|แถม|ไม่มีค่าใช้จ่าย|ไม่เสียค่าใช้จ่าย|ราคาพิเศษ|ข้อเสนอพิเศษ"),
    ("guarantee", r"รับประกัน|การันตี|รับรองว่า|สัญญาว่า|ตลอดชีพ|ไม่รั่วแน่นอน"),
    ("policy", r"นโยบาย|ข้อกำหนดและเงื่อนไข|คืนเงิน"),
    ("date_time", r"พรุ่งนี้|มะรืน|คืนนี้|สัปดาห์หน้า|อาทิตย์หน้า|เดือนหน้า|"
                  r"วัน(จันทร์|อังคาร|พุธ|พฤหัส|ศุกร์|เสาร์|อาทิตย์)|"
                  r"(มกรา|กุมภา|มีนา|เมษา|พฤษภา|มิถุนา|กรกฎา|สิงหา|กันยา|ตุลา|พฤศจิกา|ธันวา)|"
                  r"\d{1,2}\s*(โมง|นาฬิกา|น\.)|เวลา\s*\d"),
    ("appointment", r"(นัด|จอง)ไว้แล้ว|ยืนยันนัด|ยืนยันแล้ว|นัดหมายเรียบร้อย|เจอกันวัน|เจอกันพรุ่งนี้"),
    ("insurance", r"ประกัน(ภัย)?\s*(จะ)?(จ่าย|ครอบคลุม|คุ้มครอง)|เคลมประกันได้|ค่าเสียหายส่วนแรก"),
    ("urgency", r"ด่วน|รีบ|เท่านั้น|หมดเขต|คิวว่าง|คิวเต็ม|เหลือ(อีก)?\s*\d|โอกาสสุดท้าย"),
]
_MESSAGES = {code: msg for code, msg, _ in _CHECKS}
_MESSAGES["urgency"] = "pressures the customer with urgency or availability claims"
_COMPILED_TH = [(code, _MESSAGES[code], re.compile(rx)) for code, rx in _CHECKS_TH]
# A Thai greeting followed by a title means a name was used ("สวัสดีคุณ...", "เรียน คุณ...").
_THAI_NAMED_GREETING = re.compile(r"^(สวัสดี|เรียน)\s*(คุณ|พี่|น้อง)")

MAX_TEXT_LEN = 640       # ~4 SMS segments
MAX_EMAIL_LEN = 2000


def _mask_known_names(text: str, ctx: DraftContext | None) -> str:
    """Hide verified names before checking, so a business called e.g. "Lifetime
    Roofing" or a customer named "May" isn't mistaken for a forbidden claim."""
    if ctx is None:
        return text
    for known in (ctx.business_name, ctx.sender_name, ctx.greeting_name):
        if known:
            text = re.sub(re.escape(known), "NAME", text, flags=re.IGNORECASE)
    return text


def find_issues(text: str, ctx: DraftContext | None = None, channel: str = "text") -> list[Issue]:
    issues: list[Issue] = []
    body = text or ""
    if not body.strip():
        return [Issue("empty", "is empty")]
    checked = _mask_known_names(body, ctx)
    for code, msg, rx in _COMPILED:
        if rx.search(checked):
            issues.append(Issue(code, msg))
    if re.search("[฀-๿]", checked):
        found = {i.code for i in issues}
        for code, msg, rx in _COMPILED_TH:
            if code not in found and rx.search(checked):
                issues.append(Issue(code, msg))
    limit = MAX_TEXT_LEN if channel == "text" else MAX_EMAIL_LEN
    if len(body) > limit:
        issues.append(Issue("length", f"is longer than {limit} characters"))
    if ctx is not None and ctx.greeting_name is None:
        # A generator must not make up a customer name when none was supplied.
        first_line = body.strip().splitlines()[0]
        if re.match(r"^(?i:hi|hello|hey|dear)\s+(?!(?i:there|folks|all)\b)[A-Za-z]", first_line):
            issues.append(Issue("invented_name", "greets the customer by a name that isn't on file"))
        elif _THAI_NAMED_GREETING.match(first_line):
            issues.append(Issue("invented_name", "greets the customer by a name that isn't on file"))
    return issues


def describe(issues: list[Issue]) -> list[str]:
    return [f"This message {i.message}. Double-check it before approving." for i in issues]
