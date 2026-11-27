"""Lets the free built-in quote writer (demo.py) understand customer messages written in Thai.

It does NOT translate the message. It only adds plain English hint words that demo.py already
knows, e.g. "คอนโด 2 ห้องนอน 1 ห้องน้ำ ทำความสะอาดใหญ่" adds " 2 bedrooms 1 bathrooms deep clean".
Messages without Thai letters get no hints, so English quotes stay exactly as before.
"""
import re

_THAI_LETTER = re.compile("[฀-๿]")
_THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")
# Longest first, so "สิบ" is not read as part of something shorter.
_NUM_WORDS = {"หนึ่ง": 1, "สอง": 2, "สาม": 3, "สี่": 4, "ห้า": 5, "หก": 6, "เจ็ด": 7, "แปด": 8, "เก้า": 9, "สิบ": 10}
_NUM = r"(\d+|" + "|".join(sorted(_NUM_WORDS, key=len, reverse=True)) + r")"


def _n(v):
    return int(v) if v.isdigit() else _NUM_WORDS[v]


# (Thai pattern, English hint words demo.py understands)
_WORDS = [
    (r"ทำความสะอาดใหญ่|ทำความสะอาดแบบละเอียด|ทำความสะอาดอย่างละเอียด|แบบละเอียด|ล้างใหญ่|บิ๊กคลีน|ดีพคลีน", "deep clean"),
    (r"ประจำ|ทุกสัปดาห์|ทุกอาทิตย์|รายสัปดาห์|รายเดือน|ทุกเดือน|ทั่วไป|ปกติ|พื้นฐาน", "regular clean"),
    (r"ทำความสะอาด|แม่บ้าน|คลีนนิ่ง", "a house clean"),
    (r"พรม", "carpet"),
    (r"หน้าต่าง|กระจก", "window"),
    (r"เตาอบ", "oven"),
    (r"ตู้เย็น", "fridge"),
    (r"ซักผ้า|รีดผ้า|ซักรีด", "laundry"),
    (r"ฉีดน้ำแรงดัน|ล้างลานจอดรถ|ล้างพื้นลาน", "pressure wash"),
    (r"รางน้ำ", "gutter"),
    (r"โซฟา", "sofa"),
    (r"ที่นอน", "mattress"),
    (r"ย้ายออก|ย้ายเข้า|ย้ายบ้าน|ย้ายห้อง", "move-out"),
    (r"ออฟฟิศ|สำนักงาน|โกดัง", "office"),
]


def has_thai(text):
    return bool(_THAI_LETTER.search(text or ""))


def english_hints(text):
    """Extra English words for a Thai message ('' when the message has no Thai letters)."""
    if not has_thai(text):
        return ""
    t = (text or "").translate(_THAI_DIGITS)
    hints = []
    for room, word in (("ห้องนอน|นอน", "bedrooms"), ("ห้องน้ำ|น้ำ", "bathrooms")):
        # "2 ห้องนอน", "สองห้องนอน", "2 นอน" (as in "2 นอน 1 น้ำ"), or "ห้องนอน 2 ห้อง"
        m = (re.search(_NUM + r"\s*(?:" + room + ")", t)
             or re.search(r"(?:" + room.split("|")[0] + r")\s*" + _NUM, t))
        if m:
            hints.append(f"{_n(m.group(1))} {word}")
    for pattern, words in _WORDS:
        if re.search(pattern, t):
            hints.append(words)
    return " ".join(hints)


def asks_for(text, name):
    """True if a Thai add-on name the owner wrote (e.g. 'ล้างตู้เย็น') appears in the message."""
    name = (name or "").strip()
    return has_thai(name) and name in (text or "")
