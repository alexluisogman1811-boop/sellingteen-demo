"""Install on phone ("Add to Home Screen") for every app.

Each app gets, at its own address (/<app>/...):
  manifest.webmanifest   name, icon, colours, opens full screen at /<app>/
  sw.js                  a tiny service worker (no caching: always shows fresh data)
  pwa-icon-192.png / pwa-icon-512.png / apple-touch-icon.png
and every HTML page gets the matching <link>/<meta> tags in <head>.

Icons are made once by build_package.py (make_icons) into pwa_icons/ and shipped in the zip.
"""
import json
import re
from pathlib import Path

from flask import Response, request, send_file

HERE = Path(__file__).resolve().parent
ICONS = HERE / "pwa_icons"
THEME = "#2a1236"
BG = (42, 18, 54)
PINK = (255, 79, 163)
CREAM = (255, 244, 228)

OWN_MANIFEST = re.compile(r'<link[^>]*rel="manifest"[^>]*>\s*', re.I)

SW_JS = """// sellingteen: lets the app be installed. No caching, so customers always see their latest data.
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
"""


def initials(name):
    caps = [c for c in name if c.isupper()]
    return ("".join(caps[:2]) if len(caps) >= 2 else name[:2]).upper()


def make_icons(manifest, out_dir=ICONS, font_path="C:/Windows/Fonts/segoeuib.ttf"):
    """Draw a simple icon per app: plum square, pink bar, two letters (e.g. LR for LeadRescue)."""
    from PIL import Image, ImageDraw, ImageFont
    out_dir = Path(out_dir)
    out_dir.mkdir(exist_ok=True)
    for slug, spec in manifest["apps"].items():
        text = initials(spec.get("name", slug))
        for size in (512, 192, 180):
            img = Image.new("RGB", (size, size), BG)
            d = ImageDraw.Draw(img)
            d.rectangle((0, int(size * 0.80), size, size), fill=PINK)
            try:
                f = ImageFont.truetype(font_path, int(size * 0.42))
            except OSError:
                f = ImageFont.load_default(size=int(size * 0.42))
            d.text((size / 2, size * 0.42), text, font=f, fill=CREAM, anchor="mm")
            name = "apple-touch-icon.png" if size == 180 else f"pwa-icon-{size}.png"
            img.save(out_dir / f"{slug}-{name}", optimize=True)


HEAD_TAGS = ('<link rel="manifest" href="{root}/manifest.webmanifest">'
             '<meta name="theme-color" content="' + THEME + '">'
             '<link rel="apple-touch-icon" href="{root}/apple-touch-icon.png">'
             '<meta name="apple-mobile-web-app-capable" content="yes">'
             '<meta name="mobile-web-app-capable" content="yes">'
             '<meta name="apple-mobile-web-app-title" content="{name}">'
             '<script src="{root}/pwa-register.js" defer></script>'
             '<script src="{root}/line-share.js" defer></script>')
LINE_JS = (HERE / "line_share.js").read_text(encoding="utf-8") if (HERE / "line_share.js").is_file() else ""

REGISTER_JS = """// sellingteen: register the install helper (works with strict Content-Security-Policy: no inline script).
(function () {
  if (!('serviceWorker' in navigator)) return;
  var s = document.currentScript && document.currentScript.src;
  if (!s) return;
  var root = s.replace(/\\/pwa-register\\.js.*$/, '');
  navigator.serviceWorker.register(root + '/sw.js', { scope: root + '/' }).catch(function () {});
})();
"""


def install_pwa(app, slug, name):
    def manifest():
        root = request.script_root
        data = {"name": f"{name} by sellingteen", "short_name": name, "start_url": root + "/",
                "scope": root + "/", "display": "standalone", "background_color": THEME, "theme_color": THEME,
                "icons": [{"src": root + "/pwa-icon-192.png", "sizes": "192x192", "type": "image/png"},
                          {"src": root + "/pwa-icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"}]}
        return Response(json.dumps(data), mimetype="application/manifest+json")

    def sw():
        r = Response(SW_JS, mimetype="text/javascript")
        r.headers["Service-Worker-Allowed"] = request.script_root + "/"
        r.headers["Cache-Control"] = "no-cache"
        return r

    def icon(fname):
        def view():
            path = ICONS / f"{slug}-{fname}"
            if not path.is_file():
                return Response(status=404)
            return send_file(path, mimetype="image/png", max_age=86400)
        return view

    def register_js():
        r = Response(REGISTER_JS, mimetype="text/javascript")
        r.headers["Cache-Control"] = "public, max-age=86400"
        return r

    def line_js():
        r = Response(LINE_JS, mimetype="text/javascript")
        r.headers["Cache-Control"] = "public, max-age=3600"
        return r

    routes = {"/manifest.webmanifest": manifest, "/sw.js": sw, "/pwa-register.js": register_js, "/line-share.js": line_js}
    for fname in ("pwa-icon-192.png", "pwa-icon-512.png", "apple-touch-icon.png"):
        routes["/" + fname] = icon(fname)

    def serve_pwa_files():
        """Runs before the app's own checks (some apps send everyone to log in first),
        so phones can always fetch the icon and install info."""
        view = routes.get(request.path)
        return view() if view else None

    app.before_request_funcs.setdefault(None, []).insert(0, serve_pwa_files)

    @app.after_request
    def _head(response):
        try:
            if response.direct_passthrough or not (response.mimetype or "").startswith("text/html"):
                return response
            body = response.get_data(as_text=True)
            if "/sw.js" in body and "apple-touch-icon" in body:
                return response
            body = OWN_MANIFEST.sub("", body)   # an app's own manifest (SVG icon only) is replaced by the full one
            i = body.lower().find("</head>")
            if i == -1:
                return response
            tags = HEAD_TAGS.format(root=request.script_root, name=name.replace('"', ""))
            response.set_data(body[:i] + tags + body[i:])
        except Exception:
            pass  # never break a page over this
        return response
