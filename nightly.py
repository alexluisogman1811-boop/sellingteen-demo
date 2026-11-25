"""The one daily job (PythonAnywhere free plan: Tasks tab -> one daily scheduled task):

    cd ~/sellingteen && python3.13 nightly.py

1. Makes a backup zip of all customer data (keeps the newest 7).
2. If email is switched on in site.env: sends "your trial ends in 3 days" reminders (once per account).
"""
from backup import make_backup
from reminders import send_trial_reminders
from sitehost import HERE, MANIFEST, SITE_DATA, Registry, base_url, build_app, site_settings


def main():
    path, n = make_backup()
    print(f"Backup saved: {path.name} ({n} files)")
    settings = site_settings()
    if settings.get("SMTP_HOST") and settings.get("SMTP_FROM"):
        apps = {slug: build_app(slug) for slug in MANIFEST["apps"]}
        sent = send_trial_reminders(Registry(apps), settings, SITE_DATA, base_url())
        print(f"Trial reminders sent: {sent}")
    else:
        print("Email is off (no SMTP_HOST / SMTP_FROM in site.env): no reminders sent.")


if __name__ == "__main__":
    main()
