"""Form input validation. Each parser returns a clean value or records an error."""
import re
from datetime import date
from decimal import Decimal, InvalidOperation

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Form:
    """Collects cleaned values and per-field errors for one submitted form."""

    def __init__(self, data):
        self.data = data
        self.errors = {}
        self.values = {}

    @property
    def ok(self):
        return not self.errors

    def _raw(self, name):
        return (self.data.get(name) or "").strip()

    def text(self, name, label, required=True, max_len=200):
        value = self._raw(name)
        if required and not value:
            self.errors[name] = f"{label} is required."
        elif len(value) > max_len:
            self.errors[name] = f"{label} must be {max_len} characters or fewer."
        self.values[name] = value
        return value

    def email(self, name, label="Email", required=True):
        value = self.text(name, label, required=required, max_len=254).lower()
        if value and not EMAIL_RE.match(value):
            self.errors[name] = f"{label} is not a valid email address."
        self.values[name] = value
        return value

    def password(self, name, label="Password", min_len=10):
        value = self.data.get(name) or ""
        if len(value) < min_len:
            self.errors[name] = f"{label} must be at least {min_len} characters."
        elif len(value) > 200:
            self.errors[name] = f"{label} is too long."
        return value

    def decimal(self, name, label, required=True, min_value=None, max_value=None, places=2):
        raw = self._raw(name).replace(",", "").replace("$", "").replace("%", "")
        self.values[name] = raw
        if not raw:
            if required:
                self.errors[name] = f"{label} is required."
            return None
        try:
            value = Decimal(raw)
        except InvalidOperation:
            self.errors[name] = f"{label} must be a number."
            return None
        if not value.is_finite():
            self.errors[name] = f"{label} must be a number."
            return None
        if value.as_tuple().exponent < -places:
            self.errors[name] = f"{label} can have at most {places} decimal places."
            return None
        if min_value is not None and value < Decimal(min_value):
            self.errors[name] = f"{label} must be at least {min_value}."
            return None
        if max_value is not None and value > Decimal(max_value):
            self.errors[name] = f"{label} must be at most {max_value}."
            return None
        return value

    def integer(self, name, label, required=True, min_value=None, max_value=None):
        value = self.decimal(name, label, required=required, min_value=min_value,
                             max_value=max_value, places=0)
        return int(value) if value is not None else None

    def date(self, name, label, required=True):
        raw = self._raw(name)
        self.values[name] = raw
        if not raw:
            if required:
                self.errors[name] = f"{label} is required."
            return None
        try:
            return date.fromisoformat(raw).isoformat()
        except ValueError:
            self.errors[name] = f"{label} must be a valid date."
            return None

    def choice(self, name, label, choices):
        value = self._raw(name)
        self.values[name] = value
        if value not in choices:
            self.errors[name] = f"Choose a valid {label.lower()}."
            return None
        return value

    def checkbox(self, name):
        value = self.data.get(name) in ("on", "1", "true", "yes")
        self.values[name] = value
        return value


def parse_lines(data, labor_rate_ids, max_lines=50):
    """Parse repeated line-item fields from the ticket form.

    Fields per row: line_kind, line_labor (labor rate id, labor rows only),
    line_desc, line_qty, line_unit, line_rate (unit price, optional).
    Returns (lines, errors). Blank rows are skipped.
    """
    kinds = data.getlist("line_kind")
    labors = data.getlist("line_labor")
    descs = data.getlist("line_desc")
    qtys = data.getlist("line_qty")
    units = data.getlist("line_unit")
    rates = data.getlist("line_rate")
    n = max(len(kinds), len(descs), len(qtys), len(units), len(rates), len(labors))
    lines, errors = [], []

    def at(seq, i):
        return (seq[i] if i < len(seq) else "").strip()

    for i in range(n):
        kind, desc, qty_raw = at(kinds, i), at(descs, i), at(qtys, i)
        unit, rate_raw = at(units, i), at(rates, i).replace("$", "").replace(",", "")
        labor_raw = at(labors, i)
        if not desc and not qty_raw and not rate_raw:
            continue
        row = i + 1
        if kind not in ("labor", "material", "equipment"):
            errors.append(f"Line {row}: choose labor, material or equipment.")
            continue
        labor_rate_id = None
        if kind == "labor":
            if not labor_raw.isdigit() or int(labor_raw) not in labor_rate_ids:
                errors.append(f"Line {row}: choose a worker type.")
            else:
                labor_rate_id = int(labor_raw)
            unit = "hr"
        elif not desc:
            errors.append(f"Line {row}: description is required.")
        if len(desc) > 200:
            errors.append(f"Line {row}: description is too long.")
        try:
            qty = Decimal(qty_raw)
            if not qty.is_finite() or qty <= 0 or qty > 100000 or qty.as_tuple().exponent < -2:
                raise InvalidOperation
        except InvalidOperation:
            errors.append(f"Line {row}: quantity must be a positive number with up to 2 decimals.")
            qty = None
        rate = None
        if rate_raw:
            try:
                rate = Decimal(rate_raw)
                if not rate.is_finite() or rate < 0 or rate > 1000000 or rate.as_tuple().exponent < -2:
                    raise InvalidOperation
            except InvalidOperation:
                errors.append(f"Line {row}: rate must be a non-negative amount with up to 2 decimals.")
                rate = None
        lines.append({"kind": kind, "labor_rate_id": labor_rate_id, "description": desc,
                      "qty": qty, "qty_raw": qty_raw, "unit": unit[:20], "rate": rate,
                      "rate_raw": rate_raw})
    if len(lines) > max_lines:
        errors.append(f"A ticket can have at most {max_lines} lines.")
    return lines, errors
