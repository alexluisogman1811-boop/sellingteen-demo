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
    limit = MAX_TEXT_LEN if channel == "text" else MAX_EMAIL_LEN
    if len(body) > limit:
        issues.append(Issue("length", f"is longer than {limit} characters"))
    if ctx is not None and ctx.greeting_name is None:
        # A generator must not make up a customer name when none was supplied.
        first_line = body.strip().splitlines()[0]
        if re.match(r"^(?i:hi|hello|hey|dear)\s+(?!(?i:there|folks|all)\b)[A-Za-z]", first_line):
            issues.append(Issue("invented_name", "greets the customer by a name that isn't on file"))
    return issues


def describe(issues: list[Issue]) -> list[str]:
    return [f"This message {i.message}. Double-check it before approving." for i in issues]
