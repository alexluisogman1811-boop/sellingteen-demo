"""Review-request message generation.

Mock mode (AI_MOCK=1, or no API key configured) builds messages locally from
the customer's real fields only. Live mode calls the Anthropic API; its output
is checked so it cannot add contact details or links we never gave it.
"""
import json
import os
import random
import re
import urllib.error
import urllib.request
from datetime import date

ALLOWED_PLACEHOLDERS = ("customer_name", "first_name", "business_name", "service", "job_date", "review_link")
PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")
MAX_MESSAGE_LENGTH = 600

URL_RE = re.compile(r"(?:https?://|www\.)[^\s]+", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{6,}\d")
# Promises the business never made: discounts, prices, guarantees, policies.
OFFER_RE = re.compile(
    r"\d+\s?%|\$\s?\d[\d,.]*|\b(?:discounts?|coupons?|promo(?:tion)?s?|vouchers?|gift\s?cards?|"
    r"guarantee[ds]?|warrant(?:y|ies)|refunds?|rebates?|free of charge|for free|policy|policies)\b",
    re.I,
)


DEFAULT_MODEL = "claude-haiku-5-5"  # low-cost model


class AIError(Exception):
    """Raised with a user-safe message when a message cannot be generated."""


def use_mock(config):
    flag = config.get("AI_MOCK")
    if flag is not None and str(flag).strip() != "":
        return str(flag).strip() == "1"
    return not _api_key(config)


def _api_key(config):
    # Read once in create_app (the hosting package removes env vars afterwards); env is a fallback.
    return (config.get("ANTHROPIC_API_KEY") if hasattr(config, "get") else None)         or os.environ.get("ANTHROPIC_API_KEY")


def _format_date(iso):
    try:
        d = date.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    return f"{d.strftime('%B')} {d.day}"


def _service_phrase(service):
    # Sentence case reads naturally mid-sentence: "Panel upgrade" -> "panel upgrade".
    # Anything else ("EV charger install", "Tesla Powerwall install") is kept exactly as typed.
    words = service.split()
    if not words:
        return ""
    first, rest = words[0], words[1:]
    if (len(first) > 1 and first[0].isupper() and first[1:].islower()
            and all(w == w.lower() for w in rest)):
        words[0] = first[0].lower() + first[1:]
    return " ".join(words)


HONORIFICS = {"mr", "mrs", "ms", "miss", "mx", "dr", "prof", "rev", "sir"}


def _greeting_name(name):
    # "Dr. Patel" / "Mr. & Mrs. Jones" are greeted by their full name, not "Dr." or "Mr.".
    first = name.split()[0]
    if first.rstrip(".").lower() in HONORIFICS or first.lower() == "the":  # "The Rivera family"
        return name
    return first


def build_context(business, customer):
    name = " ".join(customer["name"].split())
    if name.islower():
        name = name.title()  # "jane doe" -> "Jane Doe"; mixed case is left as the owner typed it
    service = _service_phrase(customer["service"] or "")
    missing = [label for label, value in (("name", name), ("service/job", service)) if not value]
    if missing:
        raise AIError(f"This customer has no {' or '.join(missing)} saved. "
                      "Edit the customer and add it before writing a message.")
    return {
        "customer_name": name,
        "first_name": _greeting_name(name),
        "business_name": business["name"].strip(),
        "service": service,
        "job_date": _format_date(customer["job_date"]),
        # Empty when the business hasn't set one: messages then ask without a link, never a made-up one.
        "review_link": (business["review_link"] or "").strip(),
    }


def unknown_placeholders(body):
    return sorted({p for p in PLACEHOLDER_RE.findall(body) if p not in ALLOWED_PLACEHOLDERS})


def fill_template(body, ctx):
    return PLACEHOLDER_RE.sub(lambda m: ctx.get(m.group(1), m.group(0)), body).strip()


def find_invented_details(text, ctx, raw_values=()):
    """Return links, emails, phone numbers or offers in `text` that we never supplied."""
    values = [v for v in list(ctx.values()) + list(raw_values) if v]
    known = " ".join(values).lower()
    known_digits = [re.sub(r"\D", "", v) for v in values]
    found = [m.group(0) for m in OFFER_RE.finditer(text) if m.group(0).lower() not in known]
    for m in URL_RE.findall(text) + EMAIL_RE.findall(text):
        token = m.rstrip(".,!?;:)'\"").lower()
        if token not in known:
            found.append(token)
    for m in PHONE_RE.findall(text):
        digits = re.sub(r"\D", "", m)
        if not any(digits and digits in d for d in known_digits):
            found.append(m.strip())
    return found


# Mock generation ----------------------------------------------------------

_OPENERS = [
    "Hi {first_name}!",
    "Hi {first_name}, it's {business_name}.",
    "Hello {first_name}!",
]
_THANKS = [
    "Thanks again for choosing {business_name} for your {service}.",
    "Thanks again for choosing us for your {service}.",
    "Thank you for trusting us with your {service}.",
    "We really appreciated the chance to help with your {service}.",
]
_THANKS_DATED = [
    "Thanks for having us out on {job_date} for your {service}.",
    "Thank you for choosing {business_name} for your {service} on {job_date}.",
]
_ASKS = [
    "If you're happy with the work, would you mind leaving us a quick review? {review_link}",
    "If you have a minute, a short review would really help other customers find us: {review_link}",
    "Would you be willing to share a few words about your experience? {review_link}",
]
# Used when the business has no review link yet.
_ASKS_NO_LINK = [
    "If you were happy with the service, we'd really appreciate you sharing your experience in a review.",
    "If you have a minute, a short online review would really help other customers find us.",
    "Would you be willing to share a few words about your experience in a review?",
]
_CLOSERS = [
    "It makes a big difference for a small local business. Thank you!",
    "We appreciate your support!",
    "Thanks for supporting our business!",
    "Thanks so much, {business_name}",
    "",
]


def check_template_usable(template_body, ctx):
    if template_body and "{review_link}" in template_body and not ctx["review_link"]:
        raise AIError("This template includes your review link, but you haven't added one yet. "
                      "Add it on the Account page, or choose “Write it for me”.")


def tidy(text):
    return re.sub(r"\s+", " ", text).strip()


def mock_generate(ctx, template_body=None, seed=None):
    if template_body:
        check_template_usable(template_body, ctx)
        return fill_template(template_body, ctx)
    rng = random.Random(seed)
    opener = rng.choice(_OPENERS)
    if "{business_name}" in opener:
        thanks_pool = [t for t in _THANKS + _THANKS_DATED if "{business_name}" not in t]
    else:
        thanks_pool = list(_THANKS) + _THANKS_DATED
    if not ctx.get("job_date"):
        thanks_pool = [t for t in thanks_pool if "{job_date}" not in t]
    closer = rng.choice(_CLOSERS)
    if "{business_name}" in closer and "{business_name}" in opener:
        closer = "We appreciate your support!"
    asks = _ASKS if ctx.get("review_link") else _ASKS_NO_LINK
    parts = [opener, rng.choice(thanks_pool), rng.choice(asks), closer]
    return tidy(fill_template(" ".join(p for p in parts if p), ctx))


# Live generation ------------------------------------------------------------

def _live_generate(ctx, model, template_body=None, api_key=None):
    if not api_key:
        raise AIError("AI is not configured. Set AI_MOCK=1 or add ANTHROPIC_API_KEY to your environment.")
    check_template_usable(template_body, ctx)
    facts = {k: v for k, v in ctx.items() if v and k != "review_link"}
    link_rule = ("- Write the literal token {review_link} where the review link should go.\n"
                 if ctx["review_link"] else "- Do not include any link or placeholder.\n")
    prompt = (
        "Write one short, warm text message (under 320 characters) from a local service business "
        "asking a customer to leave an online review.\n"
        "Rules:\n"
        "- Use ONLY the facts below. Do not invent names, dates, prices, discounts, offers, guarantees, "
        "warranties, company policies, promises about results, details about the job, phone numbers, "
        "emails or links.\n"
        + link_rule +
        "- Plain text only, no subject line, no quotes, no other placeholders.\n"
        f"Facts: {json.dumps(facts)}\n"
    )
    if template_body:
        prompt += f"Match the tone and structure of this example:\n{template_body}\n"
    payload = json.dumps({
        "model": model,
        "max_tokens": 300,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise AIError("The AI service could not be reached. Please try again, or use mock mode.") from exc
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
    if not text:
        raise AIError("The AI service returned an empty message. Please try again.")
    return tidy(fill_template(text, ctx))


def generate_review_request(config, business, customer, template_body=None, seed=None):
    """Return (message, mode). Raises AIError with a user-safe message on failure."""
    ctx = build_context(business, customer)
    if use_mock(config):
        message, mode = mock_generate(ctx, template_body, seed), "mock"
    else:
        message, mode = _live_generate(ctx, config.get("AI_MODEL") or DEFAULT_MODEL, template_body,
                                       api_key=_api_key(config)), "live"

    message = message.strip()
    # Templates are written by the business itself, so only model output is checked.
    if mode == "live":
        # Never truncate: cutting the text could cut off the review link.
        if len(message) > MAX_MESSAGE_LENGTH:
            raise AIError("The AI wrote a message that was too long. Please try again.")
        raw = (customer["phone"], customer["email"], customer["job_date"], business["review_link"],
               template_body or "")
        if find_invented_details(message, ctx, raw):
            raise AIError("The AI added details you didn't provide (like an offer or a phone number), "
                          "so we didn't use it. Please try again or pick a template.")
    return message, mode
