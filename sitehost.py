"""Shared plumbing for the one-address hosting package (used by combined_wsgi.py, manage.py, backup.py, nightly.py).

- MANIFEST          : apps.json (which apps exist, their folder, price, billing module)
- read_env(path)    : reads a simple KEY=value settings file
- site_settings()   : sellingteen/site.env (address, admin password hash, webhook secrets, email settings)
- build_app(slug)   : creates one app with ONLY its own settings
- Registry          : one place to list accounts / switch accounts on and off in every app
"""
import importlib
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
MANIFEST = json.loads((HERE / "apps.json").read_text(encoding="utf-8"))
DATA = HERE / "data"
SITE_DATA = DATA / "_site"          # Control Center log, webhook records, reminder records

# Email settings live in site.env once and are handed to every app (so every app can send reset links).
MAIL_KEYS = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM", "SMTP_TLS", "MAIL_BACKEND")


def read_env(path):
    values = {}
    path = Path(path)
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def site_settings(root=None):
    return read_env((Path(root) if root else HERE) / "site.env")


def base_url(root=None):
    return site_settings(root).get("BASE_URL", "").rstrip("/")


def build_app(slug, root=None):
    """Create one app with ONLY its own settings in the environment (settings never leak between apps)."""
    root_dir = Path(root) if root else HERE
    spec = MANIFEST["apps"][slug]
    app_root = root_dir / "apps" / slug
    data_dir = root_dir / "data" / slug
    site = site_settings(root_dir)
    saved = dict(os.environ)
    try:
        for key in list(MANIFEST.get("managed_env", [])) + list(MAIL_KEYS):
            os.environ.pop(key, None)
        env = read_env(root_dir / "env" / f"{slug}.env")
        base = site.get("BASE_URL", "").rstrip("/")
        env = {k: v.replace("{DATA}", data_dir.as_posix()).replace("{BASE}", base) for k, v in env.items()}
        for key in MAIL_KEYS:                       # shared, optional email settings
            if site.get(key) and key not in env:
                env[key] = site[key]
        data_dir.mkdir(parents=True, exist_ok=True)
        os.environ.update(env)
        if str(app_root) not in sys.path:
            sys.path.insert(0, str(app_root))
        cwd = os.getcwd()
        os.chdir(app_root)
        try:
            module = importlib.import_module(spec["module"])
            target = getattr(module, spec["callable"])
            app = target() if spec.get("factory", True) else target
        finally:
            os.chdir(cwd)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    app = isolate_cookies(app, slug)
    prefill_payment_email(app)
    from pwa import install_pwa
    install_pwa(app, slug, spec.get("name", slug))
    from i18n import install_i18n
    install_i18n(app, slug)          # Thai switch, only for apps that have a Thai word list
    return app


STRIPE_LINK = re.compile(r'href="(https://buy\.stripe\.com/[A-Za-z0-9_]+)(\?[^"]*)?"')


def _current_email():
    """The logged-in person's email, whichever way this app stores it (None if unknown)."""
    try:
        from flask_login import current_user
        if getattr(current_user, "is_authenticated", False) and getattr(current_user, "email", None):
            return current_user.email
    except Exception:
        pass
    from flask import g
    for name in ("user", "account", "business", "company", "membership"):
        obj = g.get(name)
        if obj is None:
            continue
        for key in ("email", "owner_email"):
            try:
                value = obj[key] if not hasattr(obj, key) else getattr(obj, key)
            except (KeyError, IndexError, TypeError):
                value = None
            if isinstance(value, str) and "@" in value:
                return value
    return None


def prefill_payment_email(app):
    """Stripe Pay buttons open with the customer's sign-up email already filled in, so the payment
    matches their account and switches it on automatically. Pages without a Stripe link are untouched."""
    @app.after_request
    def _prefill(response):
        try:
            if response.direct_passthrough or not (response.mimetype or "").startswith("text/html"):
                return response
            body = response.get_data(as_text=True)
            if "buy.stripe.com/" not in body:
                return response
            email = _current_email()
            if not email:
                return response
            param = "prefilled_email=" + quote(email.strip().lower(), safe="")

            def add(m):
                query = m.group(2) or ""
                if "prefilled_email=" in query:
                    return m.group(0)
                return f'href="{m.group(1)}{query + "&amp;" if query else "?"}{param}"'

            response.set_data(STRIPE_LINK.sub(add, body))
        except Exception:
            pass  # never break a page over this
        return response


def isolate_cookies(app, slug):
    """Separate login cookie per app, sent by the browser only to that app's address."""
    app.config["SESSION_COOKIE_NAME"] = f"st_{slug}"
    app.config["SESSION_COOKIE_PATH"] = f"/{slug}"
    app.config["SESSION_COOKIE_DOMAIN"] = None
    return app


class Registry:
    """Talks to every app's billing module through the standard functions list_accounts() / set_paid_until()."""

    def __init__(self, apps, manifest=None):
        self.apps = dict(apps)                      # slug -> Flask app
        self.manifest = manifest or MANIFEST

    @property
    def slugs(self):
        return [s for s in self.manifest["apps"] if s in self.apps]

    def spec(self, slug):
        return self.manifest["apps"][slug]

    def name(self, slug):
        return self.spec(slug).get("name", slug)

    def price_usd(self, slug):
        try:
            return float(self.spec(slug).get("price_usd", 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    def _billing(self, slug):
        return importlib.import_module(self.spec(slug)["billing"])

    def list_accounts(self, slug):
        app = self.apps[slug]
        with app.app_context():
            rows = self._billing(slug).list_accounts()
        return [dict(r) for r in rows]

    def set_paid_until(self, slug, email, months):
        """Returns the new paid-until date (or None when switched off). Raises LookupError if no such account."""
        if slug not in self.apps:
            raise LookupError(f"Unknown app '{slug}'")
        app = self.apps[slug]
        with app.app_context():
            return self._billing(slug).set_paid_until(email, int(months))
