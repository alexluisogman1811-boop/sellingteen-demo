"""Input cleaning and validation.

Content is stored as plain text and escaped on output by Jinja; we never
render submitted content as HTML. Cleaning removes control and bidi-override
characters, normalises whitespace and enforces length limits.
"""
import re
import unicodedata
from datetime import datetime
from urllib.parse import urlsplit

LIMITS = {
    "customer_name": 100,
    "business": 120,
    "feedback": 2000,
    "website": 200,
    "business_name": 120,
    "project_name": 120,
    "client_name": 120,
    "description": 500,
    "intro_message": 500,
    "permission_note": 300,
}
FEEDBACK_MIN = 20
MAX_LINKS_IN_FEEDBACK = 2

_CONTROL = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁦-⁩﻿]"
)
_LINK = re.compile(r"https?://|www\.", re.I)
_HOST = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}$")


def clean_text(value, multiline=False):
    if value is None:
        return ""
    value = unicodedata.normalize("NFC", str(value))
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = _CONTROL.sub("", value)
    if multiline:
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.split("\n")]
        value = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    else:
        value = re.sub(r"\s+", " ", value)
    return value.strip()


def _check_length(data, errors, field, label, required=False, minimum=None):
    value = data[field]
    if required and not value:
        errors[field] = f"{label} is required."
    elif minimum and value and len(value) < minimum:
        errors[field] = f"{label} must be at least {minimum} characters."
    elif len(value) > LIMITS[field]:
        errors[field] = f"{label} must be {LIMITS[field]} characters or fewer."


def normalize_website(raw):
    """Return (url, error). Only http(s) URLs with a real-looking hostname."""
    raw = clean_text(raw)
    if not raw:
        return "", None
    if len(raw) > LIMITS["website"]:
        return raw, f"Website must be {LIMITS['website']} characters or fewer."
    candidate = raw if re.match(r"^[a-z][a-z0-9+.-]*:", raw, re.I) else "https://" + raw
    invalid = "Please enter a valid website address starting with http:// or https://."
    try:
        parts = urlsplit(candidate)
        host = (parts.hostname or "").lower()
        port_ok = parts.port is None or 0 < parts.port < 65536
    except ValueError:
        return raw, "Please enter a valid website address."
    if (parts.scheme.lower() not in ("http", "https") or parts.username or parts.password
            or not port_ok or " " in candidate):
        return raw, invalid
    # International domain names (e.g. bücher.de) are stored in ASCII/punycode form,
    # which also neutralises look-alike Unicode characters in the stored link.
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return raw, invalid
    if not _HOST.match(ascii_host):
        return raw, invalid
    if ascii_host != host:
        netloc = ascii_host + (f":{parts.port}" if parts.port else "")
        candidate = parts._replace(netloc=netloc).geturl()
    return candidate, None


def parse_rating(raw):
    raw = (raw or "").strip()
    if raw == "":
        return None, None
    if raw.isdigit() and 1 <= int(raw) <= 5:
        return int(raw), None
    return None, "Rating must be a whole number from 1 to 5, or left blank."


def validate_testimonial(form, include_rating=True):
    """Validate a testimonial submission or owner edit. Returns (data, errors)."""
    data = {
        "customer_name": clean_text(form.get("customer_name")),
        "business": clean_text(form.get("business")),
        "feedback": clean_text(form.get("feedback"), multiline=True),
    }
    errors = {}
    _check_length(data, errors, "customer_name", "Your name", required=True)
    _check_length(data, errors, "business", "Business")
    _check_length(data, errors, "feedback", "Feedback", required=True, minimum=FEEDBACK_MIN)
    if "feedback" not in errors and len(_LINK.findall(data["feedback"])) > MAX_LINKS_IN_FEEDBACK:
        errors["feedback"] = "Please remove links from your feedback."
    for field, label in (("customer_name", "Name"), ("business", "Business")):
        if field not in errors and _LINK.search(data[field]):
            errors[field] = f"{label} can't contain web links. Use the website field instead."

    data["website"], err = normalize_website(form.get("website"))
    if err:
        errors["website"] = err

    if include_rating:
        data["rating"], err = parse_rating(form.get("rating"))
        if err:
            errors["rating"] = err
    return data, errors


def validate_project(form):
    data = {
        "project_name": clean_text(form.get("name")),
        "client_name": clean_text(form.get("client_name")),
        "description": clean_text(form.get("description"), multiline=True),
        "intro_message": clean_text(form.get("intro_message"), multiline=True),
    }
    errors = {}
    _check_length(data, errors, "project_name", "Project name", required=True)
    _check_length(data, errors, "client_name", "Client name")
    _check_length(data, errors, "description", "Description")
    _check_length(data, errors, "intro_message", "Message to clients")
    return data, errors


def validate_profile(form):
    data = {"business_name": clean_text(form.get("business_name"))}
    errors = {}
    _check_length(data, errors, "business_name", "Business name", required=True)
    data["website"], err = normalize_website(form.get("website"))
    if err:
        errors["website"] = err
    return data, errors


def validate_permission_note(raw):
    note = clean_text(raw, multiline=True)
    if len(note) < 5:
        return note, "Describe how permission was obtained (e.g. 'Client confirmed by email on 12 May')."
    if len(note) > LIMITS["permission_note"]:
        return note, f"Note must be {LIMITS['permission_note']} characters or fewer."
    return note, None


def parse_date(raw):
    """Parse YYYY-MM-DD; return the string if valid, else None."""
    raw = (raw or "").strip()
    try:
        datetime.strptime(raw, "%Y-%m-%d")
        return raw
    except ValueError:
        return None
