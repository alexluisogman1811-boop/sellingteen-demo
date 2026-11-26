"""Pre-written follow-up wording for the free, offline template provider.

Every sentence below only uses the facts in DraftContext. No sentence states
a price, date, appointment, guarantee or policy. Sentences that ask the customer
a question are used instead of statements whenever a fact is unknown.

Thai wording (used when the owner chose Thai) is at the bottom: same facts,
same rules.
"""
import re

from .context import (
    STALENESS_RECENT,
    STALENESS_STALE,
    STALENESS_UNKNOWN,
    STALENESS_WEEK,
    DraftContext,
)

# status -> staleness -> core message. "{s}" is the fixed service phrase.
# "*" is the fallback for any staleness not listed.
CORE = {
    "new": {
        "*": "Thanks for reaching out about {s}. When would be a good time to talk about what you need?",
    },
    "contacted": {
        STALENESS_RECENT: "Thanks for talking with us about {s}. Would you like to set up a time for us to take a look?",
        STALENESS_WEEK: "I wanted to check back in about {s}. Are you still interested in having us take a look?",
        STALENESS_STALE: "It's been a while since we were last in touch about {s}. Are you still interested in having us take a look?",
        "*": "I wanted to follow up about {s}. Would you like to set up a time for us to take a look?",
    },
    "inspection_scheduled": {
        "*": "Just checking in about the inspection we have planned for {s}. If anything has changed or you have "
             "any questions, just let me know.",
    },
    "estimate_sent": {
        STALENESS_RECENT: "I wanted to make sure you received our estimate for {s}. Do you have any questions I can answer?",
        STALENESS_STALE: "It's been a while since we were last in touch about the estimate for {s}. Are you still "
                         "considering the project? If your plans have changed, that's no problem. Just let me know.",
        "*": "I'm following up on the estimate we sent for {s}. Do you have any questions, or is there anything "
             "you'd like us to go over again?",
    },
    "negotiating": {
        STALENESS_STALE: "Checking back in on our conversation about {s}. Is there anything else you need from us "
                         "to help you decide?",
        "*": "I wanted to follow up on our conversation about {s}. Is there anything else you need from us to "
             "help you decide?",
    },
    "won": {
        "*": "Thank you for choosing {us} for {s}. If you have any questions, just reply to this message.",
    },
    "lost": {
        "*": "Thanks again for considering {us} for {s}. If anything changes down the road, we'd be glad to help.",
    },
}

SUBJECTS = {
    "won": "Thank you",
    "lost": "Thanks for considering us",
}

GENERIC_CORE = "I wanted to follow up about {s}. Is there anything I can help with?"


def _intro(ctx: DraftContext) -> str:
    if ctx.sender_name and ctx.business_name:
        return f"This is {ctx.sender_name} with {ctx.business_name}."
    if ctx.business_name:
        return f"This is {ctx.business_name}."
    if ctx.sender_name:
        return f"This is {ctx.sender_name}."
    return ""


def render(ctx: DraftContext) -> tuple[str, str]:
    """Return (subject, body). Subject is empty for text messages."""
    if ctx.lang == "th":
        return render_th(ctx)
    by_status = CORE.get(ctx.status)
    if by_status is None:
        core = GENERIC_CORE
    else:
        core = by_status.get(ctx.staleness) or by_status["*"]
    us = ctx.business_name or "us"
    core = core.format(s=ctx.service.phrase, us=us)

    greeting = f"Hi {ctx.greeting_name}," if ctx.greeting_name else "Hi there,"
    intro = _intro(ctx)

    if ctx.channel == "email":
        lines = [greeting, "", " ".join(p for p in (intro, core) if p), "", "Thanks,"]
        sign = [p for p in (ctx.sender_name, ctx.business_name) if p]
        lines.extend(sign or ["Your roofing team"])
        subject = SUBJECTS.get(ctx.status) or f"Following up about {ctx.service.phrase}"
        return subject[0].upper() + subject[1:], "\n".join(lines)

    # Text messages run on from the greeting: "Hi Maria, this is Sam..."
    after_greeting = intro or core
    if not after_greeting.startswith(("I ", "I'")):
        after_greeting = after_greeting[0].lower() + after_greeting[1:]
    rest = [after_greeting, core] if intro else [after_greeting]
    body = " ".join([greeting] + rest)
    return "", body


# ----------------------------------------------------------------- Thai ---
# Same facts, same situations, same rules as the English wording above (no
# prices, dates, appointments, guarantees or policies). Polite but
# gender-neutral: no ครับ/ค่ะ, so it suits any owner. Customer, sender and
# business names are kept exactly as typed.

# Fixed Thai phrase per service key (services.py); the job type text is never copied.
SERVICE_TH = {
    "storm": "งานหลังคาที่เสียหายจากพายุ",
    "leak_repair": "งานซ่อมหลังคารั่ว",
    "inspection": "งานตรวจสภาพหลังคา",
    "gutters": "งานรางน้ำฝน",
    "component_repair": "งานส่วนประกอบหลังคา",
    "flat_roof": "งานหลังคาแบน",
    "metal": "งานหลังคาเมทัลชีท",
    "replacement": "งานเปลี่ยนหลังคาใหม่",
    "repair": "งานซ่อมหลังคา",
    "roofing": "งานหลังคา",
}
SERVICE_TH_GENERIC = "งานนี้"

CORE_TH = {
    "new": {
        "*": "ขอบคุณที่สอบถามเข้ามาเรื่อง{s} ไม่ทราบว่าสะดวกคุยรายละเอียดเรื่องที่ต้องการช่วงไหน",
    },
    "contacted": {
        STALENESS_RECENT: "ขอบคุณที่คุยกับเราเรื่อง{s} สนใจนัดเวลาให้เราเข้าไปดูหน้างานไหม",
        STALENESS_WEEK: "ขอติดตามเรื่อง{s}อีกครั้ง ยังสนใจให้เราเข้าไปดูหน้างานอยู่ไหม",
        STALENESS_STALE: "ไม่ได้คุยกันมาสักพักแล้วเรื่อง{s} ยังสนใจให้เราเข้าไปดูหน้างานอยู่ไหม",
        "*": "ขอติดตามเรื่อง{s} สนใจนัดเวลาให้เราเข้าไปดูหน้างานไหม",
    },
    "inspection_scheduled": {
        "*": "ขอติดตามเรื่องการเข้าตรวจหน้างานที่วางแผนไว้สำหรับ{s} หากมีอะไรเปลี่ยนแปลง "
             "หรือมีคำถามใดๆ แจ้งได้เลย",
    },
    "estimate_sent": {
        STALENESS_RECENT: "ขอเช็กว่าได้รับใบเสนอราคาสำหรับ{s}แล้วหรือยัง มีคำถามอะไรให้ช่วยตอบไหม",
        STALENESS_STALE: "ไม่ได้คุยกันมาสักพักแล้วเรื่องใบเสนอราคาสำหรับ{s} ยังพิจารณางานนี้อยู่ไหม "
                         "หากเปลี่ยนแผนแล้วก็ไม่เป็นไร แจ้งให้ทราบได้เลย",
        "*": "ขอติดตามใบเสนอราคาสำหรับ{s}ที่ส่งไป มีคำถามอะไร หรืออยากให้เราอธิบายส่วนไหนอีกครั้งไหม",
    },
    "negotiating": {
        STALENESS_STALE: "ขอติดต่อกลับมาอีกครั้งเรื่องที่คุยกันไว้เกี่ยวกับ{s} "
                         "ต้องการข้อมูลอะไรจากเราเพิ่มเติมเพื่อช่วยในการตัดสินใจไหม",
        "*": "ขอติดตามเรื่องที่คุยกันไว้เกี่ยวกับ{s} ต้องการข้อมูลอะไรจากเราเพิ่มเติมเพื่อช่วยในการตัดสินใจไหม",
    },
    "won": {
        "*": "ขอบคุณที่เลือกใช้บริการของ{us}สำหรับ{s} หากมีคำถามใดๆ ตอบกลับข้อความนี้ได้เลย",
    },
    "lost": {
        "*": "ขอบคุณอีกครั้งที่พิจารณาบริการของ{us}สำหรับ{s} หากวันไหนมีอะไรเปลี่ยนแปลง เรายินดีช่วยเหลือ",
    },
}

SUBJECTS_TH = {
    "won": "ขอบคุณ",
    "lost": "ขอบคุณที่พิจารณาบริการของเรา",
}
SUBJECT_TH_DEFAULT = "ติดตามเรื่อง{s}"
GENERIC_CORE_TH = "ขอติดตามเรื่อง{s} มีอะไรให้เราช่วยไหม"
SIGN_OFF_TH = "ขอบคุณ"
TEAM_TH = "ทีมช่างหลังคา"

# Romanised titles typed before a name, and the Thai word used instead.
_TITLES_TH = {"khun": "คุณ", "k": "คุณ", "p": "พี่", "phi": "พี่", "nong": "น้อง"}
_TITLES_FAMILY = {"mr", "mrs", "ms", "miss", "mx", "dr", "prof", "sir", "madam"}
# Titles typed in Thai script: keep them as typed, don't add another คุณ.
_THAI_SCRIPT_TITLES = ("คุณ", "พี่", "น้อง", "ป้า", "ลุง", "น้า", "เจ๊", "เฮีย", "ท่าน", "ดร.")


def _is_thai(ch: str) -> bool:
    return "฀" <= ch <= "๿"


def _j(*pieces: str | None) -> str:
    """Join pieces the way Thai is written: no space between two Thai words,
    a space next to Latin text ("คุณ" + "John" -> "คุณ John")."""
    out = ""
    for piece in pieces:
        if not piece:
            continue
        if out and not (_is_thai(out[-1]) and _is_thai(piece[0])):
            out += " "
        out += piece
    return out


def _fill(template: str, **values: str) -> str:
    """str.format for Thai templates: adds a space where an inserted value
    starts or ends with Latin text next to Thai, e.g. "ของ Summit Roofing สำหรับ"."""
    out = ""
    for i, part in enumerate(re.split(r"\{(\w+)\}", template)):
        if i % 2:
            value = values[part]
            if value and out and _is_thai(out[-1]) and not _is_thai(value[0]):
                out += " "
            out += value + ("\x00" if value and not _is_thai(value[-1]) else "")
        else:
            if out.endswith("\x00"):
                out = out[:-1] + (" " if part and _is_thai(part[0]) else "")
            out += part
    return out.replace("\x00", "")


def thai_name(name: str | None) -> str | None:
    """How to address the customer in Thai: คุณ + name, never inventing one.

    "สมชาย" -> "คุณสมชาย", "Maria" -> "คุณ Maria", "Khun Nok" -> "คุณ Nok",
    "P Nok" -> "พี่ Nok", "Mr Smith" -> "คุณ Smith", "คุณนก" stays as typed.
    A title on its own ("Khun") is not a name, so None is returned.
    """
    if not name:
        return None
    parts = name.split()
    if parts[0] in _THAI_SCRIPT_TITLES:          # "คุณ นก" -> "คุณนก"
        return " ".join([_j(parts[0], parts[1]), *parts[2:]]) if len(parts) > 1 else None
    if name.startswith(_THAI_SCRIPT_TITLES):     # "คุณนก": title already attached
        return name
    title = parts[0].lower().rstrip(".'")
    if title in _TITLES_TH or title in _TITLES_FAMILY:
        if len(parts) == 1:
            return None
        prefix, rest = _TITLES_TH.get(title, "คุณ"), parts[1:]
    else:
        prefix, rest = "คุณ", parts
    return " ".join([_j(prefix, rest[0]), *rest[1:]])


def _intro_th(ctx: DraftContext) -> str:
    # Names are set off by spaces so the customer's and sender's names never run together.
    if ctx.sender_name and ctx.business_name:
        return f"ขออนุญาตแนะนำตัว {ctx.sender_name} จาก {ctx.business_name}"
    if ctx.business_name:
        return f"ขออนุญาตติดต่อมาจาก {ctx.business_name}"
    if ctx.sender_name:
        return f"ขออนุญาตแนะนำตัว {ctx.sender_name}"
    return ""


def render_th(ctx: DraftContext) -> tuple[str, str]:
    """Thai (subject, body), built from exactly the same facts as the English version."""
    by_status = CORE_TH.get(ctx.status)
    if by_status is None:
        core = GENERIC_CORE_TH
    else:
        core = by_status.get(ctx.staleness) or by_status["*"]
    service = SERVICE_TH.get(ctx.service.key, SERVICE_TH_GENERIC) if ctx.service.supported else SERVICE_TH_GENERIC
    core = _fill(core, s=service, us=ctx.business_name or "เรา")
    greeting = _j("สวัสดี", thai_name(ctx.greeting_name))
    intro = _intro_th(ctx)

    if ctx.channel == "email":
        lines = [greeting, "", " ".join(p for p in (intro, core) if p), "", SIGN_OFF_TH]
        sign = [p for p in (ctx.sender_name, ctx.business_name) if p]
        lines.extend(sign or [TEAM_TH])
        subject = SUBJECTS_TH.get(ctx.status) or _fill(SUBJECT_TH_DEFAULT, s=service)
        return subject, "\n".join(lines)

    return "", " ".join(p for p in (greeting, intro, core) if p)


# Exposed for tests: every combination the template provider can produce.
ALL_STALENESS = (STALENESS_RECENT, STALENESS_WEEK, STALENESS_STALE, STALENESS_UNKNOWN)
