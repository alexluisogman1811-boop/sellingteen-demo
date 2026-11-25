"""Server-side form validation."""
import re
from datetime import date

# Catches things like "password: hunter2", "api_key=abc", "token: xyz" in free text.
SECRET_RE = re.compile(
    r"(?i)\b(pass(word|wd|phrase)?|pwd|api[\s_-]?key|secret|access[\s_-]?token|token|private[\s_-]?key)"
    r"\s*[:=]\s*\S+"
)
DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")

SECRET_MESSAGE = (
    "{label} looks like it contains a password, key or token. SiteCare Desk does not "
    "store credentials - keep them in a password manager."
)


def normalize_domain(raw):
    value = (raw or "").strip().lower()
    value = re.sub(r"^[a-z][a-z0-9+.-]*://", "", value)  # drop scheme
    value = re.split(r"[/?#]", value, maxsplit=1)[0]  # drop path/query
    if "@" in value:  # user:pass@host - never keep credentials
        value = value.rsplit("@", 1)[1]
    value = value.split(":", 1)[0]  # drop port
    return value.rstrip(".")


def parse_month(value):
    """Parse YYYY-MM into (year, month) or return None."""
    m = MONTH_RE.match((value or "").strip())
    if not m:
        return None
    year, month = int(m.group(1)), int(m.group(2))
    if not (2000 <= year <= 2100 and 1 <= month <= 12):
        return None
    return year, month


class Form:
    """Collects cleaned values in `data` and messages in `errors` (field -> message)."""

    def __init__(self, source):
        self.src = source
        self.data = {}
        self.errors = {}

    @property
    def ok(self):
        return not self.errors

    def error(self, name, message):
        self.errors.setdefault(name, message)

    def raw(self, name):
        return (self.src.get(name) or "").strip()

    def text(self, name, label, required=False, max_len=200, multiline=False, no_secrets=False):
        value = self.raw(name)
        value = value.replace("\r\n", "\n") if multiline else " ".join(value.split())
        if required and not value:
            self.error(name, f"{label} is required.")
        elif len(value) > max_len:
            self.error(name, f"{label} must be {max_len} characters or fewer.")
        elif no_secrets and SECRET_RE.search(value):
            self.error(name, SECRET_MESSAGE.format(label=label))
        self.data[name] = value
        return value

    def choice(self, name, label, choices, required=True):
        value = self.raw(name)
        if not value and not required:
            self.data[name] = ""
            return ""
        if value not in choices:
            self.error(name, f"Choose a valid {label.lower()}.")
        self.data[name] = value
        return value

    def date(self, name, label, required=True, not_after=None):
        value = self.raw(name)
        self.data[name] = None
        if not value:
            if required:
                self.error(name, f"{label} is required.")
            return None
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            self.error(name, f"{label} must be a valid date (YYYY-MM-DD).")
            return None
        if not 2000 <= parsed.year <= 2100:
            self.error(name, f"{label} must be between 2000 and 2100.")
            return None
        if not_after is not None and parsed > not_after:
            self.error(name, f"{label} cannot be in the future.")
            return None
        self.data[name] = parsed.isoformat()
        return parsed

    def integer(self, name, label, lo, hi, required=True):
        value = self.raw(name)
        self.data[name] = None
        if not value:
            if required:
                self.error(name, f"{label} is required.")
            return None
        try:
            number = int(value)
        except ValueError:
            self.error(name, f"{label} must be a whole number.")
            return None
        if not lo <= number <= hi:
            self.error(name, f"{label} must be between {lo} and {hi}.")
            return None
        self.data[name] = number
        return number

    def domain(self, name, label="Domain"):
        value = normalize_domain(self.src.get(name))
        if not value:
            self.error(name, f"{label} is required.")
        elif not DOMAIN_RE.match(value):
            self.error(name, f"Enter a valid domain such as example.com.")
        self.data[name] = value
        return value

    def email(self, name, label, required=False):
        value = self.raw(name).lower()
        if not value:
            if required:
                self.error(name, f"{label} is required.")
        elif len(value) > 200 or not EMAIL_RE.match(value):
            self.error(name, f"Enter a valid email address.")
        self.data[name] = value
        return value
