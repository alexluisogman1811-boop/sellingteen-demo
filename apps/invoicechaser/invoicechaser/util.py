"""Shared helpers: dates, money, invoice status, auth guard, rate limiting."""
import functools
import threading
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from flask import current_app, g, redirect, request, url_for

DUE_SOON_DAYS = 7
MAX_AMOUNT_CENTS = 100_000_000_000  # 1 billion in major units
MIN_DATE = date(2000, 1, 1)
MAX_DATE = date(2100, 12, 31)

CURRENCIES = {
    "USD": "$", "EUR": "€", "GBP": "£", "THB": "฿", "AUD": "A$",
    "CAD": "C$", "SGD": "S$", "NZD": "NZ$", "INR": "₹", "JPY": "¥",
}
ZERO_DECIMAL_CURRENCIES = {"JPY"}

STATUS_LABELS = {
    "paid": "Paid",
    "overdue": "Overdue",
    "due_soon": "Due soon",
    "open": "Not due yet",
}


@functools.lru_cache(maxsize=1)
def timezones():
    try:
        return sorted(available_timezones())
    except Exception:  # pragma: no cover - platform without tz database
        return ["UTC"]


def valid_timezone(name):
    if not name or name not in timezones():
        return False
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def today():
    """Today's date in the signed-in business's timezone (status depends on it)."""
    override = current_app.config.get("TODAY")
    if override:
        return date.fromisoformat(override)
    tz_name = g.user["timezone"] if g.get("user") is not None else "UTC"
    try:
        return datetime.now(ZoneInfo(tz_name)).date()
    except (ZoneInfoNotFoundError, ValueError):
        return datetime.now(timezone.utc).date()


def parse_date(value):
    """Parse YYYY-MM-DD (or other ISO forms) within a sane range; None if invalid."""
    if isinstance(value, date):
        return value
    try:
        d = date.fromisoformat((value or "").strip())
    except (ValueError, TypeError):
        return None
    return d if MIN_DATE <= d <= MAX_DATE else None


def parse_amount(value, currency="USD"):
    """Parse a user-entered amount into integer cents, or None if invalid."""
    raw = (value or "").strip().replace(",", "")
    for symbol in CURRENCIES.values():
        raw = raw.replace(symbol, "")
    raw = raw.strip()
    if not raw or len(raw) > 20:
        return None
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        return None
    max_places = 0 if currency in ZERO_DECIMAL_CURRENCIES else 2
    if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -max_places:
        return None
    cents = int(amount * 100)
    if cents > MAX_AMOUNT_CENTS:
        return None
    return cents


def amount_input(cents, currency="USD"):
    return f"{cents // 100}" if currency in ZERO_DECIMAL_CURRENCIES else f"{cents / 100:.2f}"


def format_money(cents, currency="USD"):
    symbol = CURRENCIES.get(currency, currency + " ")
    if currency in ZERO_DECIMAL_CURRENCIES:
        return f"{symbol}{cents // 100:,}"
    return f"{symbol}{cents / 100:,.2f}"


def format_date(value):
    d = value if isinstance(value, date) else parse_date((value or "")[:10])
    return f"{d:%b} {d.day}, {d.year}" if d else ""


def invoice_status(inv, ref=None):
    """Derive status from stored fields. Paid wins; otherwise compare due date to today.

    This and invoices.status_clause() are the only places status is decided; tests
    check they always agree.
    """
    if inv["paid_at"]:
        return "paid"
    ref = ref or today()
    due = parse_date(inv["due_date"])
    if due < ref:
        return "overdue"
    if due <= ref + timedelta(days=DUE_SOON_DAYS):
        return "due_soon"
    return "open"


def days_overdue(inv, ref=None):
    ref = ref or today()
    return max(0, (ref - parse_date(inv["due_date"])).days)


def days_until_due(inv, ref=None):
    ref = ref or today()
    return (parse_date(inv["due_date"]) - ref).days


def due_relative(inv, ref=None):
    """Human wording like 'due today', 'due tomorrow', '3 days overdue'."""
    days = days_until_due(inv, ref)
    if days == 0:
        return "Due today"
    if days == 1:
        return "Due tomorrow"
    if days > 1:
        return f"Due in {days} days"
    return f"{-days} day{'s' if days != -1 else ''} overdue"


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def local_now_iso():
    """The business's local date and time, on the same date as today().

    Use for timestamps the owner sees as dates (e.g. follow-up history): a UTC timestamp can fall on a
    different calendar day than the business's own today(), so it would show the wrong date.
    """
    tz_name = g.user["timezone"] if g.get("user") is not None else "UTC"
    try:
        now = datetime.now(ZoneInfo(tz_name))
    except (ZoneInfoNotFoundError, ValueError):
        now = datetime.now(timezone.utc)
    return datetime.combine(today(), now.time()).isoformat(timespec="seconds")


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            # Only GET pages make sense to return to after logging in.
            # script_root keeps the address prefix when the app runs under one (e.g. /invoicechaser)
            target = (request.script_root + request.full_path).rstrip("?") if request.method == "GET" else None
            return redirect(url_for("auth.login", next=target))
        return view(*args, **kwargs)
    return wrapped


def safe_next(target):
    """Only allow same-site relative redirects (prevents open redirect)."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None


def like_pattern(q):
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class RateLimiter:
    """In-process sliding-window limiter (one per app). For multiple processes, use Redis."""

    def __init__(self, limit, window_seconds):
        self.limit, self.window = limit, window_seconds
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key, now):
        hits = self._hits[key]
        while hits and hits[0] < now - self.window:
            hits.popleft()
        return hits

    def blocked(self, key):
        with self._lock:
            if len(self._hits) > 10_000:  # bound memory under spraying
                now = time.monotonic()
                for k in [k for k, v in self._hits.items() if not v or v[-1] < now - self.window]:
                    del self._hits[k]
            return len(self._prune(key, time.monotonic())) >= self.limit

    def hit(self, key):
        with self._lock:
            now = time.monotonic()
            self._prune(key, now).append(now)

    def reset(self, key):
        with self._lock:
            self._hits.pop(key, None)


def limiter(name, limit, window_seconds):
    store = current_app.extensions.setdefault("rate_limiters", {})
    if name not in store:
        store[name] = RateLimiter(limit, window_seconds)
    return store[name]
