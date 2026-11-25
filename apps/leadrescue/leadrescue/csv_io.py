"""CSV import/export for leads.

Import: the upload is read in memory (never written to disk), size-limited by
MAX_CONTENT_LENGTH, row-limited, decoded strictly as UTF-8, and every row is
validated with the same LeadForm used by the web UI. Import is all-or-nothing.

Export: cells that could be interpreted as spreadsheet formulas are prefixed
with an apostrophe to prevent CSV/formula injection.
"""
import csv
import io

from werkzeug.datastructures import MultiDict

from .forms import LeadForm
from .models import LEAD_SOURCES, LEAD_STATUSES, SOURCE_LABELS, STATUS_LABELS, Lead

EXPORT_COLUMNS = [
    "first_name", "last_name", "phone", "email", "address", "city", "state", "zip_code",
    "source", "status", "job_type", "estimated_value", "last_contact_date", "next_follow_up_date",
    "notes", "is_demo",
]
IMPORT_COLUMNS = [c for c in EXPORT_COLUMNS if c != "is_demo"]
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

_STATUS_BY_LABEL = {label.lower(): key for key, label in LEAD_STATUSES}
_SOURCE_BY_LABEL = {label.lower(): key for key, label in LEAD_SOURCES}


class CSVImportError(Exception):
    pass


def sanitize_cell(value) -> str:
    text = "" if value is None else str(value)
    if text.startswith(FORMULA_PREFIXES):
        return "'" + text
    return text


def export_leads(leads: list[Lead]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    writer.writerow(EXPORT_COLUMNS)
    for lead in leads:
        row = []
        for col in EXPORT_COLUMNS:
            value = getattr(lead, col)
            if value is None:
                value = ""
            elif col in ("last_contact_date", "next_follow_up_date"):
                value = value.isoformat()
            elif col == "is_demo":
                value = "yes" if value else "no"
            row.append(sanitize_cell(value))
        writer.writerow(row)
    return buf.getvalue()


def _normalise_choice(value: str, keys, by_label) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    lowered = v.lower()
    if lowered in keys:
        return lowered
    return by_label.get(lowered, lowered.replace(" ", "_"))


def parse_import(raw: bytes, max_rows: int):
    """Return (rows, errors). rows are cleaned dicts ready for Lead(**row)."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CSVImportError("File must be UTF-8 encoded text (save as 'CSV UTF-8' in Excel).")
    if "\x00" in text:
        raise CSVImportError("File doesn't look like a text CSV.")

    try:
        return _parse_rows(text, max_rows)
    except csv.Error as exc:
        # Malformed quoting, a field over the csv module's size limit, etc.
        raise CSVImportError(f"The file isn't a valid CSV ({exc}). Check for unclosed quotes or very long cells.")


def _parse_rows(text: str, max_rows: int):
    # strict=True rejects malformed quoting instead of silently guessing.
    reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
    if not reader.fieldnames:
        raise CSVImportError("The file is empty or has no header row.")
    headers = [(h or "").strip().lower() for h in reader.fieldnames]
    reader.fieldnames = headers
    if "first_name" not in headers:
        raise CSVImportError("Missing required column 'first_name'. Download the template to see the expected columns.")
    unknown = [h for h in headers if h and h not in EXPORT_COLUMNS]
    if unknown:
        raise CSVImportError(f"Unknown column(s): {', '.join(unknown[:5])}.")
    duplicates = sorted({h for h in headers if h and headers.count(h) > 1})
    if duplicates:
        raise CSVImportError(f"Column(s) appear more than once: {', '.join(duplicates)}.")

    status_keys = set(STATUS_LABELS)
    source_keys = set(SOURCE_LABELS)
    rows, errors = [], []
    for line_no, raw_row in enumerate(reader, start=2):
        if line_no - 1 > max_rows:
            raise CSVImportError(f"Too many rows. The limit is {max_rows} per import.")
        if None in raw_row:
            errors.append((line_no, "Row has more values than there are columns."))
            continue
        values = {k: (v or "").strip() for k, v in raw_row.items() if k in IMPORT_COLUMNS}
        if not any(values.values()):
            continue  # skip blank lines
        values["status"] = _normalise_choice(values.get("status", ""), status_keys, _STATUS_BY_LABEL) or "new"
        values["source"] = _normalise_choice(values.get("source", ""), source_keys, _SOURCE_BY_LABEL) or "other"
        if values.get("estimated_value"):
            values["estimated_value"] = values["estimated_value"].replace("$", "").replace(",", "")

        form = LeadForm(formdata=MultiDict(values), meta={"csrf": False})
        form.assigned_to_id.choices = [(0, "Unassigned")]
        if not form.validate():
            msgs = [f"{form[name].label.text}: {errs[0]}" for name, errs in form.errors.items()]
            errors.append((line_no, "; ".join(msgs)))
            continue
        rows.append({
            name: (form[name].data if form[name].data is not None else None)
            for name in IMPORT_COLUMNS
        })
    return rows, errors
