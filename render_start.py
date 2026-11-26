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
    if os.environ.get("DEMO_MODE", "1") == "1":
        # Never take real money on the demo (its data can be erased): hide every Pay button.
        for env_file in sorted((HERE / "env").glob("*.env")):
            set_env_values(env_file, {"PAYMENT_LINK": ""})
        print("render_start: demo mode, payment links switched off")
        # Ready-made example accounts that some apps include (all fictional sample data):
        demo_flags = {"proofpay": {"DEMO_MODE": "1"},          # "Try the demo" button on its home page
                      "slotsimple": {"DEMO_MODE": "1"}}        # demo studio; login shown on its login page
        # (SignedExtra deliberately refuses demo data on a production server, so it is not switched on here.)
        for slug, values in demo_flags.items():
            env_file = HERE / "env" / f"{slug}.env"
            if env_file.is_file():
                set_env_values(env_file, values)
        # Example accounts (fictional data) for apps that have no demo button of their own.
        try:
            from demo_accounts import seed_all
            from sitehost import build_app
            print("render_start: example accounts:", seed_all(build_app))
        except Exception as exc:
            print("render_start: example accounts skipped:", exc)
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
