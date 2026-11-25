"""Input parsing and validation for forms and query strings."""
import re
from datetime import date, datetime
from urllib.parse import urlparse

from . import timeutil
from .constants import (
    COLORS,
    MAX_CAPTION,
    MAX_DESCRIPTION,
    MAX_HASHTAGS,
    MAX_NAME,
    MAX_NOTES,
    MAX_TITLE,
    MAX_URL,
    PLATFORMS,
    STATUS_LABELS,
    STATUSES,
    TRANSITIONS,
)

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HASHTAG_BODY = re.compile(r"^\w{1,100}$", re.UNICODE)
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def clean_text(value, max_len, multiline=False):
    """Strip, normalise newlines, drop control characters and enforce a length."""
    value = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    value = _CONTROL_CHARS.sub("", value)
    if not multiline:
        value = " ".join(value.split())
    value = value.strip()
    return value, len(value) <= max_len


def parse_date(value):
    """Parse YYYY-MM-DD. Returns None for blank input, raises ValueError if invalid."""
    value = (value or "").strip()
    if not value:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Use the date format YYYY-MM-DD.")
    parsed = date.fromisoformat(value)  # raises ValueError for e.g. 2026-02-30
    if not 1970 <= parsed.year <= 2200:
        raise ValueError("Pick a date between 1970 and 2200.")
    return parsed


def parse_time(value):
    """Parse HH:MM (24h). Returns None for blank input, raises ValueError if invalid."""
    value = (value or "").strip()
    if not value:
        return None
    # Browsers may submit HH:MM:SS; keep minutes precision.
    if re.fullmatch(r"\d{2}:\d{2}:\d{2}", value):
        value = value[:5]
    if not _TIME_RE.fullmatch(value):
        raise ValueError("Use the time format HH:MM (24-hour).")
    return value


def normalize_hashtags(raw):
    """Turn free text like 'sale, #Summer  #summer' into '#sale #Summer'.

    Returns (normalized_string, error_or_None)."""
    raw = (raw or "").replace(",", " ")
    tags, seen = [], set()
    for token in raw.split():
        body = token.lstrip("#")
        if not body:
            continue
        if not _HASHTAG_BODY.fullmatch(body):
            return "", f"“{token[:40]}” is not a valid hashtag (letters, numbers and _ only)."
        key = body.casefold()
        if key in seen:
            continue
        seen.add(key)
        tags.append("#" + body)
    if len(tags) > MAX_HASHTAGS:
        return "", f"Use at most {MAX_HASHTAGS} hashtags."
    return " ".join(tags), None


def validate_url(value):
    value = (value or "").strip()
    if not value:
        return "", None
    if len(value) > MAX_URL:
        return "", "Link is too long."
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return "", "Link must start with http:// or https://."
    return value, None


def _optional_fk(conn, table, raw, account_id):
    raw = (raw or "").strip()
    if not raw:
        return None, None
    if not raw.isdigit() or len(raw) > 18:
        return None, "Invalid selection."
    # table is a fixed literal; the account check stops pointing at another business's campaign.
    row = conn.execute(f"SELECT id FROM {table} WHERE id = ? AND account_id = ?",
                       (int(raw), account_id)).fetchone()
    if row is None:
        return None, "That option no longer exists."
    return int(raw), None


def validate_post_form(form, conn, allowed_statuses, has_image=False, *, account_id):
    """Validate the create/edit post form. Returns (data, errors).

    `has_image` says whether the post will have an image after saving, so a
    scheduled post must have a caption or an image (no empty planned posts)."""
    errors = {}
    data = {}

    title, ok = clean_text(form.get("title"), MAX_TITLE)
    if not title:
        errors["title"] = "Give the post a short working title."
    elif not ok:
        errors["title"] = f"Keep the title under {MAX_TITLE} characters."
    data["title"] = title

    caption, ok = clean_text(form.get("caption"), MAX_CAPTION, multiline=True)
    if not ok:
        errors["caption"] = f"Caption is longer than {MAX_CAPTION} characters."
    data["caption"] = caption

    platform = (form.get("platform") or "").strip()
    if platform not in PLATFORMS:
        errors["platform"] = "Choose a platform."
    data["platform"] = platform

    status = (form.get("status") or "draft").strip()
    if status not in allowed_statuses:
        errors["status"] = "That status is not available here."
    data["status"] = status

    hashtags, err = normalize_hashtags(form.get("hashtags"))
    if err:
        errors["hashtags"] = err
    data["hashtags"] = hashtags

    data["campaign_id"], err = _optional_fk(conn, "campaigns", form.get("campaign_id"), account_id)
    if err:
        errors["campaign_id"] = err
    data["category_id"], err = _optional_fk(conn, "categories", form.get("category_id"), account_id)
    if err:
        errors["category_id"] = err

    try:
        planned = parse_date(form.get("planned_date"))
        data["planned_date"] = planned.isoformat() if planned else None
    except ValueError as exc:
        errors["planned_date"] = str(exc)
        data["planned_date"] = None
    try:
        data["planned_time"] = parse_time(form.get("planned_time"))
    except ValueError as exc:
        errors["planned_time"] = str(exc)
        data["planned_time"] = None
    if data["planned_time"] and not data["planned_date"] and "planned_date" not in errors:
        errors["planned_date"] = "Add a date to go with the time."
    if status == "scheduled" and not data["planned_date"] and "planned_date" not in errors:
        errors["planned_date"] = "Scheduled posts need a planned date."
    if status == "scheduled" and not caption and not has_image and "caption" not in errors:
        errors["caption"] = "Add a caption (or an image) before scheduling."

    notes, ok = clean_text(form.get("notes"), MAX_NOTES, multiline=True)
    if not ok:
        errors["notes"] = f"Notes must be under {MAX_NOTES} characters."
    data["notes"] = notes

    data["published_url"], err = validate_url(form.get("published_url"))
    if err:
        errors["published_url"] = err

    # Platform caption limits: block scheduling an over-length caption, warn otherwise.
    warnings = []
    info = PLATFORMS.get(platform)
    if info and info["limit"] and len(caption) > info["limit"] and "caption" not in errors:
        msg = (f"Caption is {len(caption)} characters; {info['label']} allows {info['limit']}.")
        if status == "scheduled":
            errors["caption"] = msg + " Shorten it before scheduling."
        else:
            warnings.append(msg + " Shorten it before scheduling.")
    if status == "scheduled" and data["planned_date"] and not errors:
        today, hm = today_and_now()
        if data["planned_date"] < today or (
            data["planned_date"] == today and data["planned_time"] and data["planned_time"] < hm
        ):
            warnings.append("The planned time is already in the past, so this post shows as overdue "
                            "until you mark it published or move it.")
    data["warnings"] = warnings
    return data, errors


LOCKED_WHEN_PUBLISHED = (("planned_date", "date"), ("planned_time", "time"), ("platform", "platform"))


def published_record_errors(existing, data):
    """A post that stays Published keeps its recorded date, time and platform."""
    errors = {}
    if existing["status"] == "published" and data["status"] == "published":
        for key, label in LOCKED_WHEN_PUBLISHED:
            if (data.get(key) or None) != (existing[key] or None):
                errors[key] = (f"This post is recorded as published, so its {label} is locked. "
                               "Change the status back to Scheduled first if the record is wrong.")
    return errors


def check_transition(current, new, planned_date, confirmed, was_published=False, has_content=True):
    """Return an error message if the status move is not allowed, else None.

    `was_published` is True when the post already carries a user-confirmed
    published record (published_at). Only such archived posts may be restored
    straight back to Published; that restores the earlier confirmation."""
    if new not in STATUSES:
        return "Unknown status."
    if new == current:
        return f"Post is already {STATUS_LABELS[new].lower()}."
    if current == "archived" and new == "published":
        if not was_published:
            return "Only posts you previously marked as published can be restored to Published."
        return None
    if new not in TRANSITIONS[current]:
        return f"A {STATUS_LABELS[current].lower()} post can’t move straight to {STATUS_LABELS[new].lower()}."
    if new == "scheduled" and not planned_date:
        return "Set a planned date before scheduling."
    if new == "scheduled" and not has_content:
        return "Add a caption or an image before scheduling."
    if new == "published" and not confirmed:
        return "Tick the confirmation box to record that you published this post yourself."
    return None


def validate_named_item(form, with_color=False):
    """Validate campaign/category forms."""
    errors, data = {}, {}
    name, ok = clean_text(form.get("name"), MAX_NAME)
    if not name:
        errors["name"] = "Name is required."
    elif not ok:
        errors["name"] = f"Keep the name under {MAX_NAME} characters."
    data["name"] = name
    desc, ok = clean_text(form.get("description"), MAX_DESCRIPTION, multiline=True)
    if not ok:
        errors["description"] = f"Description must be under {MAX_DESCRIPTION} characters."
    data["description"] = desc
    if with_color:
        color = form.get("color") or "blue"
        if color not in COLORS:
            errors["color"] = "Pick a colour from the list."
        data["color"] = color
        for key in ("start_date", "end_date"):
            try:
                d = parse_date(form.get(key))
                data[key] = d.isoformat() if d else None
            except ValueError as exc:
                errors[key] = str(exc)
                data[key] = None
        if data.get("start_date") and data.get("end_date") and data["start_date"] > data["end_date"]:
            errors["end_date"] = "End date must be on or after the start date."
    return data, errors


def safe_next(value, fallback):
    """Only allow same-site relative redirects."""
    from flask import request

    value = (value or "").strip()
    if value.startswith("/") and not value.startswith("//") and "\\" not in value:
        # When the app is mounted under a prefix (e.g. /contentqueue), stay inside it.
        root = request.script_root or ""
        if not root or value == root or value.startswith((root + "/", root + "?")):
            return value
    return fallback


def today_and_now():
    current = timeutil.now()
    return current.date().isoformat(), current.strftime("%H:%M")
