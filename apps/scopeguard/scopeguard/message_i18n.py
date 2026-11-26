"""Thai versions of the built-in message templates.

The hosting layer lets the account owner pick Thai with the cookie ``st_lang=th``. Page text is
translated by that layer; the *message* text a freelancer copies to a client is produced here.

Rules (kept deliberately strict):
  * Stored templates are never rewritten. New accounts are still seeded with the English defaults.
  * At render time, a stored template is shown in Thai only if its subject AND body still exactly
    match one of the English built-ins (line endings ignored). Anything the user wrote or edited is
    their own text and is rendered exactly as stored, in any language setting.
  * English rendering is unchanged.
  * Every placeholder in the English built-in appears in its Thai version, so the same real project
    details fill in. Thai text adds no deadlines, prices, legal claims or consequences.
"""
import re

from flask import has_request_context, request

from .db import DEFAULT_TEMPLATES

LANG_COOKIE = "st_lang"


def message_lang():
    """'th' when the owner chose Thai (cookie st_lang=th) in the current request, else 'en'.

    Safe outside a request (CLI, tests, background work): returns 'en'.
    """
    if has_request_context() and request.cookies.get(LANG_COOKIE) == "th":
        return "th"
    return "en"


# Same order as db.DEFAULT_TEMPLATES. Polite, gender-neutral business Thai: no ครับ/ค่ะ and no
# gendered first-person pronouns (the sentences simply omit the subject, which is natural in Thai).
THAI_TEMPLATES = [
    (
        "Scope review request",
        "โปรดตรวจสอบขอบเขตงานของ {project_name}",
        "เรียน คุณ{client_name}\n\n"
        "ได้จัดทำเอกสารขอบเขตงานที่ตกลงกันไว้สำหรับ {project_name} (เวอร์ชัน {scope_version}) เรียบร้อยแล้ว\n"
        "รบกวนตรวจสอบงานที่ส่งมอบ รายการที่ไม่รวมอยู่ในขอบเขตงาน และกำหนดการสำคัญได้ที่ลิงก์นี้:\n{review_link}\n\n"
        "หากทุกอย่างถูกต้อง สามารถกดอนุมัติได้ในหน้านั้น หรือหากมีส่วนใดที่ต้องปรับ "
        "ก็สามารถขอแก้ไขได้เช่นกัน\n\nขอบคุณ\n{freelancer_name}",
    ),
    (
        "Change request proposal",
        "คำขอเปลี่ยนแปลงงานสำหรับ {project_name}: {change_title}",
        "เรียน คุณ{client_name}\n\n"
        "รายการที่ขอเพิ่มเติม: {change_title}\n"
        "รายการนี้อยู่นอกขอบเขตงานที่ตกลงกันไว้\n"
        "เหตุผล: {change_reason}\n"
        "เวลาที่ต้องใช้เพิ่ม (ชั่วโมง): {change_hours}\n"
        "ผลกระทบด้านราคา: {change_cost}\n\n"
        "สามารถอนุมัติหรือปฏิเสธคำขอนี้ได้ที่ลิงก์นี้:\n{review_link}\n\n"
        "จะเริ่มดำเนินการเมื่อคำขอนี้ได้รับการอนุมัติแล้ว\n\nขอบคุณ\n{freelancer_name}",
    ),
    (
        "Revision allowance reached",
        "เรื่องการแก้ไขงานของ {project_name}",
        "เรียน คุณ{client_name}\n\n"
        "ขอแจ้งให้ทราบว่า จำนวนรอบการแก้ไขที่ตกลงกันไว้สำหรับ {project_name} "
        "({revision_allowance} รอบต่องานที่ส่งมอบแต่ละรายการ) ได้ใช้ครบแล้วสำหรับงานอย่างน้อยหนึ่งรายการ\n"
        "ยินดีดำเนินการต่อ โดยการแก้ไขรอบถัดไปจะจัดทำเป็นคำขอเปลี่ยนแปลงงาน "
        "เพื่อให้ทั้งสองฝ่ายมีบันทึกที่ชัดเจน\n\nขอบคุณ\n{freelancer_name}",
    ),
    (
        "Thanks for approving",
        "ขอบเขตงานของ {project_name} ได้รับการอนุมัติแล้ว",
        "เรียน คุณ{client_name}\n\n"
        "ขอบคุณที่อนุมัติขอบเขตงานเวอร์ชัน {scope_version} ของ {project_name} "
        "ได้บันทึกการอนุมัตินี้ไว้แล้ว และจะคอยแจ้งความคืบหน้าให้ทราบ\n\n"
        "ขอแสดงความนับถือ\n{freelancer_name}",
    ),
]

# Filler values message_context() uses when a detail is missing. In a Thai built-in message they are
# shown in Thai; real values (names, numbers, amounts, links) are never altered.
THAI_FALLBACKS = {
    "scope_version": ("(not yet published)", "(ยังไม่ได้เผยแพร่)"),
    "review_link": ("[create a client review link and paste it here]", "[สร้างลิงก์ให้ลูกค้าตรวจสอบ แล้ววางที่นี่]"),
    "deadline": ("(no deadline set)", "(ยังไม่ได้กำหนดวันส่งงาน)"),
    "change_reason": ("(not recorded)", "(ไม่ได้บันทึกไว้)"),
    "change_hours": ("not estimated", "ยังไม่ได้ประเมิน"),
    "change_cost": ("no cost proposed", "ยังไม่ได้เสนอราคา"),
}


def _norm(text):
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


_THAI_BY_ENGLISH = {
    (_norm(en[1]), _norm(en[2])): (th[1], th[2])
    for en, th in zip(DEFAULT_TEMPLATES, THAI_TEMPLATES)
}


def builtin_thai(subject, body):
    """(thai_subject, thai_body) if subject/body are still an unedited English built-in, else None."""
    return _THAI_BY_ENGLISH.get((_norm(subject), _norm(body)))


_LATIN_START = re.compile(r"[A-Za-z0-9]")


def _thai_context(context):
    ctx = dict(context)
    for key, (english, thai) in THAI_FALLBACKS.items():
        if ctx.get(key) == english:
            ctx[key] = thai
    return ctx


def localized_message(template, context, render, lang=None):
    """Render a stored template row as (subject, body) in the owner's chosen message language.

    ``render`` is services.render_text. English, or any template that is not an unedited built-in,
    renders exactly as before.
    """
    lang = lang or message_lang()
    subject, body = template["subject"], template["body"]
    thai = builtin_thai(subject, body) if lang == "th" else None
    if thai is None:
        return render(subject, context), render(body, context)
    th_subject, th_body = thai
    name = str(context.get("client_name") or "")
    if _LATIN_START.match(name):
        # Thai writes คุณสมชาย but คุณ Maya: put a space before a Latin-letter name.
        th_body = th_body.replace("คุณ{client_name}", "คุณ {client_name}")
    ctx = _thai_context(context)
    return render(th_subject, ctx), render(th_body, ctx)
