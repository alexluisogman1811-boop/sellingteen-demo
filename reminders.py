""""Your free trial ends in 3 days" emails (only when email is switched on in site.env).

Sent once per account per trial. Never sends marketing. If email is off, nothing happens and the apps keep
showing their in-app trial banner as before.
"""
from datetime import date, timedelta

import mailer
from control_center import AppendLog, Store, _as_date

DAYS_BEFORE = 3


def reminder_text(app_name, link, trial_end):
    return (f"Hello,\n\n"
            f"Your free trial of {app_name} ends on {trial_end.isoformat()} (in {DAYS_BEFORE} days).\n\n"
            f"To keep using it after that, log in and choose Subscribe:\n{link}\n\n"
            f"If you don't subscribe, nothing is charged. You can still log in to download (export) your data.\n\n"
            f"You get this email once, because you started a free trial. Reply if you have a question.\n\n"
            f"sellingteen\n")


def send_trial_reminders(registry, settings, data_dir, base_url, today=None):
    """Returns the number of reminders sent."""
    mail = mailer.mail_settings(settings)
    if not mailer.mail_enabled(mail):
        return 0
    today = today or date.today()
    target = today + timedelta(days=DAYS_BEFORE)
    store = Store(data_dir / "control.sqlite3")
    log = AppendLog(data_dir / "admin_log.jsonl")
    sent = 0
    for slug in registry.slugs:
        try:
            accounts = registry.list_accounts(slug)
        except Exception as exc:
            log.write("reminder_error", app=slug, result=f"{type(exc).__name__}: {exc}"[:200])
            continue
        for a in accounts:
            until = _as_date(a.get("until"))
            email = (a.get("email") or "").strip().lower()
            if a.get("state") != "trial" or until != target or not email:
                continue
            if store.reminder_sent(slug, email, until.isoformat()):
                continue
            link = f"{base_url}/{slug}/" if base_url else f"/{slug}/"
            if mailer.send_mail(mail, email, f"Your {registry.name(slug)} free trial ends in {DAYS_BEFORE} days",
                                reminder_text(registry.name(slug), link, until)):
                store.mark_reminder(slug, email, until.isoformat())
                log.write("trial_reminder_sent", app=slug, email=email)
                sent += 1
    return sent
