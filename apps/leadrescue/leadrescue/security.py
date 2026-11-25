from urllib.parse import urlsplit

from flask import Flask, g, redirect, request

from .tenancy import load_active_membership

CSP = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    "style-src 'self'; "
    "script-src 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "object-src 'none'"
)


def is_safe_redirect(target: str | None) -> bool:
    """Only allow same-site relative paths (blocks open redirects like //evil.com)."""
    if not target:
        return False
    if "\\" in target or any(ord(c) < 32 for c in target):
        return False
    parts = urlsplit(target)
    return not parts.scheme and not parts.netloc and target.startswith("/") and not target.startswith("//")


def with_script_root(target: str) -> str:
    """Make a safe relative path point inside this app when it's mounted under a prefix
    (e.g. /leadrescue on the shared sellingteen host). Paths that already include the
    prefix are left alone."""
    root = request.script_root or ""
    if root and not (target == root or target.startswith(root + "/")):
        return root + target
    return target


def is_same_app_referrer() -> bool:
    """True when the Referer is a page of THIS app (same host and same URL prefix),
    so another app on the same host can't steer our redirects."""
    ref = request.referrer
    return bool(ref) and ref.startswith(request.url_root)


def register_security(app: Flask) -> None:
    @app.before_request
    def _force_https():
        # The health check is called by the host over plain HTTP inside its network.
        if app.config.get("FORCE_HTTPS") and not request.is_secure and request.endpoint != "main.healthz":
            return redirect(request.url.replace("http://", "https://", 1), code=301)

    @app.before_request
    def _load_tenant():
        g.memberships = []
        load_active_membership()

    @app.after_request
    def _headers(response):
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if app.config.get("SESSION_COOKIE_SECURE"):
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if request.endpoint and request.endpoint != "static":
            response.headers.setdefault("Cache-Control", "no-store")
        return response
