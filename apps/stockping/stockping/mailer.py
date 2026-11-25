"""Tiny OPTIONAL email sender (Python standard library only, no extra packages).

Email is OFF unless both SMTP_HOST and SMTP_FROM are set. It is used ONLY for:
  - self-service "forgot password" links
  - "your free trial ends in 3 days" reminders
It is NEVER used for marketing.

Settings (environment variables, read once when the app starts):
  SMTP_HOST      e.g. smtp.gmail.com            (empty = email off)
  SMTP_PORT      587 (STARTTLS, default) or 465 (SSL)
  SMTP_USER      login name for the mail server (optional)
  SMTP_PASSWORD  app password for the mail server (optional; keep it in an env file only)
  SMTP_FROM      the "From" address, e.g. sellingteen <you@example.com>
  SMTP_TLS       1 (default) = use encryption, 0 = none (only for a local test server)
  MAIL_BACKEND   smtp (default) or memory (tests: messages go to OUTBOX, nothing is sent)

Usage inside a Flask app:
    app.config["MAIL"] = mailer.mail_settings()          # in create_app()
    mailer.mail_enabled(current_app.config["MAIL"])       # True / False
    mailer.send_mail(current_app.config["MAIL"], to, subject, body)  # True if handed to the server
"""
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import parseaddr

OUTBOX = []  # filled instead of sending when MAIL_BACKEND=memory


def mail_settings(env=None):
    env = os.environ if env is None else env
    try:
        port = int(env.get("SMTP_PORT") or 587)
    except ValueError:
        port = 587
    return {
        "host": (env.get("SMTP_HOST") or "").strip(),
        "port": port,
        "user": (env.get("SMTP_USER") or "").strip(),
        "password": env.get("SMTP_PASSWORD") or "",
        "sender": (env.get("SMTP_FROM") or "").strip(),
        "tls": (env.get("SMTP_TLS") or "1").strip() != "0",
        "backend": (env.get("MAIL_BACKEND") or "smtp").strip().lower(),
    }


def mail_enabled(settings):
    if not settings:
        return False
    if settings.get("backend") == "memory":
        return True
    return bool(settings.get("host") and settings.get("sender"))


def _clean_header(value):
    # Stop header injection: no line breaks in To / Subject / From.
    return " ".join(str(value).replace("\r", " ").replace("\n", " ").split())


def send_mail(settings, to, subject, body):
    """Send one plain-text email. Returns True if handed to the mail server, False if email is off or it failed."""
    if not mail_enabled(settings):
        return False
    to = _clean_header(to)
    if "@" not in parseaddr(to)[1]:
        return False
    msg = EmailMessage()
    msg["From"] = _clean_header(settings.get("sender") or "sellingteen <no-reply@example.invalid>")
    msg["To"] = to
    msg["Subject"] = _clean_header(subject)[:200]
    msg.set_content(str(body))
    if settings.get("backend") == "memory":
        OUTBOX.append({"to": to, "subject": msg["Subject"], "body": str(body)})
        return True
    try:
        context = ssl.create_default_context()
        if settings["port"] == 465 and settings.get("tls", True):
            server = smtplib.SMTP_SSL(settings["host"], settings["port"], timeout=20, context=context)
        else:
            server = smtplib.SMTP(settings["host"], settings["port"], timeout=20)
            if settings.get("tls", True):
                server.starttls(context=context)
        with server:
            if settings.get("user"):
                server.login(settings["user"], settings.get("password", ""))
            server.send_message(msg)
        return True
    except (OSError, smtplib.SMTPException):
        return False
