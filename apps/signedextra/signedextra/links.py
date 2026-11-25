"""Links people copy and send (invite, signing and password-reset links).

They must point at the real public address. With PUBLIC_BASE_URL set (always in production) the
request's Host header is never used. In development without it, Flask's normal external URL is used.
"""
from flask import current_app, has_request_context, request, url_for


def public_base_configured():
    base = (current_app.config.get("PUBLIC_BASE_URL") or "").strip()
    return base.startswith(("http://", "https://"))


def shareable_url(endpoint, **values):
    base = (current_app.config.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    path = url_for(endpoint, **values)
    if base:
        root = request.script_root if has_request_context() else ""
        if root and path.startswith(root):
            path = path[len(root):]
        return base + path
    if current_app.config.get("PRODUCTION"):
        # No public address configured: give the path only rather than trusting the Host header.
        return path
    return url_for(endpoint, _external=True, **values)
