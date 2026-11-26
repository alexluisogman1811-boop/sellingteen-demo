"""Render DEMO entry point: all apps at one address, with a "demo, data may reset" banner on every page.

Render's free plan wipes saved files whenever the service restarts or wakes from sleep,
so this is for trying the apps, NOT for paying customers. Real customers go on PythonAnywhere.

Start command on Render:  python render_start.py && gunicorn render_wsgi:application --workers 1 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT
"""
import os

from combined_wsgi import application as _inner

# Styled by a separate CSS file (not inline), so apps with a strict Content-Security-Policy still show it properly.
BANNER = (
    '<link rel="stylesheet" href="/_demo/banner.css"><div class="st-demo-banner" role="note">'
    'Demo only: try everything freely, but anything you enter may be erased when this demo restarts. '
    'Please don\'t enter real customer data.</div>'
).encode()
EXAMPLE_LOGINS = {"invoicechaser", "payrecover", "contractordocs", "leadfollowup"}   # see demo_accounts.py


def banner_for(path):
    slug = (path or "/").strip("/").split("/", 1)[0]
    if slug in EXAMPLE_LOGINS:
        return BANNER.replace(b"</div>", b" <strong>Look around without signing up:</strong> log in with "
                                          b"<code>demo@example.com</code> / <code>demo-example-123</code> (example account).</div>")
    return BANNER


BANNER_CSS = (b".st-demo-banner{position:sticky;top:0;z-index:2147483647;background:#ffb35c;color:#2a1236;"
              b"font:600 14px/1.4 system-ui,sans-serif;padding:8px 14px;text-align:center}")


def _with_banner(app):
    def wrapped(environ, start_response):
        if environ.get("PATH_INFO") == "/_demo/banner.css":
            start_response("200 OK", [("Content-Type", "text/css"), ("Content-Length", str(len(BANNER_CSS))),
                                      ("Cache-Control", "public, max-age=86400")])
            return [BANNER_CSS]
        captured = {}

        def capture(status, headers, exc_info=None):
            captured["status"], captured["headers"], captured["exc"] = status, headers, exc_info
            return lambda data: None  # apps here return bodies, they don't call write()

        body_iter = app(environ, capture)
        headers = captured.get("headers", [])
        ctype = next((v for k, v in headers if k.lower() == "content-type"), "")
        if not ctype.startswith("text/html"):
            start_response(captured["status"], headers, captured.get("exc"))
            return body_iter
        try:
            body = b"".join(body_iter)
        finally:
            if hasattr(body_iter, "close"):
                body_iter.close()
        low = body.lower()
        i = low.find(b"<body")
        if i != -1:
            j = body.find(b">", i)
            if j != -1:
                body = body[: j + 1] + banner_for(environ.get("SCRIPT_NAME", "") + environ.get("PATH_INFO", "")) + body[j + 1:]
        headers = [(k, v) for k, v in headers if k.lower() != "content-length"]
        headers.append(("Content-Length", str(len(body))))
        start_response(captured["status"], headers, captured.get("exc"))
        return [body]

    return wrapped


application = _with_banner(_inner) if os.environ.get("DEMO_MODE", "1") == "1" else _inner
