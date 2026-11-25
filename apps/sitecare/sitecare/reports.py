"""Monthly maintenance report: data gathering, plain-text summary and CSV.

Definitions (also shown on the report itself):
- Completed: recorded as completed with a completion date inside the month.
- Overdue: still unrecorded, due on or before month end and before today.
- Scheduled later: still unrecorded, due today or later but within the month.
- Skipped: due inside the month and deliberately skipped (with a reason).
- Issues: reported by month end and not resolved before month start; status is
  shown as it was at month end.
"""
import calendar
import csv
import hashlib
import io
import json
from datetime import date, timedelta

from .constants import (
    CATEGORIES, ISSUE_STATUSES, RESULTS, SEVERITIES, SEVERITY_ORDER_SQL, VERIFICATION, VERIFICATION_NOTE,
)
from .helpers import OCCURRENCE_SELECT, verification_key
from .scheduling import ensure_occurrences

MANUAL_NOTICE = (
    "All results in this report were recorded manually by the maintainer. SiteCare Desk does not run "
    "or verify backups, uptime monitoring or security scans. " + VERIFICATION_NOTE
)


MAX_PERIOD_DAYS = 366


def month_bounds(year, month):
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _is_whole_month(start, end):
    return start.day == 1 and end == month_bounds(start.year, start.month)[1]


def period_label(start, end):
    if _is_whole_month(start, end):
        return start.strftime("%B %Y")
    return f"{start.strftime('%d %b %Y')} - {end.strftime('%d %b %Y')}"


def period_key(start, end):
    return start.strftime("%Y-%m") if _is_whole_month(start, end) else f"{start.isoformat()}_{end.isoformat()}"


def parse_period(args, today):
    """Read ?month=YYYY-MM or ?start=&end= (YYYY-MM-DD). Returns ((start, end), None) or (None, error)."""
    from .validation import parse_month

    start_raw, end_raw = (args.get("start") or "").strip(), (args.get("end") or "").strip()
    if start_raw or end_raw:
        try:
            start, end = date.fromisoformat(start_raw), date.fromisoformat(end_raw)
        except ValueError:
            return None, "Enter both a start and an end date as YYYY-MM-DD."
        if not (2000 <= start.year <= 2100 and 2000 <= end.year <= 2100):
            return None, "Dates must be between 2000 and 2100."
        if start > end:
            return None, "The start date must be on or before the end date."
        if (end - start).days + 1 > MAX_PERIOD_DAYS:
            return None, f"A report can cover at most {MAX_PERIOD_DAYS} days."
        return (start, end), None
    month = parse_month(args.get("month") or today.strftime("%Y-%m"))
    if not month:
        return None, "Choose a valid month (YYYY-MM)."
    return month_bounds(*month), None


def _occurrences(db, account_id, where, params):
    rows = []
    for r in db.execute(OCCURRENCE_SELECT + " WHERE o.account_id = ? AND " + where, (account_id, *params)).fetchall():
        row = dict(r)
        row["verification"] = verification_key(row)
        rows.append(row)
    return rows


def _issue_rows(db, account_id, website_id, s, e):
    rows = db.execute(
        "SELECT i.*, t.title AS found_task, o.completed_on AS found_on FROM issues i "
        "LEFT JOIN occurrences o ON o.id = i.occurrence_id AND o.account_id = i.account_id "
        "LEFT JOIN tasks t ON t.id = o.task_id "
        "WHERE i.account_id = ? AND i.website_id = ? AND i.reported_on <= ? AND (i.resolved_on IS NULL OR i.resolved_on >= ?) "
        f"ORDER BY {SEVERITY_ORDER_SQL.format(col='i.severity')}, i.reported_on",
        (account_id, website_id, e, s),
    ).fetchall()
    issues = []
    for r in rows:
        i = dict(r)
        closed_by_end = i["resolved_on"] is not None and i["resolved_on"] <= e
        if closed_by_end:
            i["status_at_end"] = i["status"]
        elif i["status"] in ("resolved", "wont_fix"):
            i["status_at_end"] = "open"  # closed after the period ended
        else:
            i["status_at_end"] = i["status"]
        i["open_at_end"] = i["status_at_end"] in ("open", "in_progress")
        i["closed_after_period"] = i["resolved_on"] is not None and i["resolved_on"] > e
        issues.append(i)
    return issues


def build_report(db, website_id, year, month, today, *, account_id):
    """Monthly report for one website (kept for the per-website report pages)."""
    start, end = month_bounds(year, month)
    return build_period_report(db, website_id, start, end, today, account_id=account_id)


def build_period_report(db, website_id, start, end, today, *, account_id):
    """Report for one website of `account_id`; None if the website isn't that account's."""
    website = db.execute(
        "SELECT w.*, c.name AS client_name, c.contact_name, c.contact_email "
        "FROM websites w JOIN clients c ON c.id = w.client_id WHERE w.id = ? AND w.account_id = ?",
        (website_id, account_id),
    ).fetchone()
    if website is None:
        return None
    # Make sure everything scheduled within the month exists (bounded to a year ahead).
    ensure_occurrences(db, min(max(end, today), today + timedelta(days=366)), account_id=account_id)

    s, e, t = start.isoformat(), end.isoformat(), today.isoformat()
    completed = _occurrences(
        db, account_id, "t.website_id = ? AND o.status = 'completed' AND o.completed_on BETWEEN ? AND ? "
        "ORDER BY o.completed_on, t.title", (website_id, s, e),
    )
    overdue = _occurrences(
        db, account_id, "t.website_id = ? AND o.status = 'pending' AND o.due_date <= ? AND o.due_date < ? "
        "ORDER BY o.due_date, t.title", (website_id, e, t),
    )
    scheduled_later = _occurrences(
        db, account_id, "t.website_id = ? AND o.status = 'pending' AND o.due_date BETWEEN ? AND ? "
        "ORDER BY o.due_date, t.title", (website_id, max(s, t), e),
    )
    skipped = _occurrences(
        db, account_id, "t.website_id = ? AND o.status = 'skipped' AND o.due_date BETWEEN ? AND ? "
        "ORDER BY o.due_date, t.title", (website_id, s, e),
    )
    issues = _issue_rows(db, account_id, website_id, s, e)

    # Everything that was due inside the month, by its current status.
    due_in_month = {"completed": 0, "skipped": 0, "pending": 0}
    for row in db.execute(
        "SELECT o.status, COUNT(*) AS n FROM occurrences o JOIN tasks t ON t.id = o.task_id "
        "WHERE o.account_id = ? AND t.website_id = ? AND o.due_date BETWEEN ? AND ? GROUP BY o.status",
        (account_id, website_id, s, e),
    ):
        due_in_month[row["status"]] = row["n"]
    due_in_month["total"] = sum(due_in_month.values())

    categories = {}
    for key, rows in (("completed", completed), ("overdue", overdue), ("skipped", skipped)):
        for o in rows:
            cat = categories.setdefault(o["category"], {"completed": 0, "problems": 0, "overdue": 0, "skipped": 0})
            cat[key] += 1
            if key == "completed" and o["result"] in ("issues_found", "failed"):
                cat["problems"] += 1
    category_rows = [
        {"category": k, "label": CATEGORIES.get(k, k), **v}
        for k, v in sorted(categories.items(), key=lambda kv: list(CATEGORIES).index(kv[0]) if kv[0] in CATEGORIES else 99)
    ]

    counts = {
        "completed": len(completed),
        "results": {key: sum(1 for r in completed if r["result"] == key) for key in RESULTS},
        "overdue": len(overdue),
        "scheduled_later": len(scheduled_later),
        "outstanding": len(overdue) + len(scheduled_later),
        "skipped": len(skipped),
        "issues": len(issues),
        "issues_open": sum(1 for i in issues if i["open_at_end"]),
        "issues_resolved_in_month": sum(
            1 for i in issues if i["status"] == "resolved" and i["resolved_on"] and s <= i["resolved_on"] <= e
        ),
        "issues_wont_fix_in_month": sum(
            1 for i in issues if i["status"] == "wont_fix" and i["resolved_on"] and s <= i["resolved_on"] <= e
        ),
        "evidence": sum(r["attachment_count"] for r in completed),
        "manual": sum(1 for r in completed if r["verification"] == "manual"),
        "manual_evidence": sum(1 for r in completed if r["verification"] == "manual_evidence"),
        "verified": 0,  # no integrations exist, so nothing is ever automatically verified
    }
    attention = {
        "overdue": overdue,
        "issues": [i for i in issues if i["open_at_end"] and i["severity"] in ("high", "critical")],
        "problems": [o for o in completed if o["result"] in ("issues_found", "failed")],
    }
    report = {
        "website": website,
        "month_key": period_key(start, end),
        "period_label": period_label(start, end),
        "start": start,
        "end": end,
        "today": today,
        "period_open": end >= today,
        "completed": completed,
        "overdue": overdue,
        "scheduled_later": scheduled_later,
        "skipped": skipped,
        "issues": issues,
        "due_in_month": due_in_month,
        "categories": category_rows,
        "attention": attention,
        "counts": counts,
        "notice": MANUAL_NOTICE,
    }
    report["summary"] = summary_text(report)
    return report


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def summary_text(r):
    w, c, d = r["website"], r["counts"], r["due_in_month"]
    result_bits = ", ".join(f"{RESULTS[k]}: {n}" for k, n in c["results"].items() if n)
    lines = [
        f"Website maintenance summary - {w['name']} ({w['domain']})",
        f"Client: {w['client_name']}",
        f"Period: {r['period_label']} (generated {r['today'].isoformat()})",
        "",
        f"Work recorded this period: {_plural(c['completed'], 'task')}" + (f" ({result_bits})" if result_bits else ""),
        f"How results are known: {_plural(c['manual'] + c['manual_evidence'], 'manual record')} "
        f"({c['manual_evidence']} with evidence attached); {c['verified']} verified automatically",
        f"Due this period: {d['total']} - {d['completed']} completed, {d['skipped']} skipped, "
        f"{d['pending']} not recorded yet",
        f"Overdue (not recorded): {c['overdue']}",
    ]
    if r["period_open"]:
        lines.append(f"Scheduled later this period: {c['scheduled_later']}")
    lines.append(
        f"Issues during period: {c['issues']} ({c['issues_open']} open at period end, "
        f"{c['issues_resolved_in_month']} resolved in period)"
    )
    if r["completed"]:
        lines += ["", "Work completed (manual records):"]
        for o in r["completed"]:
            who = f" by {o['recorded_by']}" if o["recorded_by"] else ""
            ev = f" [{_plural(o['attachment_count'], 'evidence file')}]" if o["attachment_count"] else ""
            lines.append(f"- {o['completed_on']} {o['title']}: {RESULTS.get(o['result'], o['result'])}{who}{ev}")
    if r["overdue"]:
        lines += ["", "Overdue - not recorded:"]
        lines += [f"- {o['title']} (due {o['due_date']})" for o in r["overdue"]]
    if r["issues"]:
        lines += ["", "Issues:"]
        for i in r["issues"]:
            extra = f", closed {i['resolved_on']}" if i["closed_after_period"] else ""
            lines.append(f"- [{SEVERITIES[i['severity']]}] {i['title']}: {ISSUE_STATUSES[i['status_at_end']]} at period end{extra}")
    lines += ["", MANUAL_NOTICE]
    return "\n".join(lines)


def _cell(value):
    """Neutralise spreadsheet formula injection."""
    text = "" if value is None else str(value)
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


REPORT_COLUMNS = [
    "website", "domain", "client", "period", "section", "item", "category", "due_date", "completed_on",
    "reported_on", "resolved_on", "status", "result", "severity", "recorded_by", "verification", "notes",
    "evidence_files",
]


def report_csv(r):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=REPORT_COLUMNS)
    writer.writeheader()
    for row in report_rows(r):
        writer.writerow({k: _cell(v) for k, v in row.items()})
    return buf.getvalue()


def report_rows(r):
    w = r["website"]
    base = {"website": w["name"], "domain": w["domain"], "client": w["client_name"], "period": r["month_key"]}

    def occ_row(section, o, status):
        return {
            **base, "section": section, "item": o["title"], "category": CATEGORIES.get(o["category"], o["category"]),
            "due_date": o["due_date"], "completed_on": o["completed_on"] or "", "status": status,
            "result": RESULTS.get(o["result"], "") if o["result"] else "", "recorded_by": o["recorded_by"],
            "verification": VERIFICATION[o["verification"]] if section in ("completed", "skipped") else "",
            "notes": o["notes"], "evidence_files": o["attachment_count"],
        }

    rows = [occ_row("completed", o, "Completed") for o in r["completed"]]
    rows += [occ_row("overdue", o, "Overdue - not recorded") for o in r["overdue"]]
    rows += [occ_row("scheduled_later", o, "Scheduled") for o in r["scheduled_later"]]
    rows += [occ_row("skipped", o, "Skipped") for o in r["skipped"]]
    for i in r["issues"]:
        notes = i["description"]
        if i["resolution_notes"]:
            notes = f"{notes}\nResolution: {i['resolution_notes']}".strip()
        rows.append({
            **base, "section": "issue", "item": i["title"], "reported_on": i["reported_on"],
            "resolved_on": i["resolved_on"] or "", "status": f"{ISSUE_STATUSES[i['status_at_end']]} at period end",
            "severity": SEVERITIES[i["severity"]], "notes": notes,
        })
    return rows


def history_csv(rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "domain", "due_date", "task", "category", "status", "completed_on", "result", "recorded_by",
        "verification", "notes", "evidence_files", "recorded_at",
    ])
    for o in rows:
        writer.writerow(map(_cell, [
            o["domain"], o["due_date"], o["title"], CATEGORIES.get(o["category"], o["category"]), o["status"],
            o["completed_on"], RESULTS.get(o["result"], o["result"] or ""), o["recorded_by"],
            VERIFICATION[verification_key(o)], o["notes"], o["attachment_count"], o["recorded_at"],
        ]))
    return buf.getvalue()


# ---------------------------------------------------------------- client reports

_TOTAL_KEYS = (
    "completed", "overdue", "scheduled_later", "skipped", "issues", "issues_open", "issues_resolved_in_month",
    "issues_wont_fix_in_month", "evidence", "manual", "manual_evidence", "verified",
)


def build_client_report(db, client_id, start, end, today, website_id=None, *, account_id):
    """Report for every website of one client (or one of its websites) over any period.

    Returns None when the client isn't `account_id`'s or `website_id` doesn't belong to the
    client, so a tampered URL can never pull another client's or account's data in.
    """
    client = db.execute("SELECT * FROM clients WHERE id = ? AND account_id = ?", (client_id, account_id)).fetchone()
    if client is None:
        return None
    sql, params = "SELECT id FROM websites WHERE client_id = ? AND account_id = ?", [client_id, account_id]
    if website_id is not None:
        sql += " AND id = ?"
        params.append(website_id)
    site_ids = [r["id"] for r in db.execute(sql + " ORDER BY name COLLATE NOCASE", params)]
    if website_id is not None and not site_ids:
        return None
    sites = [build_period_report(db, sid, start, end, today, account_id=account_id) for sid in site_ids]
    totals = {k: sum(r["counts"][k] for r in sites) for k in _TOTAL_KEYS}
    totals["results"] = {k: sum(r["counts"]["results"][k] for r in sites) for k in RESULTS}
    report = {
        "client": client,
        "sites": sites,
        "website_id": website_id,
        "start": start,
        "end": end,
        "today": today,
        "period_open": end >= today,
        "period_label": period_label(start, end),
        "period_key": period_key(start, end),
        "totals": totals,
        "notice": MANUAL_NOTICE,
    }
    report["summary"] = client_summary_text(report)
    report["fingerprint"] = fingerprint(report)
    return report


def client_summary_text(cr):
    t = cr["totals"]
    lines = [
        f"Maintenance summary - {cr['client']['name']}",
        f"Period: {cr['period_label']} (generated {cr['today'].isoformat()})",
        f"Websites covered: {len(cr['sites'])}",
        "",
        f"Work recorded: {_plural(t['completed'], 'task')} across all websites; "
        f"{t['verified']} verified automatically",
        f"Overdue (not recorded): {t['overdue']}",
        f"Issues during period: {t['issues']} ({t['issues_open']} open at period end)",
    ]
    if not cr["sites"]:
        lines += ["", "This client has no websites yet."]
    for r in cr["sites"]:
        lines += ["", "-" * 60] + [line for line in r["summary"].split("\n") if line != MANUAL_NOTICE]
    lines += ["", MANUAL_NOTICE]
    return "\n".join(lines)


def fingerprint(cr):
    """Hash of the saved records behind a report (not of date-dependent labels like 'overdue').

    Used to tell whether records for a period changed after a report was saved.
    """
    payload = []
    for r in cr["sites"]:
        payload.append(["site", r["website"]["id"], r["website"]["name"], r["website"]["domain"]])
        for o in r["completed"] + r["skipped"]:
            payload.append([o["id"], o["status"], o["completed_on"], o["result"], o["notes"],
                            o["recorded_by"], o["attachment_count"]])
        for i in r["issues"]:
            payload.append([i["id"], i["title"], i["description"], i["severity"], i["status"],
                            i["reported_on"], i["resolved_on"], i["resolution_notes"]])
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def client_report_csv(cr):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=REPORT_COLUMNS)
    writer.writeheader()
    for r in cr["sites"]:
        for row in report_rows(r):
            writer.writerow({k: _cell(v) for k, v in row.items()})
    return buf.getvalue()
