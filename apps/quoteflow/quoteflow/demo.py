"""Demo mode (AI_MOCK=1): a rule-based stand-in for the AI, for development and
testing without paid API calls.

It follows the same rules as the real AI's instructions, so the whole product
can be exercised realistically:
- prices only come from pricing.calculate_price (the same calculator),
- services are only those in the business's services list or add-on list,
- durations and policies are quoted word-for-word from the profile,
- availability is never promised,
- anything it can't answer from the profile is listed for the owner.

It reads simple phrasing well, but it is NOT a measure of real AI quality.
"""
import re

from .pricing import calculate_price, configured_addons, pricing_rules

DEMO_NOTICE = "Demo mode: this quote was written by the built-in simulator, not the real AI (AI_MOCK=1)."

_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                 "nine": 9, "ten": 10, "single": 1}

# Services customers commonly ask about that may not be part of a regular/deep clean.
# (pattern, name used in replies)
_OTHER_SERVICES = [
    (r"carpet", "carpet cleaning"),
    (r"window", "window cleaning"),
    (r"oven", "oven cleaning"),
    (r"fridge|refrigerator", "fridge cleaning"),
    (r"laundry|ironing", "laundry and ironing"),
    (r"pressure.?wash|power.?wash|jet.?wash|driveway|patio", "pressure washing"),
    (r"gutter", "gutter cleaning"),
    (r"upholster|sofa|couch", "upholstery cleaning"),
    (r"mattress", "mattress cleaning"),
    (r"move.?out|move.?in|end.of.tenancy|moving out", "move-in/move-out cleaning"),
    (r"office|commercial|warehouse", "commercial cleaning"),
    (r"biohazard|hoard", "specialist cleaning"),
]
_ADDON_FILLER = {"inside", "interior", "clean", "cleaning", "the", "of", "and", "extra", "deep", "full", "add", "on"}

_DEEP = r"\bdeep\b"
_BASIC = r"\b(basic|regular|standard|routine|weekly|fortnightly|biweekly|bi-weekly|monthly|maintenance)\b"
_GENERIC_CLEAN = (r"\b(a|the|my|our|your) (house |home |apartment |flat |place )?clean(ing)?\b(?! (the|my|our|of) )"
                  r"|\bclean(ing)? (of|for) (my|our|a|the)\b|\b(house|home)\s?clean")
_DAY = (r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|today|this week|next week|"
        r"weekend|\d{1,2}(st|nd|rd|th)\b)")


def _count(text, pattern):
    m = re.search(r"\b(\d+|" + "|".join(_NUMBER_WORDS) + r")[\s-]*(?:" + pattern + r")", text)
    if not m:
        return None
    v = m.group(1)
    return int(v) if v.isdigit() else _NUMBER_WORDS[v]


def _customer_name(original):
    text = original.strip()
    # Only a capitalised word in a sign-off, e.g. "Thanks, Priya" or "- Mark".
    for pattern in (r"(?:[Tt]hanks|[Tt]hank you|[Cc]heers|[Rr]egards|[Bb]est)[,!.]?\s+([A-Z][a-z]+)\s*[.!]?$",
                    r"[-–—]\s*([A-Z][a-z]+)\s*$"):
        m = re.search(pattern, text)
        if m:
            return m.group(1)
    return None


def _significant(words):
    return {w.rstrip("s") for w in re.findall(r"[a-z]+", words.lower()) if w not in _ADDON_FILLER and len(w) > 2}


def _requested_addons(text, addons):
    asked = _significant(text)
    return [a for a in addons if _significant(a["name"]) and _significant(a["name"]) <= asked]


def _service_line(services_text, service_type):
    pattern = r"\bdeep\b" if service_type == "deep" else r"\b(basic|standard|regular)\b"
    for line in services_text.splitlines():
        if re.search(pattern, line, re.IGNORECASE):
            return line.strip().lstrip("-•* ").strip()
    return None


def _line_name(line):
    return re.split(r"\s+[-–—]\s+|\s*\(|:", line, maxsplit=1)[0].strip()


def _duration(line):
    if not line:
        return ""
    m = re.search(r"((?:about|around|approx\.?|approximately)\s+)?\d+(?:\.\d+)?(?:\s*(?:-|–|to)\s*\d+(?:\.\d+)?)?"
                  r"\s*(?:hours?|hrs?|minutes?|mins?)\b.*$", line, re.IGNORECASE)
    if not m:
        return ""
    d = m.group(0).strip().rstrip(".")
    return d[0].upper() + d[1:]


def _join(items):
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _sentence(text, end="."):
    """One tidy line from the owner's own text (whitespace collapsed, trailing period handled)."""
    t = " ".join(text.split()).rstrip(" .")
    return t + end


def _lines(text):
    """Non-empty lines of an owner-written field, each as a sentence, word for word."""
    out = []
    for line in text.splitlines():
        line = line.strip().lstrip("-•* ").strip()
        if line:
            out.append(line if line.endswith((".", "!", "?")) else line + ".")
    return out


def _covers(rules):
    """What the starting price includes, from the owner's own room rules."""
    bed, bath = rules["bedrooms_included"], rules["bathrooms_included"]
    parts = [f"up to {n} {w}{'s' if n != 1 else ''}" for n, w in ((bed, "bedroom"), (bath, "bathroom")) if n]
    return "covers " + " and ".join(parts) if parts else ""


def simulate_quote(profile, customer_request):
    """Return the same structure the real AI path returns (before _finalize)."""
    from .ai import _finalize   # local import: ai imports this module

    original = customer_request
    t = original.lower()
    rules = pricing_rules(profile)
    services_text = profile.get("services", "")
    offered_text = (services_text + "\n" + "\n".join(a["name"] for a in rules["addons"])).lower()

    addons_requested = _requested_addons(t, rules["addons"])
    addon_words = set().union(*(_significant(a["name"]) for a in addons_requested)) if addons_requested else set()

    explicit = "deep" if re.search(_DEEP, t) else ("basic" if re.search(_BASIC, t) else None)
    not_offered, offered_unpriced, owner_notes = [], [], []
    for pattern, label in _OTHER_SERVICES:
        if not re.search(pattern, t):
            continue
        if any(re.search(pattern, w) for w in addon_words):
            continue                     # handled as a priced add-on
        if label == "move-in/move-out cleaning" and explicit and not re.search(pattern, offered_text):
            # "We're moving out and need a deep clean": context for the requested clean, not a separate service.
            owner_notes.append("Customer mentioned moving in/out — check your quoted clean covers what they need.")
            continue
        (offered_unpriced if re.search(pattern, offered_text) else not_offered).append(label)

    bedrooms = _count(t, r"bed(room)?s?\b|br\b")
    bathrooms = _count(t, r"bath(room)?s?\b|ba\b")
    generic = bool(re.search(_GENERIC_CLEAN, t))
    rooms_given = bedrooms is not None or bathrooms is not None
    other_requested = bool(not_offered or offered_unpriced)
    wants_clean = bool(explicit or generic or addons_requested or (rooms_given and not other_requested))
    service_type = explicit or "basic"

    calc = None
    if wants_clean:
        calc = calculate_price(profile, service_type, bedrooms, bathrooms, [a["name"] for a in addons_requested])

    business = profile.get("business_name") or "our team"
    name = _customer_name(original)
    line = _service_line(services_text, service_type) if wants_clean else None
    duration = _duration(line) if calc and calc["can_quote"] else ""
    missing = [DEMO_NOTICE] + owner_notes
    upsells = [a["name"] for a in rules["addons"] if a not in addons_requested][:2] if calc else []
    paragraphs = [f"Thank you for getting in touch with {business}!"]

    # The quote itself. Every amount comes from `calc` (the pricing calculator) or the owner's add-on list.
    if calc:
        job = f"{'deep' if service_type == 'deep' else 'regular'} clean"
        home = ""
        if bedrooms is not None and bathrooms is not None:
            home = f" of your {bedrooms}-bedroom, {bathrooms}-bathroom home"
        elif bedrooms is not None:
            home = f" of your {bedrooms}-bedroom home"
        extras = f", plus {_join([a['name'].lower() for a in addons_requested])}" if addons_requested else ""
        if not calc["can_quote"]:
            paragraphs.append(f"I'd be happy to help with a {job}{home}. I'll confirm the exact price for you "
                              "shortly.")
        elif calc["is_starting_price"]:
            # "How many bedrooms the property has." -> "how many bedrooms"
            asks = [a.lower().rstrip(".").replace(" the property has", "") for a in calc["ask_customer"]]
            covers = _covers(rules)
            text = (f"Our {job} starts from {calc['price_display'].replace('From ', '')}"
                    f"{' (' + covers + ')' if covers else ''}{extras}.")
            if asks:
                text += f" Could you let me know {_join(asks)} the property has? Then I can send you an exact price."
            paragraphs.append(text)
        elif len(calc["breakdown"]) > 1:
            items = "\n".join(f"• {b}" for b in calc["breakdown"])
            paragraphs.append(f"Here's your quote for a {job}{home}{extras}:\n\n{items}\n"
                              f"Total: {calc['price_display']}")
        else:
            paragraphs.append(f"For a {job}{home}{extras}, the price would be {calc['price_display']}.")
        if duration and not calc["is_starting_price"]:
            paragraphs.append(f"As a guide, it usually takes {duration[0].lower() + duration[1:]}.")
        if not explicit and profile.get("deep_price") and service_type == "basic":
            paragraphs.append("We also offer deep cleaning if you'd like a more thorough clean.")
        if calc["can_quote"] and upsells:
            paragraphs.append("Optional extras you can add: "
                              + _join([u[0].lower() + u[1:] for u in configured_addons(profile, upsells)]) + ".")

    if not_offered:
        verb = "isn't something we offer" if len(not_offered) == 1 else "aren't services we offer"
        sentence = f"Unfortunately, {_join(not_offered)} {verb}."
        ours = [_line_name(l.strip().lstrip("-•* ")) for l in services_text.splitlines()
                if l.strip() and not l.lower().lstrip("-•* ").startswith("add-on")][:3]
        if ours and not calc:
            sentence += f" We provide {_join([o.lower() for o in ours])} — I'd be happy to quote for those."
        paragraphs.append(sentence)
        for label in not_offered:
            missing.append(f"Customer asked about {label}, which isn't in your services list.")
    for label in offered_unpriced:
        paragraphs.append(f"We do offer {label} — I'll confirm the price for that separately.")
        missing.append(f"No price rule for {label} — add it as an add-on, or reply with a price yourself.")

    if not calc and not other_requested:
        paragraphs.append("To put together a quote, could you tell me what kind of clean you need "
                          "(regular or deep) and how many bedrooms and bathrooms the property has?")

    if calc and calc["can_quote"] and not duration:
        missing.append(f"No typical duration listed for {service_type} cleaning in your services.")

    # "Good to know": the owner's own words, quoted as they wrote them.
    if calc:
        notes = []
        if (profile.get("service_area") or "").strip():
            notes.append(f"Service area: {_sentence(profile['service_area'], end='')}")
        if (profile.get("opening_hours") or "").strip():
            notes.append(f"Hours: {_sentence(profile['opening_hours'], end='')}")
        notes += _lines(profile.get("starting_prices", "")) + _lines(profile.get("policies", ""))
        if notes:
            paragraphs.append("Good to know:\n" + "\n".join(f"• {n}" for n in notes[:6]))

    if re.search(_DAY, t):
        paragraphs.append("I'll check our schedule for the day you mentioned and confirm.")
    elif calc and calc["can_quote"] and not calc["is_starting_price"]:
        paragraphs.append("To book, just reply with a day and time that suit you and I'll confirm.")
    if calc and calc["can_quote"] and not calc["is_starting_price"]:
        paragraphs.append("If any details are different from what you described, let me know and I'll "
                          "update the quote.")
    contact = (profile.get("contact_info") or "").strip()
    if contact:
        paragraphs.append(f"Our contact details: {_sentence(contact, end='')}.")

    reply = f"Hi {name or 'there'},\n\n" + "\n\n".join(paragraphs) + f"\n\nKind regards,\n{business}"

    # Owner-facing summary.
    parts = []
    if calc:
        size = f" of a {bedrooms}-bed, {bathrooms}-bath home" if bedrooms is not None and bathrooms is not None else ""
        parts.append(f"a {service_type} clean{size}")
        if addons_requested:
            parts[-1] += " plus " + _join([a["name"].lower() for a in addons_requested])
    parts += not_offered + offered_unpriced
    summary = (f"{name or 'Customer'} wants " + _join(parts) + ".") if parts else \
        f"{name or 'Customer'} is asking about pricing but hasn't said what they need."

    included = []
    if calc and calc["can_quote"]:
        included.append(_line_name(line) if line else f"{service_type.title()} clean")
        included += [a["name"] for a in addons_requested]

    ai = {
        "request_summary": summary,
        "estimated_time": duration,
        "services_included": included,
        "suggested_add_ons": upsells,
        "customer_reply": reply,
        "missing_information": missing,
    }
    return _finalize(profile, ai, calc, original)
