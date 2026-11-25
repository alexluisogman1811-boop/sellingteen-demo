"""Local, rule-based caption templates. No AI service, no network calls.

Templates use {placeholder} markers, e.g. "New in store: {product}! {cta}".
Substitution is a plain regex replace (never str.format), so user text cannot
reach Python attribute lookups.
"""
import re

PLACEHOLDER_RE = re.compile(r"\{([a-z][a-z0-9_]{0,30})\}")
MAX_VALUE = 300


def find_placeholders(body):
    seen = []
    for name in PLACEHOLDER_RE.findall(body or ""):
        if name not in seen:
            seen.append(name)
    return seen


def fill_template(body, values):
    """Replace {name} markers with values; unknown/blank markers are left in place."""

    def repl(match):
        value = (values.get(match.group(1)) or "").strip()[:MAX_VALUE]
        return value if value else match.group(0)

    return PLACEHOLDER_RE.sub(repl, body or "")


def label_for(name):
    return name.replace("_", " ").capitalize()
