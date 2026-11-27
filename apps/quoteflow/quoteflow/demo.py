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
from .thai_request import asks_for, english_hints

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


def _duration_text(line):
    """The duration part of a service line, exactly as the owner typed it."""
    if not line:
        return ""
    m = re.search(r"((?:about|around|approx\.?|approximately)\s+)?\d+(?:\.\d+)?(?:\s*(?:-|–|to)\s*\d+(?:\.\d+)?)?"
                  r"\s*(?:hours?|hrs?|minutes?|mins?)\b.*$", line, re.IGNORECASE)
    if not m:
        return ""
    return m.group(0).strip().rstrip(".")


def _duration(line):
    d = _duration_text(line)
    if not d:
        return ""
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


# --- Thai replies (owner chose Thai; see language.reply_language) ---------------------------------
# Only the template's own sentences are translated. Everything the owner typed (business name,
# service and add-on names, durations, policies, hours, area, contact) is quoted exactly as written,
# and every amount is the calculator's / format_money's own text.

_TH_JOB = {"deep": "ทำความสะอาดแบบละเอียด", "basic": "ทำความสะอาดทั่วไป"}
_TH_SERVICES = {
    "carpet cleaning": "ทำความสะอาดพรม",
    "window cleaning": "ทำความสะอาดหน้าต่าง",
    "oven cleaning": "ทำความสะอาดเตาอบ",
    "fridge cleaning": "ทำความสะอาดตู้เย็น",
    "laundry and ironing": "ซักรีดเสื้อผ้า",
    "pressure washing": "ฉีดล้างด้วยน้ำแรงดันสูง",
    "gutter cleaning": "ทำความสะอาดรางน้ำฝน",
    "upholstery cleaning": "ทำความสะอาดโซฟาและเฟอร์นิเจอร์บุผ้า",
    "mattress cleaning": "ทำความสะอาดที่นอน",
    "move-in/move-out cleaning": "ทำความสะอาดก่อนย้ายเข้าหรือหลังย้ายออก",
    "commercial cleaning": "ทำความสะอาดสำนักงานและสถานที่เชิงพาณิชย์",
    "specialist cleaning": "ทำความสะอาดเฉพาะทาง",
}
_TH_ROOM = {"bedroom": "ห้องนอน", "bathroom": "ห้องน้ำ"}
_DAY_TH = r"(วันจันทร์|วันอังคาร|วันพุธ|วันพฤหัส|วันศุกร์|วันเสาร์|วันอาทิตย์|พรุ่งนี้|วันนี้|มะรืน|สัปดาห์นี้|สัปดาห์หน้า|อาทิตย์หน้า|เสาร์อาทิตย์|สุดสัปดาห์)"


def _join_th(items):
    """'A, B และ C' (no space after และ before Thai text, one before Latin text)."""
    items = list(items)
    if len(items) == 1:
        return items[0]
    last = items[-1]
    sep = "" if re.match(r"[฀-๿]", last) else " "
    return ", ".join(items[:-1]) + " และ" + sep + last


def _covers_th(rules):
    bed, bath = rules["bedrooms_included"], rules["bathrooms_included"]
    parts = [f"ไม่เกิน {n} {w}" for n, w in ((bed, "ห้องนอน"), (bath, "ห้องน้ำ")) if n]
    return "ครอบคลุม" + " และ".join(parts) if parts else ""


def _ask_th(ask):
    """calculate_price's ask_customer item ("How many bedrooms the property has.") in Thai."""
    for room, th in _TH_ROOM.items():
        if room in ask.lower():
            return "จำนวน" + th
    return "รายละเอียดเพิ่มเติมของที่พัก"


def _breakdown_th(calc, rules, currency):
    """The calculator's breakdown lines with the template words in Thai. Amounts are copied from the
    calculator's own text; add-on lines are the owner's names and prices, unchanged."""
    from .pricing import format_money
    addon_lines = {f"{a['name']}: {format_money(a['price'], currency)}" for a in rules["addons"]}
    out = []
    for i, line in enumerate(calc["breakdown"]):
        m = re.match(r"^(Basic|Deep) cleaning(?: \(includes (.*)\))?: (.+)$", line) if i == 0 else None
        if m:
            incl = (m.group(2) or "").replace(" bed", " ห้องนอน").replace(" bath", " ห้องน้ำ")
            out.append(f"{_TH_JOB[m.group(1).lower()]}{' (รวม ' + incl + ')' if incl else ''}: {m.group(3)}")
            continue
        m = re.match(r"^(\d+) extra (bedroom|bathroom)s? × (.+): (.+)$", line) if line not in addon_lines else None
        if m:
            out.append(f"{_TH_ROOM[m.group(2)]}เพิ่ม {m.group(1)} ห้อง × {m.group(3)}: {m.group(4)}")
            continue
        out.append(line)
    return out


def _owner_text(text):
    """An owner-written field on one line, otherwise exactly as typed."""
    return " ".join(text.split())


def _owner_lines(text):
    """Non-empty lines of an owner-written field, exactly as typed (bullet markers removed)."""
    return [l for l in (line.strip().lstrip("-•* ").strip() for line in text.splitlines()) if l]


def _thai_reply(profile, rules, calc, *, name, service_type, explicit, bedrooms, bathrooms, addons_requested,
                upsells, line, not_offered, offered_unpriced, other_requested, services_text, t):
    """The customer reply in Thai. Mirrors the English reply in simulate_quote sentence for sentence."""
    from .language import address_th
    cur = profile.get("currency") or "USD"
    business = (profile.get("business_name") or "").strip()
    duration = _duration_text(line) if calc and calc["can_quote"] else ""
    paragraphs = [f"ขอบคุณที่ติดต่อ {business}" if business else "ขอบคุณที่ติดต่อเรา"]

    if calc:
        job = _TH_JOB[service_type]
        home = ""
        if bedrooms is not None and bathrooms is not None:
            home = f" สำหรับบ้านของคุณขนาด {bedrooms} ห้องนอน {bathrooms} ห้องน้ำ"
        elif bedrooms is not None:
            home = f" สำหรับบ้านของคุณขนาด {bedrooms} ห้องนอน"
        extras = f" พร้อม {_join_th([a['name'] for a in addons_requested])}" if addons_requested else ""
        if not calc["can_quote"]:
            paragraphs.append(f"เรายินดีให้บริการ{job}{home} และจะแจ้งราคาที่แน่นอนให้ทราบอีกครั้งโดยเร็ว")
        elif calc["is_starting_price"]:
            asks = [_ask_th(a) for a in calc["ask_customer"]]
            covers = _covers_th(rules)
            text = (f"ราคา{job}{extras} เริ่มต้นที่ {calc['price_display'].replace('From ', '')}"
                    f"{' (' + covers + ')' if covers else ''}")
            if asks:
                text += f" รบกวนแจ้ง{_join_th(asks)}ของที่พัก เพื่อที่เราจะได้แจ้งราคาที่แน่นอนให้ทราบ"
            paragraphs.append(text)
        elif len(calc["breakdown"]) > 1:
            items = "\n".join(f"• {b}" for b in _breakdown_th(calc, rules, cur))
            paragraphs.append(f"ใบเสนอราคา{job}{home}{extras} มีรายละเอียดดังนี้\n\n{items}\n"
                              f"รวมทั้งหมด: {calc['price_display']}")
        else:
            paragraphs.append(f"ค่าบริการ{job}{home}{extras} อยู่ที่ {calc['price_display']}")
        if duration and not calc["is_starting_price"]:
            paragraphs.append(f"ระยะเวลาโดยประมาณ: {duration}")
        if not explicit and profile.get("deep_price") and service_type == "basic":
            paragraphs.append("หากต้องการความสะอาดที่ละเอียดยิ่งขึ้น เรามีบริการทำความสะอาดแบบละเอียดด้วย")
        if calc["can_quote"] and upsells:
            paragraphs.append("บริการเสริมที่เพิ่มได้: " + _join_th(configured_addons(profile, upsells)))

    if not_offered:
        sentence = f"ขออภัย ขณะนี้เรายังไม่มีบริการ{_join_th([_TH_SERVICES[l] for l in not_offered])}"
        ours = [_line_name(l.strip().lstrip("-•* ")) for l in services_text.splitlines()
                if l.strip() and not l.lower().lstrip("-•* ").startswith("add-on")][:3]
        if ours and not calc:
            sentence += f" บริการที่เรามีคือ {_join_th(ours)} ยินดีเสนอราคาให้สำหรับบริการเหล่านี้"
        paragraphs.append(sentence)
    for label in offered_unpriced:
        paragraphs.append(f"เรามีบริการ{_TH_SERVICES[label]} และจะแจ้งราคาสำหรับบริการนี้ให้ทราบแยกต่างหาก")

    if not calc and not other_requested:
        paragraphs.append("เพื่อจัดทำใบเสนอราคา รบกวนแจ้งประเภทการทำความสะอาดที่ต้องการ (แบบทั่วไปหรือแบบละเอียด) "
                          "รวมถึงจำนวนห้องนอนและห้องน้ำของที่พัก")

    if calc:
        notes = []
        if (profile.get("service_area") or "").strip():
            notes.append(f"พื้นที่ให้บริการ: {_owner_text(profile['service_area'])}")
        if (profile.get("opening_hours") or "").strip():
            notes.append(f"เวลาทำการ: {_owner_text(profile['opening_hours'])}")
        notes += _owner_lines(profile.get("starting_prices", "")) + _owner_lines(profile.get("policies", ""))
        if notes:
            paragraphs.append("ข้อมูลเพิ่มเติม:\n" + "\n".join(f"• {n}" for n in notes[:6]))

    if re.search(_DAY, t) or re.search(_DAY_TH, t):
        paragraphs.append("เราจะตรวจสอบตารางงานสำหรับวันที่คุณแจ้งมา แล้วจะยืนยันกลับอีกครั้ง")
    elif calc and calc["can_quote"] and not calc["is_starting_price"]:
        paragraphs.append("หากต้องการจอง ตอบกลับพร้อมแจ้งวันและเวลาที่สะดวกได้เลย แล้วเราจะยืนยันกลับอีกครั้ง")
    if calc and calc["can_quote"] and not calc["is_starting_price"]:
        paragraphs.append("หากรายละเอียดใดไม่ตรงกับที่แจ้งไว้ โปรดแจ้งให้เราทราบ แล้วเราจะปรับใบเสนอราคาให้")
    contact = (profile.get("contact_info") or "").strip()
    if contact:
        paragraphs.append(f"ช่องทางติดต่อ: {_owner_text(contact)}")

    greeting = "สวัสดี " + address_th(name) if name else "สวัสดี"
    return greeting + "\n\n" + "\n\n".join(paragraphs) + "\n\nขอแสดงความนับถือ\n" + (business or "ทีมงาน")


def simulate_quote(profile, customer_request, lang="en"):
    """Return the same structure the real AI path returns (before _finalize).
    lang "th" writes the customer reply in Thai; everything else (and the price) is the same."""
    from .ai import _finalize   # local import: ai imports this module

    original = customer_request
    t = original.lower()
    hints = english_hints(original)          # Thai message: add English words this simulator knows
    if hints:
        t = t + " " + hints
    rules = pricing_rules(profile)
    services_text = profile.get("services", "")
    offered_text = (services_text + "\n" + "\n".join(a["name"] for a in rules["addons"])).lower()

    addons_requested = _requested_addons(t, rules["addons"])
    addons_requested += [a for a in rules["addons"] if a not in addons_requested and asks_for(original, a["name"])]
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
    if lang == "th":
        reply = _thai_reply(profile, rules, calc, name=name, service_type=service_type, explicit=explicit,
                            bedrooms=bedrooms, bathrooms=bathrooms, addons_requested=addons_requested,
                            upsells=upsells, line=line, not_offered=not_offered,
                            offered_unpriced=offered_unpriced, other_requested=other_requested,
                            services_text=services_text, t=t)

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
