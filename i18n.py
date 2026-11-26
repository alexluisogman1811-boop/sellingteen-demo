"""Thai language for the apps, done once in the hosting layer.

- A small "ไทย | EN" switch appears on every page of an app that has a Thai word list (i18n/<app>.json).
- The choice is remembered in a cookie (st_lang) for that app.
- When Thai is chosen, the page's English text is swapped for Thai just before it is sent:
  visible text, button labels, placeholders and titles. What customers TYPE is never touched
  (text boxes, scripts and styles are skipped), and anything without a translation stays in English.
- Word lists: i18n/common.json (shared words) + i18n/<app>.json. "{}" stands for a changing value,
  e.g. "{} leads due today" -> "วันนี้ต้องติดตาม {} ราย".
"""
import html
import json
import re
from pathlib import Path
from urllib.parse import quote

from flask import redirect, request

HERE = Path(__file__).resolve().parent
I18N = HERE / "i18n"
COOKIE = "st_lang"

_SKIP_BLOCK = re.compile(r"(<(script|style|textarea|pre|code)\b[^>]*>.*?</\2\s*>)", re.S | re.I)
_TAG = re.compile(r"(<[^>]+>)")
_ATTR = re.compile(r'(\s(?:placeholder|title|aria-label|alt)=")([^"]*)(")', re.I)
_BUTTON_VALUE = re.compile(r'(<input\b[^>]*\btype="(?:submit|button)"[^>]*\bvalue=")([^"]*)(")', re.I)
_WS = re.compile(r"\s+")
_AFTER = [(" per month", " ต่อเดือน"), (" a month", " ต่อเดือน"), ("/month", "/เดือน")]

# Dates written in words ("Thursday, 26 November 2026", "Wed 26 Nov 2026") -> Thai names.
_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
_TH_MONTHS = ["มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน", "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"]
_TH_MON = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]
_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_TH_DAYS = ["วันจันทร์", "วันอังคาร", "วันพุธ", "วันพฤหัสบดี", "วันศุกร์", "วันเสาร์", "วันอาทิตย์"]
_TH_DY = ["จ.", "อ.", "พ.", "พฤ.", "ศ.", "ส.", "อา."]
_DATE_WORD = {**{m: t for m, t in zip(_MONTHS, _TH_MONTHS)}, **{m[:3]: t for m, t in zip(_MONTHS, _TH_MON)},
              **{d: t for d, t in zip(_DAYS, _TH_DAYS)}, **{d[:3]: t for d, t in zip(_DAYS, _TH_DY)}}
_DATE_TOKEN = re.compile(r"\b(" + "|".join(sorted(_DATE_WORD, key=len, reverse=True)) + r")\b")
_DATE_ONLY = re.compile(r"^(?:(?:%s),?\s+)?(?:\d{1,2}\s+(?:%s)|(?:%s)\s+\d{1,2},?)(?:\s+\d{4})?(?:,?\s+\d{1,2}:\d{2}(?:\s?[AP]M)?)?$" % (
    "|".join(_DAYS + [d[:3] for d in _DAYS]), "|".join(_MONTHS + [m[:3] for m in _MONTHS]),
    "|".join(_MONTHS + [m[:3] for m in _MONTHS])))


def thai_date(s):
    s = _DATE_TOKEN.sub(lambda m: _DATE_WORD[m.group(1)], s).replace(",", "")
    return re.sub(r"(\d{4})\s+(\d{1,2}:\d{2})", r"\1 เวลา \2", s)

CSS = """.st-lang{position:fixed;right:12px;bottom:12px;z-index:2147483646;background:#2a1236;border-radius:999px;
padding:4px;display:flex;gap:2px;box-shadow:0 2px 10px rgba(0,0,0,.25);font:600 13px/1 system-ui,sans-serif}
.st-lang a{color:#fff4e4;text-decoration:none;padding:7px 11px;border-radius:999px}
.st-lang a.on{background:#ff4fa3;color:#2a1236}"""


def load_words(slug):
    words = {}
    for name in ("common.json", f"{slug}.json"):
        p = I18N / name
        if p.is_file():
            words.update(json.loads(p.read_text(encoding="utf-8")))
    return words


class Translator:
    def __init__(self, words):
        self.exact = {}
        self.patterns = []
        for en, th in words.items():
            if not th:
                continue
            key = _WS.sub(" ", en).strip()
            if "{}" in key:
                parts = [re.escape(p) for p in key.split("{}")]
                rx = re.compile("^" + "(.+?)".join(parts) + "$", re.S)
                self.patterns.append((rx, th))
            else:
                self.exact[key] = th
        self.patterns.sort(key=lambda p: -len(p[0].pattern))   # most specific first

    def text(self, raw):
        """Translate one piece of (already HTML-escaped) page text, keeping its surrounding spaces."""
        stripped = raw.strip()
        if not stripped or not re.search(r"[A-Za-z]", stripped):
            return raw
        key = _WS.sub(" ", html.unescape(stripped))
        th = self.exact.get(key)
        if th is None and _DATE_ONLY.match(key):
            th = thai_date(key)
        if th is None:
            for rx, template in self.patterns:
                m = rx.match(key)
                if m:
                    vals = iter([thai_date(v) if _DATE_ONLY.match(v.strip()) else self.exact.get(v.strip(), v)
                                 for v in m.groups()])
                    th = re.sub(r"\{\}", lambda _: next(vals, ""), template)
                    break
        if th is None:
            return raw
        lead = raw[: len(raw) - len(raw.lstrip())]
        trail = raw[len(raw.rstrip()):]
        return lead + html.escape(th, quote=False) + trail

    def _attr(self, value):
        return html.escape(self.text(html.unescape(value)), quote=True) if value else value

    def _chunk(self, chunk):
        out = []
        for i, piece in enumerate(_TAG.split(chunk)):
            if i % 2:   # a tag: translate its visible attributes
                piece = _ATTR.sub(lambda m: m.group(1) + self._attr(m.group(2)) + m.group(3), piece)
                piece = _BUTTON_VALUE.sub(lambda m: m.group(1) + self._attr(m.group(2)) + m.group(3), piece)
                out.append(piece)
            else:
                out.append(self.text(piece))
        return "".join(out)

    def page(self, body):
        out, pos = [], 0
        for m in _SKIP_BLOCK.finditer(body):      # scripts, styles and what customers typed stay as they are
            out.append(self._chunk(body[pos:m.start()]))
            out.append(m.group(0))
            pos = m.end()
        out.append(self._chunk(body[pos:]))
        body = "".join(out)
        for en, th in _AFTER:      # common leftovers inside values, e.g. prices "$12 per month"
            body = body.replace(en, th)
        return re.sub(r"<html\b([^>]*?)\slang=\"[^\"]*\"", r'<html\1 lang="th"', body, count=1)


def install_i18n(app, slug):
    if not (I18N / f"{slug}.json").is_file():
        return False          # this app has no Thai word list yet: no switch, page stays English
    words = load_words(slug)
    tr = Translator(words)

    def css():
        r = app.response_class(CSS, mimetype="text/css")
        r.headers["Cache-Control"] = "public, max-age=86400"
        return r

    def switch():
        to = "th" if request.args.get("to") == "th" else "en"
        nxt = request.args.get("next") or (request.script_root + "/")
        if not nxt.startswith(request.script_root + "/") or nxt.startswith("//"):
            nxt = request.script_root + "/"
        r = redirect(nxt)
        r.set_cookie(COOKIE, to, max_age=365 * 24 * 3600, path="/", samesite="Lax",   # one choice for every app
                     secure=request.is_secure, httponly=True)
        return r

    routes = {"/st-lang.css": css, "/st-lang": switch}

    def serve():
        view = routes.get(request.path)
        return view() if view else None

    app.before_request_funcs.setdefault(None, []).insert(0, serve)

    @app.after_request
    def _translate(response):
        try:
            if response.direct_passthrough or not (response.mimetype or "").startswith("text/html"):
                return response
            body = response.get_data(as_text=True)
            lang = request.cookies.get(COOKIE, "en")
            if lang == "th":
                body = tr.page(body)
            here = html.escape(quote(request.script_root + request.full_path.rstrip("?"), safe=""), quote=True)
            root = request.script_root
            switch_html = (f'<link rel="stylesheet" href="{root}/st-lang.css"><nav class="st-lang" aria-label="Language">'
                           f'<a href="{root}/st-lang?to=th&amp;next={here}" class="{"on" if lang == "th" else ""}" lang="th">ไทย</a>'
                           f'<a href="{root}/st-lang?to=en&amp;next={here}" class="{"on" if lang != "th" else ""}" lang="en">EN</a></nav>')
            i = body.lower().rfind("</body>")
            if i != -1:
                body = body[:i] + switch_html + body[i:]
            response.set_data(body)
        except Exception:
            pass   # never break a page over translation
        return response

    return True
