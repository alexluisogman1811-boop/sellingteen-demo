"""Render DEMO entry point: all apps at one address, with a "demo, data may reset" banner on every page.

Render's free plan wipes saved files whenever the service restarts or wakes from sleep,
so this is for trying the apps, NOT for paying customers. Real customers go on PythonAnywhere.

Start command on Render:  python render_start.py && gunicorn render_wsgi:application --workers 1 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT
"""
import os

from combined_wsgi import application as _inner

BANNER = (
    '<div style="position:sticky;top:0;z-index:2147483647;background:#ffb35c;color:#2a1236;'
    'font:600 14px/1.4 system-ui,sans-serif;padding:8px 14px;text-align:center">'
    'Demo only: try everything freely, but anything you enter may be erased when this demo restarts. '
    'Please don\'t enter real customer data.</div>'
).encode()


def _with_banner(app):
    def wrapped(environ, start_response):
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
                body = body[: j + 1] + BANNER + body[j + 1:]
        headers = [(k, v) for k, v in headers if k.lower() != "content-length"]
        headers.append(("Content-Length", str(len(body))))
        start_response(captured["status"], headers, captured.get("exc"))
        return [body]

    return wrapped


application = _with_banner(_inner) if os.environ.get("DEMO_MODE", "1") == "1" else _inner
