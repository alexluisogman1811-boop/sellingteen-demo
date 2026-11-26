"""One web address, many apps.

Runs every sellingteen app inside ONE PythonAnywhere web app (the free plan allows only one):
    https://YOURNAME.pythonanywhere.com/reviewboost
    https://YOURNAME.pythonanywhere.com/invoicechaser   ... and so on.
    https://YOURNAME.pythonanywhere.com/admin           Owner Control Center (off until a password is set)
    https://YOURNAME.pythonanywhere.com/webhooks/...    automatic activation after payment (off until a secret is set)

Each app gets its own settings file (env/<app>.env), its own database folder (data/<app>/),
its own login cookie (st_<app>, only sent to /<app>), and runs exactly as it does on its own.
"""
import json

from werkzeug.middleware.dispatcher import DispatcherMiddleware

from control_center import create_admin_app
from sitehost import HERE, MANIFEST, SITE_DATA, Registry, build_app, read_env, site_settings  # noqa: F401  (read_env kept for old imports)
from webhooks import create_webhooks_app


def make_application():
    apps = {slug: build_app(slug) for slug in MANIFEST["apps"]}
    settings = site_settings()
    registry = Registry(apps)
    SITE_DATA.mkdir(parents=True, exist_ok=True)
    mounts = {"/" + slug: app for slug, app in apps.items()}
    mounts["/admin"] = create_admin_app(registry, settings, SITE_DATA)
    mounts["/webhooks"] = create_webhooks_app(registry, settings, SITE_DATA, HERE / "webhooks.json")
    # One sellingteen account for every app (sign in / create accounts everywhere at once). It reaches each app
    # through the whole site, so it needs a reference to the finished dispatcher (filled in below).
    from hub import create_hub_app
    site = {}
    mounts["/account"] = create_hub_app(lambda: site["app"], MANIFEST)

    def home(environ, start_response):
        """The bare address sends visitors to the sellingteen website; /healthz is for monitoring."""
        if environ.get("PATH_INFO", "") == "/healthz":
            start_response("200 OK", [("Content-Type", "application/json")])
            return [json.dumps({"status": "ok", "apps": sorted(apps)}).encode()]
        start_response("302 Found", [("Location", MANIFEST.get("website", "https://sellingteen-apps.netlify.app"))])
        return [b""]

    site["app"] = DispatcherMiddleware(home, mounts)
    return site["app"], apps, registry


application, APPS, REGISTRY = make_application()
