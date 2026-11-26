"""Deterministic pricing from the owner's saved rules.

The AI never does price math. It only tells us what the customer asked for
(service type, rooms, add-ons); this module turns that into a price using the
business's configured rules, and reports exactly what's missing when it can't.
"""
import re

MONEY_FIELDS = ["basic_price", "deep_price", "extra_bedroom_price", "extra_bathroom_price"]
COUNT_FIELDS = ["bedrooms_included", "bathrooms_included"]

SYMBOLS = {"USD": "$", "CAD": "$", "AUD": "$", "NZD": "$", "SGD": "$",
           "EUR": "€", "GBP": "£", "INR": "₹", "THB": "฿", "ZAR": "R"}

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def parse_money(value):
    """'$1,200.50' -> 1200.5; '' -> None; garbage -> ValueError."""
    s = (value or "").strip()
    if not s:
        return None
    s = re.sub(r"^(?:[$€£₹฿R]|[A-Z]{3})\s?", "", s)
    if not _NUMBER.fullmatch(s):
        raise ValueError(s)
    return float(s.replace(",", ""))


def parse_count(value):
    s = (value or "").strip()
    if not s:
        return None
    if not s.isdigit():
        raise ValueError(s)
    return int(s)


def parse_addons(text):
    """One add-on per line, 'Name: price' (also accepts 'Name - price' / 'Name $price').
    Returns (addons, bad_lines)."""
    addons, bad = [], []
    for line in (text or "").splitlines():
        line = line.strip().lstrip("-•* ").strip()
        if not line:
            continue
        m = re.match(r"^(.*?)[\s:\-–—+]*([$€£₹฿]?\s?\d[\d,]*(?:\.\d+)?)\s*$", line)
        name = m.group(1).strip(" :-–—+") if m else ""
        if not m or not name:
            bad.append(line)
            continue
        addons.append({"name": name, "price": parse_money(m.group(2))})
    return addons, bad


def fmt_number(n):
    return f"{n:,.0f}" if float(n).is_integer() else f"{n:,.2f}"


def format_money(amount, currency):
    sym = SYMBOLS.get(currency)
    return f"{sym}{fmt_number(amount)}" if sym else f"{fmt_number(amount)} {currency}"


def validate_and_normalize(data):
    """Validate pricing fields from the profile form. Mutates `data` to a
    normalized form and returns a list of error messages."""
    errors = []
    labels = {
        "basic_price": "Basic cleaning starting price",
        "deep_price": "Deep cleaning starting price",
        "extra_bedroom_price": "Extra bedroom price",
        "extra_bathroom_price": "Extra bathroom price",
        "bedrooms_included": "Bedrooms included",
        "bathrooms_included": "Bathrooms included",
    }
    for f in MONEY_FIELDS:
        try:
            v = parse_money(data.get(f))
            data[f] = "" if v is None else fmt_number(v).replace(",", "")
        except ValueError:
            errors.append(f"{labels[f]} must be a number, like 120 or 89.50.")
    for f in COUNT_FIELDS:
        try:
            v = parse_count(data.get(f))
            data[f] = "" if v is None else str(v)
        except ValueError:
            errors.append(f"{labels[f]} must be a whole number, like 2.")
    _, bad = parse_addons(data.get("addons"))
    for line in bad:
        errors.append(f'Add-on "{line}" needs a price, like "Inside oven: 45".')
    return errors


def pricing_rules(profile):
    """Parsed rules from a saved (already validated) profile."""
    rules = {f: parse_money(profile.get(f)) for f in MONEY_FIELDS}
    rules.update({f: parse_count(profile.get(f)) for f in COUNT_FIELDS})
    rules["addons"], _ = parse_addons(profile.get("addons"))
    return rules


def _match_addon(requested, addons):
    r = requested.strip().lower()
    for a in addons:
        n = a["name"].lower()
        if r == n or r in n or n in r:
            return a
    return None


def calculate_price(profile, service_type, bedrooms=None, bathrooms=None, add_ons=()):
    rules = pricing_rules(profile)
    cur = profile.get("currency") or "USD"
    out = {
        "service_type": service_type,
        "can_quote": False,
        "total": None,
        "price_display": "",
        "is_starting_price": False,
        "breakdown": [],
        "missing_for_owner": [],     # gaps in the business's own settings
        "ask_customer": [],          # details the customer still needs to give
    }
    label = {"basic": "Basic cleaning", "deep": "Deep cleaning"}.get(service_type)
    if label is None:
        out["missing_for_owner"].append(f'No pricing rule for "{service_type}" cleaning.')
        return out
    base = rules[f"{service_type}_price"]
    if base is None:
        out["missing_for_owner"].append(f"{label} starting price is not set in your pricing rules.")
        return out

    total = base
    incl = []
    if rules["bedrooms_included"] is not None:
        incl.append(f"{rules['bedrooms_included']} bed")
    if rules["bathrooms_included"] is not None:
        incl.append(f"{rules['bathrooms_included']} bath")
    out["breakdown"].append(f"{label}{' (includes ' + ', '.join(incl) + ')' if incl else ''}: {format_money(base, cur)}")
    starting = False

    for room, count in (("bedroom", bedrooms), ("bathroom", bathrooms)):
        included = rules[f"{room}s_included"]
        extra_price = rules[f"extra_{room}_price"]
        if count is None:
            out["ask_customer"].append(f"How many {room}s the property has.")
            starting = True
            continue
        if included is None:
            out["missing_for_owner"].append(f"Number of {room}s included in the base price is not set.")
            starting = True
            continue
        extra = max(0, count - included)
        if extra == 0:
            continue
        if extra_price is None:
            out["missing_for_owner"].append(f"Extra {room} price is not set ({extra} extra {room}{'s' if extra > 1 else ''} requested).")
            starting = True
            continue
        cost = extra * extra_price
        total += cost
        out["breakdown"].append(f"{extra} extra {room}{'s' if extra > 1 else ''} × {format_money(extra_price, cur)}: {format_money(cost, cur)}")

    for name in add_ons or ():
        a = _match_addon(name, rules["addons"])
        if a is None:
            out["missing_for_owner"].append(f'Customer asked for "{name}", which has no price in your add-on list.')
            starting = True
            continue
        total += a["price"]
        out["breakdown"].append(f"{a['name']}: {format_money(a['price'], cur)}")

    out.update(
        can_quote=True,
        total=total,
        is_starting_price=starting,
        price_display=("From " if starting else "") + format_money(total, cur),
    )
    return out


def configured_addons(profile, names, cur=None):
    """Map AI-suggested upsell names onto configured add-ons, formatted with price.
    Anything not in the owner's list is dropped."""
    rules = pricing_rules(profile)
    cur = cur or profile.get("currency") or "USD"
    seen, result = set(), []
    for n in names or ():
        a = _match_addon(n, rules["addons"])
        if a and a["name"] not in seen:
            seen.add(a["name"])
            result.append(f"{a['name']} (+{format_money(a['price'], cur)})")
    return result


_AMOUNT = re.compile(
    r"(?:[$€£₹฿]\s?|\b(?:USD|EUR|GBP|CAD|AUD|NZD|CHF|SEK|NOK|DKK|SGD|THB|ZAR|INR)\s?)(\d[\d,]*(?:\.\d+)?)"
    r"|(\d[\d,]*(?:\.\d+)?)\s?(?:USD|EUR|GBP|CAD|AUD|NZD|CHF|SEK|NOK|DKK|SGD|THB|ZAR|INR|kr)\b"
)


_CODES = r"USD|EUR|GBP|CAD|AUD|NZD|CHF|SEK|NOK|DKK|SGD|THB|ZAR|INR"
_THAI_CHAR = "฀-๿"
_ANY_NUM = r"[\d๐-๙][\d๐-๙,]*(?:\.[\d๐-๙]+)?"
# Amounts that only appear in Thai text and that _AMOUNT misses: Thai digits after a currency symbol,
# "<number> บาท", and currency codes written right next to Thai letters (no word boundary there).
# Each alternative needs Thai characters, so English replies are checked exactly as before.
_AMOUNT_TH = re.compile(
    r"[$€£₹฿]\s?([๐-๙][๐-๙,]*(?:\.[๐-๙]+)?)"
    rf"|({_ANY_NUM})\s?บาท"
    rf"|(?<=[{_THAI_CHAR}])(?:{_CODES})\s?({_ANY_NUM})"
    rf"|({_ANY_NUM})\s?(?:{_CODES}|kr)(?=[{_THAI_CHAR}])"
)
_THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")


def _amounts(reply):
    """(number, matched text) for every money amount in the reply, English or Thai."""
    found, spans = [], []
    for m in _AMOUNT.finditer(reply or ""):
        found.append((float((m.group(1) or m.group(2)).replace(",", "")), m.group(0).strip()))
        spans.append(m.span())
    for m in _AMOUNT_TH.finditer(reply or ""):
        if any(m.start() < e and s < m.end() for s, e in spans):
            continue                      # already found by _AMOUNT
        num = next(g for g in m.groups() if g)
        found.append((float(num.translate(_THAI_DIGITS).replace(",", "")), m.group(0).strip()))
    return found


def unsupported_amounts(reply, profile, calcs=None):
    """Money amounts in the reply that don't appear anywhere in the owner's
    saved information or in our own calculations. Used as a safety net."""
    if isinstance(calcs, dict):
        calcs = [calcs]
    allowed = set()
    sources = list(profile.values())
    for calc in calcs or ():
        sources += calc["breakdown"]
        allowed.add(float(calc["total"] or 0))
    for v in sources:
        for n in _NUMBER.findall(str(v or "").translate(_THAI_DIGITS)):
            allowed.add(float(n.replace(",", "")))
    bad = []
    for n, text in _amounts(reply):
        if n not in allowed:
            bad.append(text)
    return bad
