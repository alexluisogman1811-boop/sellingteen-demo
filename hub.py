"""One sellingteen account for every app: /account

- "Create accounts in all apps": one form (your name, business name, email, password). Each app's OWN sign-up
  page is filled in for you, so every app's own rules, checks and terms still apply.
- "Sign in to all apps": one email + password. Each app's OWN login page is used, and the login cookies each app
  sends back are handed to your browser, so you are logged in to every app at once.
- The password is only passed straight to each app's own form inside this server, never stored or logged here.
- Apps that need something extra (or where your password differs) are listed with a link to do them yourself.

Nothing inside the apps is changed.
"""
import html
import re
import secrets
import time
from collections import defaultdict, deque
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from flask import Flask, make_response, redirect, request

# Where each app keeps its login and sign-up pages (default: /login and /signup).
PATHS = {"slotsimple": ("/admin/login", "/admin/signup")}
BUSINESS_FIELDS = ("business_name", "company_name", "shop_name")
MIN_PASSWORD = 12           # the strictest app needs 10; 12 works everywhere
MAX_ATTEMPTS = 8            # per IP per 10 minutes (each attempt fans out to every app)


class _Forms(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms, self._cur = [], None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._cur = {"action": a.get("action") or "", "method": (a.get("method") or "get").lower(), "fields": []}
            self.forms.append(self._cur)
        elif tag in ("input", "select", "textarea") and self._cur is not None:
            self._cur["fields"].append({"tag": tag, **a})

    def handle_endtag(self, tag):
        if tag == "form":
            self._cur = None


def _post_form(page_html):
    p = _Forms()
    p.feed(page_html)
    return next((f for f in p.forms if f["method"] == "post" and any(x.get("type") == "password" for x in f["fields"])), None)


def _fill(form, values, sign_up):
    """Fill a login/sign-up form: hidden fields as they are, our values in the matching fields."""
    data = {}
    has_business = any(x.get("name") in BUSINESS_FIELDS for x in form["fields"])
    for f in form["fields"]:
        name, t = f.get("name"), (f.get("type") or "").lower()
        if not name:
            continue
        low = name.lower()
        if t == "hidden":
            data[name] = f.get("value", "")
        elif t == "password":
            data[name] = values["password"]
        elif t == "email" or "email" in low or low in ("username", "login", "user", "identifier"):
            data[name] = values["email"]
        elif not sign_up:
            if t == "checkbox" and "checked" in f:
                data[name] = f.get("value") or "on"
        elif low in BUSINESS_FIELDS:
            data[name] = values["business"]
        elif low in ("name", "full_name", "your_name", "owner_name"):
            data[name] = values["name"] if has_business else values["business"]
        elif t == "checkbox":
            if re.search(r"agree|terms|accept|consent", low):
                data[name] = f.get("value") or "on"   # the customer ticked "I agree" on our page
        elif f["tag"] == "select":
            pass                                       # app default
        elif "required" in f:
            return None                                # needs something we don't have: do it in the app
    return data


class _Inner:
    """Talks to one app inside this server, as the customer's browser would, keeping that app's cookies."""

    def __init__(self, wsgi_app, root_url, client_ip, user_agent):
        from werkzeug.test import Client
        self.c = Client(wsgi_app)
        self.base = root_url.rstrip("/")
        self.headers = {"User-Agent": user_agent or "sellingteen-account", "X-Forwarded-For": client_ip,
                        "X-Forwarded-Proto": urlsplit(root_url).scheme}
        self.environ = {"REMOTE_ADDR": client_ip}
        self.set_cookies = []

    def _req(self, method, path, data=None, referer=None):
        h = dict(self.headers)
        if referer:
            h["Referer"] = self.base + referer
        r = self.c.open(path, method=method, data=data, base_url=self.base, headers=h, environ_base=self.environ)
        self.set_cookies += r.headers.getlist("Set-Cookie")
        return r

    def go(self, method, path, data=None, referer=None, hops=4):
        r = self._req(method, path, data, referer)
        while r.status_code in (301, 302, 303, 307, 308) and hops:
            path = urlsplit(urljoin(self.base + path, r.headers["Location"])).path or "/"
            r = self._req("GET", path)
            hops -= 1
        return r, path


def _attempt(inner, slug, values, sign_up):
    login, signup = PATHS.get(slug, ("/login", "/signup"))
    path = f"/{slug}{signup if sign_up else login}"
    page, landed = inner.go("GET", path)
    form = _post_form(page.get_data(as_text=True)) if page.status_code == 200 else None
    if form is None:
        return "manual", "This app's form couldn't be opened here."
    data = _fill(form, values, sign_up)
    if data is None:
        return "manual", "This app asks for extra details at sign-up."
    action = form["action"] or landed
    action = urlsplit(urljoin(inner.base + landed, action)).path
    r, final = inner.go("POST", action, data=data, referer=landed)
    stay = {f"/{slug}{login}", f"/{slug}{signup}", action}
    if r.status_code < 400 and final not in stay and "/login" not in final and "/signup" not in final:
        return "ok", ""
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.get_data(as_text=True)))
    if sign_up and re.search(r"already (exists|registered|has an account)|already in use|already used", text, re.I):
        return "exists", "You already have an account here: use Sign in."
    return "failed", ("Sign-up wasn't accepted here. Open the app to see why." if sign_up
                      else "Email or password didn't match here (or this app has no account for you yet).")


TEXT = {
    "en": {"title": "Your sellingteen account", "lede": "One email and password for every sellingteen app.",
           "signin": "Sign in to all apps", "create": "Create accounts in all apps", "email": "Email",
           "password": "Password", "again": "Password again", "name": "Your name", "business": "Business name",
           "agree": "I agree to each app's Terms, Privacy Policy and Refund Policy.", "go_in": "Sign in everywhere",
           "go_create": "Create my accounts", "wait": "This takes a few seconds.", "results": "Results",
           "ok": "Signed in", "made": "Account created", "exists": "Already has an account", "failed": "Not done",
           "manual": "Do this one yourself", "open": "Open", "back": "Back", "min": f"At least {MIN_PASSWORD} characters.",
           "err_pw": f"Use a password of at least {MIN_PASSWORD} characters.", "err_match": "The two passwords don't match.",
           "err_agree": "Please tick that you agree to the terms.", "err_fields": "Please fill in every field.",
           "err_rate": "Too many attempts. Please wait 10 minutes.", "err_form": "The form expired. Please try again.",
           "note": "Your password is passed straight to each app's own login and is never stored by this page."},
    "th": {"title": "บัญชี sellingteen ของคุณ", "lede": "อีเมลและรหัสผ่านเดียว ใช้ได้กับทุกแอปของ sellingteen",
           "signin": "เข้าสู่ระบบทุกแอป", "create": "สร้างบัญชีในทุกแอป", "email": "อีเมล", "password": "รหัสผ่าน",
           "again": "ยืนยันรหัสผ่าน", "name": "ชื่อของคุณ", "business": "ชื่อธุรกิจ",
           "agree": "ยอมรับข้อกำหนด นโยบายความเป็นส่วนตัว และนโยบายการคืนเงินของแต่ละแอป", "go_in": "เข้าสู่ระบบทุกแอป",
           "go_create": "สร้างบัญชีของฉัน", "wait": "ใช้เวลาสักครู่", "results": "ผลลัพธ์", "ok": "เข้าสู่ระบบแล้ว",
           "made": "สร้างบัญชีแล้ว", "exists": "มีบัญชีอยู่แล้ว", "failed": "ยังไม่สำเร็จ", "manual": "ทำเองในแอปนี้",
           "open": "เปิด", "back": "กลับ", "min": f"อย่างน้อย {MIN_PASSWORD} ตัวอักษร",
           "err_pw": f"ใช้รหัสผ่านอย่างน้อย {MIN_PASSWORD} ตัวอักษร", "err_match": "รหัสผ่านทั้งสองช่องไม่ตรงกัน",
           "err_agree": "กรุณาติ๊กยอมรับข้อกำหนด", "err_fields": "กรุณากรอกให้ครบทุกช่อง",
           "err_rate": "ลองหลายครั้งเกินไป กรุณารอ 10 นาที", "err_form": "ฟอร์มหมดอายุ กรุณาลองใหม่",
           "note": "รหัสผ่านจะถูกส่งตรงไปยังหน้าเข้าสู่ระบบของแต่ละแอป และไม่ถูกเก็บไว้ที่หน้านี้"},
}

CSS = """body{margin:0;background:#1a0b26;color:#fff4e4;font:16px/1.5 system-ui,sans-serif}
main{max-width:640px;margin:0 auto;padding:32px 16px}h1{margin:.2em 0}.lede{color:#d6bedc}
.card{background:#2a1440;border-radius:16px;padding:20px;margin:18px 0}label{display:block;margin:10px 0 4px;font-weight:600}
input[type=text],input[type=email],input[type=password]{width:100%;box-sizing:border-box;padding:10px;border-radius:10px;border:1px solid #5a3a70;background:#1a0b26;color:#fff4e4;font:inherit}
button{margin-top:14px;background:#ff4fa3;color:#1a0b26;border:0;border-radius:999px;padding:11px 20px;font:700 16px system-ui;cursor:pointer}
.hint{color:#bfa6c8;font-size:14px}.err{background:#5a1a2a;color:#ffd6dc;padding:10px 12px;border-radius:10px}
table{width:100%;border-collapse:collapse}td{padding:8px 4px;border-bottom:1px solid #3a2050}
.ok{color:#57e6c0}.bad{color:#ffb35c}a{color:#ffb35c}.agree{font-weight:400;display:flex;gap:8px;align-items:flex-start}
.lang{float:right}.lang a{margin-left:8px}"""


def create_hub_app(get_wsgi, manifest, data_dir=None):
    """get_wsgi(): the whole combined site (so each app is reached at its real /<app> address)."""
    app = Flask("sellingteen_hub")
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    tokens = {}                     # one-time form tokens -> expiry (no cookies needed for CSRF)
    attempts = defaultdict(deque)

    def lang():
        return "th" if request.cookies.get("st_lang") == "th" else "en"

    def token():
        now = time.time()
        for k in [k for k, exp in tokens.items() if exp < now]:
            tokens.pop(k, None)
        t = secrets.token_urlsafe(24)
        tokens[t] = now + 1800
        return t

    def page(body, status=200):
        L = lang()
        here = request.script_root + "/"
        switch = (f'<span class="lang"><a href="/{next(iter(manifest["apps"]))}/st-lang?to=th&amp;next={html.escape(here)}">ไทย</a>'
                  f'<a href="/{next(iter(manifest["apps"]))}/st-lang?to=en&amp;next={html.escape(here)}">EN</a></span>')
        doc = (f'<!doctype html><html lang="{L}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
               f'<title>{TEXT[L]["title"]}</title><link rel="stylesheet" href="{request.script_root}/hub.css"></head>'
               f'<body><main>{switch}{body}</main></body></html>')
        r = make_response(doc, status)
        r.headers["Cache-Control"] = "no-store"
        r.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; form-action 'self'; frame-ancestors 'none'"
        r.headers["X-Frame-Options"] = "DENY"
        r.headers["Referrer-Policy"] = "same-origin"
        return r

    def forms(error="", values=None):
        T = TEXT[lang()]
        v = {k: html.escape(x) for k, x in (values or {}).items()}
        err = f'<p class="err">{html.escape(error)}</p>' if error else ""
        return page(
            f'<h1>{T["title"]}</h1><p class="lede">{T["lede"]}</p>{err}'
            f'<section class="card"><h2>{T["signin"]}</h2><form method="post" action="{request.script_root}/signin">'
            f'<input type="hidden" name="t" value="{token()}">'
            f'<label for="se">{T["email"]}</label><input id="se" type="email" name="email" required autocomplete="email" value="{v.get("email", "")}">'
            f'<label for="sp">{T["password"]}</label><input id="sp" type="password" name="password" required autocomplete="current-password">'
            f'<button>{T["go_in"]}</button> <span class="hint">{T["wait"]}</span></form></section>'
            f'<section class="card"><h2>{T["create"]}</h2><form method="post" action="{request.script_root}/create">'
            f'<input type="hidden" name="t" value="{token()}">'
            f'<label for="cn">{T["name"]}</label><input id="cn" type="text" name="name" required maxlength="100" autocomplete="name" value="{v.get("name", "")}">'
            f'<label for="cb">{T["business"]}</label><input id="cb" type="text" name="business" required maxlength="100" autocomplete="organization" value="{v.get("business", "")}">'
            f'<label for="ce">{T["email"]}</label><input id="ce" type="email" name="email" required autocomplete="email" value="{v.get("email", "")}">'
            f'<label for="cp">{T["password"]}</label><input id="cp" type="password" name="password" required minlength="{MIN_PASSWORD}" autocomplete="new-password">'
            f'<span class="hint">{T["min"]}</span>'
            f'<label for="cp2">{T["again"]}</label><input id="cp2" type="password" name="password2" required minlength="{MIN_PASSWORD}" autocomplete="new-password">'
            f'<label class="agree"><input type="checkbox" name="agree" value="1" required> {T["agree"]}</label>'
            f'<button>{T["go_create"]}</button> <span class="hint">{T["wait"]}</span></form></section>'
            f'<p class="hint">{T["note"]}</p>', 400 if error else 200)

    def limited():
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
        q, now = attempts[ip], time.time()
        while q and q[0] < now - 600:
            q.popleft()
        if len(q) >= MAX_ATTEMPTS:
            return True
        q.append(now)
        return False

    def check_token():
        return tokens.pop(request.form.get("t", ""), 0) >= time.time()

    def run(sign_up):
        T = TEXT[lang()]
        f = request.form
        values = {"email": f.get("email", "").strip().lower(), "password": f.get("password", ""),
                  "name": f.get("name", "").strip(), "business": f.get("business", "").strip()}
        keep = {k: values[k] for k in ("email", "name", "business")}
        if not check_token():
            return forms(T["err_form"], keep)
        if limited():
            return forms(T["err_rate"], keep)
        if not values["email"] or not values["password"] or (sign_up and not (values["name"] and values["business"])):
            return forms(T["err_fields"], keep)
        if sign_up:
            if len(values["password"]) < MIN_PASSWORD:
                return forms(T["err_pw"], keep)
            if values["password"] != f.get("password2", ""):
                return forms(T["err_match"], keep)
            if f.get("agree") != "1":
                return forms(T["err_agree"], keep)
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "127.0.0.1").split(",")[0].strip()
        rows, cookies = [], []
        for slug, spec in manifest["apps"].items():
            inner = _Inner(get_wsgi(), request.host_url, ip, request.headers.get("User-Agent", ""))   # site root, not /account
            try:
                state, why = _attempt(inner, slug, values, sign_up)
            except Exception:
                state, why = "failed", ""
            if state == "ok":
                cookies += [c for c in inner.set_cookies if c.split("=", 1)[0].strip() == f"st_{slug}"]
            rows.append((slug, spec.get("name", slug), state, why))
        values["password"] = ""                 # drop it as soon as we're done
        label = {"ok": T["made"] if sign_up else T["ok"], "exists": T["exists"], "failed": T["failed"], "manual": T["manual"]}
        trs = "".join(
            f'<tr><td>{html.escape(name)}</td><td class="{"ok" if state == "ok" else "bad"}">{label[state]}'
            f'{"<br><span class=hint>" + html.escape(why) + "</span>" if why and lang() == "en" else ""}</td>'
            f'<td><a href="/{slug}/" target="_blank" rel="noopener">{T["open"]}</a></td></tr>'
            for slug, name, state, why in rows)
        done = sum(1 for r in rows if r[2] == "ok")
        resp = page(f'<h1>{T["results"]}</h1><p class="lede">{done} / {len(rows)}</p><table>{trs}</table>'
                    f'<p><a href="{request.script_root}/">{T["back"]}</a></p>')
        for c in cookies:                        # each app's own login cookie, only for that app's address
            resp.headers.add("Set-Cookie", c)
        return resp

    @app.route("/")
    def home():
        return forms()

    @app.route("/hub.css")
    def css():
        r = make_response(CSS)
        r.mimetype = "text/css"
        return r

    @app.route("/signin", methods=["POST"])
    def signin():
        return run(sign_up=False)

    @app.route("/create", methods=["POST"])
    def create():
        return run(sign_up=True)

    @app.route("/signin", methods=["GET"])
    @app.route("/create", methods=["GET"])
    def back_home():
        return redirect(request.script_root + "/")

    return app
