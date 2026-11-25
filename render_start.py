"""Runs once each time Render starts the demo (Render forgets all files on every restart).

- Creates site.env, env/<app>.env (fresh secret keys) and webhooks.json from the templates.
- Sets the web address from Render's RENDER_EXTERNAL_URL.
- Turns on the Control Center only if you set ADMIN_PASSWORD (12+ characters) in Render's Environment tab.
"""
import os
import sys

from manage import set_admin_password, set_env_values, setup
from sitehost import HERE


def main():
    made = setup()
    print("render_start: created", ", ".join(made) or "nothing (already there)")
    url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if url:
        set_env_values(HERE / "site.env", {"BASE_URL": url})
        print("render_start: BASE_URL =", url)
    pw = os.environ.get("ADMIN_PASSWORD", "")
    if len(pw) >= 12:
        set_admin_password(HERE, pw)
        print("render_start: Control Center turned on at /admin")
    else:
        print("render_start: Control Center stays off (set ADMIN_PASSWORD, 12+ characters, to turn it on)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
