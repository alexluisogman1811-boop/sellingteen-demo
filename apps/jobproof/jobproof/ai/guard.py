"""Post-generation safety net shared by every provider.

The prompt tells the model not to invent facts; this module enforces it mechanically:
any sentence or bullet that introduces a number, price, warranty/guarantee, policy or
other risky claim that does not appear in the user's own input is removed, and list
items must be traceable to words the user actually wrote.
"""
import re

SECTIONS = ["reported_issue", "findings", "work_completed", "materials", "testing", "customer_notes", "follow_up"]
MAX_ITEMS = 20
MAX_ITEM_LEN = 400
MAX_SUMMARY_LEN = 1500

# Terms the report must never introduce on its own.
RISKY_TERMS = [
    "warrant", "guarantee", "guaranteed", "$", "€", "£", "price", "cost", "charge", "fee", "invoice",
    "discount", "free of charge", "lifetime", "certified", "certification", "licensed", "insured",
    "code compliant", "up to code", "to code", "policy", "policies", "appointment", "a.m.", "p.m.",
    "o'clock", "percent", "%", "psi", "volts", "amps", "inches", "feet", "meters", "degrees",
    "next week", "next month", "tomorrow", "satisfied", "happy with", "thanked",
]

# Hedged language: a guess must never be presented as a fact.
HEDGE_RE = re.compile(
    r"\b(?:probably|maybe|possibly|perhaps|presumably|might|may have|may be|likely|unlikely|"
    r"i think|we think|i believe|we believe|i guess|guess|seems?|seemed|appears? to|appeared to|"
    r"not sure|unsure|unclear|assume[ds]?|could be|could have|supposedly|hopefully)\b",
    re.IGNORECASE,
)

STOPWORDS = {
    "the", "and", "for", "with", "was", "were", "that", "this", "from", "into", "onto", "under", "over",
    "all", "any", "are", "has", "had", "have", "not", "but", "then", "also", "there", "their", "they",
    "our", "you", "your", "its", "out", "off", "per", "via", "job", "work", "customer", "technician",
    "service", "completed", "performed", "visit", "report", "item", "items", "new",
}

_NUM_RE = re.compile(r"\d+(?:[.,:/]\d+)*")
_WORD_RE = re.compile(r"[a-z][a-z'\-]{2,}")


def _clean(text, limit):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    text = text.lstrip("-*•· ").strip()
    return text[:limit]


def normalise_content(raw):
    """Coerce provider output into the report schema (raises ValueError if unusable)."""
    if not isinstance(raw, dict):
        raise ValueError("provider output is not an object")
    content = {"summary": _clean(raw.get("summary", ""), MAX_SUMMARY_LEN)}
    for section in SECTIONS:
        items = raw.get(section) or []
        if isinstance(items, str):
            items = [items]
        if not isinstance(items, list):
            raise ValueError(f"section {section} is not a list")
        seen, cleaned = set(), []
        for item in items[:MAX_ITEMS]:
            text = _clean(item, MAX_ITEM_LEN)
            if text and text.lower() not in seen:
                seen.add(text.lower())
                cleaned.append(text)
        content[section] = cleaned
    return content


def _stem(word):
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _significant_words(text):
    return {_stem(w) for w in _WORD_RE.findall(text.lower()) if w not in STOPWORDS}


def introduces_unsupported_fact(text, source_lower, source_numbers):
    lower = text.lower()
    for term in RISKY_TERMS:
        if term in lower and term not in source_lower:
            return True
    for num in _NUM_RE.findall(text):
        if num not in source_numbers:
            return True
    return False


def enforce_grounding(content, source):
    source_lower = source.lower()
    source_numbers = set(_NUM_RE.findall(source))
    source_words = _significant_words(source)

    sentences = re.split(r"(?<=[.!?])\s+", content.get("summary", ""))
    kept = [s for s in sentences
            if s and not introduces_unsupported_fact(s, source_lower, source_numbers) and not HEDGE_RE.search(s)]
    result = {"summary": " ".join(kept).strip()}

    for section in SECTIONS:
        items = []
        for item in content.get(section, []):
            if introduces_unsupported_fact(item, source_lower, source_numbers):
                continue
            if section != "follow_up" and HEDGE_RE.search(item):
                continue  # recommendations may look ahead; reported facts may not be guesses
            if not (_significant_words(item) & source_words):
                continue  # nothing in this item comes from the user's input
            items.append(item)
        result[section] = items
    return result
