"""Domain logic shared by the freelancer views, client portal, demo seeding and tests."""
import hashlib
import json
import re
import secrets
from datetime import date, datetime, timedelta, timezone

CR_STATUSES = ("draft", "submitted", "approved", "rejected", "withdrawn", "completed")
# Allowed change request status transitions (who may make each one is enforced by the callers:
# approved/rejected need a client decision record; the rest are the freelancer's).
CR_TRANSITIONS = {
    "draft": ("submitted", "withdrawn"),
    "submitted": ("approved", "rejected", "withdrawn"),
    "approved": ("completed",),
    "rejected": (),
    "withdrawn": (),
    "completed": (),
}
CLIENT_DECISIONS = ("approved", "rejected")
FREELANCER_TRANSITIONS = {("draft", "submitted"), ("draft", "withdrawn"), ("submitted", "withdrawn"),
                          ("approved", "completed")}
PROJECT_STATUSES = ("active", "on_hold", "completed")

METHOD_LABELS = {
    "client_review_link": (
        "Electronic acknowledgement by the client via secure review link "
        "(typed name + confirmation checkbox). Not a verified e-signature."
    ),
    "recorded_by_freelancer": (
        "Recorded by the freelancer from off-platform evidence (see note). "
        "Not entered by the client."
    ),
    "demo_sample": "Sample demo data. Not a real approval.",
}
METHOD_SHORT = {
    "client_review_link": "entered by the client on their review link",
    "recorded_by_freelancer": "recorded by the freelancer from off-platform evidence",
    "demo_sample": "sample data, not a real approval",
}
CR_LABELS = {
    "draft": "Draft, not sent",
    "submitted": "Submitted, awaiting decision",
    "approved": "Approved",
    "completed": "Completed",
    "rejected": "Rejected by client",
    "withdrawn": "Withdrawn",
}
DECISION_LABELS = {
    "approved": "Approved",
    "changes_requested": "Changes requested",
    "rejected": "Rejected",
}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class ValidationError(Exception):
    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = errors


class Form:
    """Tiny form validator: collects errors instead of raising on the first one."""

    def __init__(self, data):
        self.data = data
        self.errors = []

    def text(self, name, label, required=True, max_len=200):
        value = (self.data.get(name) or "").strip()
        if required and not value:
            self.errors.append(f"{label} is required.")
        elif len(value) > max_len:
            self.errors.append(f"{label} must be at most {max_len} characters.")
        return value

    def email(self, name, label, required=False):
        value = self.text(name, label, required=required, max_len=254)
        if value and not EMAIL_RE.match(value):
            self.errors.append(f"{label} must be a valid email address.")
        return value or None

    def date(self, name, label, required=False):
        value = self.text(name, label, required=required, max_len=10)
        if not value:
            return None
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            self.errors.append(f"{label} must be a date (YYYY-MM-DD).")
            return None

    def number(self, name, label, required=False, minimum=0, maximum=1_000_000, integer=False):
        raw = (self.data.get(name) or "").strip()
        if not raw:
            if required:
                self.errors.append(f"{label} is required.")
            return None
        try:
            value = int(raw) if integer else float(raw)
        except ValueError:
            self.errors.append(f"{label} must be a {'whole ' if integer else ''}number.")
            return None
        if value != value or not (minimum <= value <= maximum):  # NaN check included
            self.errors.append(f"{label} must be between {minimum} and {maximum}.")
            return None
        return value

    def choice(self, name, label, choices):
        value = (self.data.get(name) or "").strip()
        if value not in choices:
            self.errors.append(f"{label} is invalid.")
            return None
        return value

    def check(self):
        if self.errors:
            raise ValidationError(self.errors)


# ---------------------------------------------------------------- history


def log_event(db, project_id, kind, message, actor):
    db.execute(
        # The account always comes from the project, so history can never land in another account.
        "INSERT INTO events (account_id, project_id, kind, message, actor, created_at)"
        " VALUES ((SELECT account_id FROM projects WHERE id = ?), ?, ?, ?, ?, ?)",
        (project_id, project_id, kind, message, actor, now()),
    )


# ---------------------------------------------------------------- scope


def current_scope(db, project_id):
    """The working (draft) scope, in the same shape as a stored snapshot."""
    p = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    return {
        "terms": {
            "name": p["name"],
            "description": p["description"] or "",
            "start_date": p["start_date"],
            "deadline": p["deadline"],
            "revision_allowance": p["revision_allowance"],
        },
        "deliverables": [
            {
                "id": r["id"],
                "title": r["title"],
                "description": r["description"] or "",
                "revision_allowance": r["revision_allowance"],
                "due_date": r["due_date"],
            }
            for r in db.execute(
                "SELECT * FROM deliverables WHERE project_id = ? AND archived_at IS NULL ORDER BY id",
                (project_id,),
            )
        ],
        "exclusions": [
            {"id": r["id"], "text": r["text"]}
            for r in db.execute(
                "SELECT * FROM exclusions WHERE project_id = ? AND archived_at IS NULL ORDER BY id",
                (project_id,),
            )
        ],
        "milestones": [
            {"id": r["id"], "title": r["title"], "due_date": r["due_date"]}
            for r in db.execute(
                "SELECT * FROM milestones WHERE project_id = ? AND archived_at IS NULL "
                "ORDER BY due_date IS NULL, due_date, id",
                (project_id,),
            )
        ],
    }


def latest_version(db, project_id):
    return db.execute(
        "SELECT * FROM scope_versions WHERE project_id = ? ORDER BY version_no DESC LIMIT 1",
        (project_id,),
    ).fetchone()


def snapshot(version_row):
    return json.loads(version_row["snapshot_json"]) if version_row else None


def _canonical(scope):
    return json.dumps(scope, sort_keys=True)


def draft_differs(db, project_id):
    latest = latest_version(db, project_id)
    if latest is None:
        return True
    return _canonical(current_scope(db, project_id)) != _canonical(snapshot(latest))


def publish_version(db, project_id, change_note, actor):
    """Store the current scope as a new immutable version. Returns the version number."""
    scope = current_scope(db, project_id)
    if not scope["deliverables"]:
        raise ValidationError(["Add at least one deliverable before publishing a scope version."])
    latest = latest_version(db, project_id)
    if latest is not None and _canonical(scope) == _canonical(snapshot(latest)):
        raise ValidationError(["Nothing has changed since the latest published version."])
    version_no = (latest["version_no"] if latest else 0) + 1
    db.execute(
        "INSERT INTO scope_versions (account_id, project_id, version_no, snapshot_json, change_note, created_by,"
        " created_at) VALUES ((SELECT account_id FROM projects WHERE id = ?), ?, ?, ?, ?, ?, ?)",
        (project_id, project_id, version_no, _canonical(scope), change_note or None, actor, now()),
    )
    log_event(db, project_id, "scope_published", f"Published scope version {version_no}.", actor)
    return version_no


def diff_scopes(old, new):
    """Human-readable differences between two snapshots (old may be None)."""
    if old is None:
        return {"first": True, "terms": [], "sections": {}}
    labels = {
        "name": "Project name",
        "description": "Description",
        "start_date": "Start date",
        "deadline": "Deadline",
        "revision_allowance": "Revisions per deliverable",
    }
    terms = [
        (labels[k], old["terms"].get(k), new["terms"].get(k))
        for k in labels
        if old["terms"].get(k) != new["terms"].get(k)
    ]
    sections = {}
    for section, key in (("deliverables", "title"), ("exclusions", "text"), ("milestones", "title")):
        before = {i["id"]: i for i in old.get(section, [])}
        after = {i["id"]: i for i in new.get(section, [])}
        added = [after[i][key] for i in after if i not in before]
        removed = [before[i][key] for i in before if i not in after]
        changed = [after[i][key] for i in after if i in before and after[i] != before[i]]
        if added or removed or changed:
            sections[section] = {"added": added, "removed": removed, "changed": changed}
    return {"first": False, "terms": terms, "sections": sections}


def approved_version(db, project_id):
    """(version_row, decision_row) for the newest version whose current decision is 'approved'."""
    for v in db.execute(
        "SELECT * FROM scope_versions WHERE project_id = ? ORDER BY version_no DESC", (project_id,)
    ).fetchall():
        d = latest_decision(db, "scope_version", v["id"])
        if d is not None and d["decision"] == "approved":
            return v, d
    return None, None


def describe_changes(old, new, labels):
    """'Due date: (none) → 2026-09-09; Title: “A” → “B”' for the fields that differ."""
    def norm(value):
        return None if value == "" else value

    def fmt(value):
        if value is None:
            return "(none)"
        return f"“{value}”" if isinstance(value, str) else f"{value:g}" if isinstance(value, float) else str(value)

    parts = []
    for key, label in labels.items():
        before, after = norm(old.get(key)), norm(new.get(key))
        if before != after:
            parts.append(f"{label}: {fmt(before)} → {fmt(after)}")
    return "; ".join(parts)


def group_change_requests(db, project_id, client_view=False):
    """Change requests split by status, each with its deciding record, plus agreed/pending totals.

    Totals only add up estimates that were actually entered; missing ones are counted, never guessed.
    The client view leaves out drafts and anything withdrawn before it was ever submitted."""
    groups = {status: [] for status in CR_STATUSES}
    where = " AND submitted_at IS NOT NULL" if client_view else ""
    for cr in db.execute(
        f"SELECT * FROM change_requests WHERE project_id = ?{where} ORDER BY created_at DESC, id DESC",
        (project_id,),
    ).fetchall():
        groups[cr["status"]].append({"cr": cr, "decision": latest_decision(db, "change_request", cr["id"])})

    def totals(items):
        hours = [i["cr"]["estimated_hours"] for i in items if i["cr"]["estimated_hours"] is not None]
        costs = [i["cr"]["proposed_cost"] for i in items if i["cr"]["proposed_cost"] is not None]
        return {
            "count": len(items),
            "hours": sum(hours) if hours else None,
            "cost": sum(costs) if costs else None,
            "hours_missing": len(items) - len(hours),
            "cost_missing": len(items) - len(costs),
        }

    return {
        "groups": groups,
        "agreed": totals(groups["approved"] + groups["completed"]),
        "pending": totals(groups["submitted"]),
    }


def scope_baseline(db, project_id):
    """The original agreed scope (first client-approved version) and everything approved since."""
    original = original_dec = None
    for v in db.execute(
        "SELECT * FROM scope_versions WHERE project_id = ? ORDER BY version_no", (project_id,)
    ).fetchall():
        d = db.execute(
            "SELECT * FROM approvals WHERE scope_version_id = ? AND decision = 'approved' ORDER BY id LIMIT 1",
            (v["id"],),
        ).fetchone()
        if d is not None:
            original, original_dec = v, d
            break
    current, current_dec = approved_version(db, project_id)
    changed = original is not None and current is not None and current["id"] != original["id"]
    crs = group_change_requests(db, project_id)
    return {
        "original": original,
        "original_dec": original_dec,
        "current": current,
        "current_dec": current_dec,
        "scope_diff": diff_scopes(snapshot(original), snapshot(current)) if changed else None,
        "approved_changes": crs["groups"]["approved"] + crs["groups"]["completed"],
        "agreed": crs["agreed"],
    }


def same_person(typed, expected):
    """Typed-name confirmation: case and spacing are ignored, everything else must match."""
    norm = lambda v: " ".join((v or "").split()).casefold()  # noqa: E731
    return bool(norm(typed)) and norm(typed) == norm(expected)


def latest_decision(db, subject_type, subject_id):
    column = "scope_version_id" if subject_type == "scope_version" else "change_request_id"
    return db.execute(
        f"SELECT * FROM approvals WHERE subject_type = ? AND {column} = ? ORDER BY id DESC LIMIT 1",
        (subject_type, subject_id),
    ).fetchone()


def record_decision(db, *, project_id, subject_type, subject_id, decision, approver_name,
                    approver_email, method, evidence_note=None, comment=None,
                    ip_address=None, user_agent=None, share_link_id=None):
    if method == "recorded_by_freelancer" and not (evidence_note or "").strip():
        raise ValidationError(["Describe the evidence for a decision recorded on the client's behalf."])
    if not (approver_name or "").strip():
        raise ValidationError(["The name of the person who made the decision is required."])
    if subject_type == "scope_version":
        previous = latest_decision(db, "scope_version", subject_id)
        if previous is not None and previous["decision"] == decision:
            raise ValidationError([
                f"This version is already marked “{DECISION_LABELS[decision]}” "
                f"(by {previous['approver_name']}, {previous['created_at']}). No duplicate record was created."
            ])
    db.execute(
        "INSERT INTO approvals (account_id, project_id, subject_type, scope_version_id, change_request_id, decision,"
        " approver_name, approver_email, method, evidence_note, comment, ip_address, user_agent,"
        " share_link_id, created_at) VALUES ((SELECT account_id FROM projects WHERE id = ?), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            project_id,
            project_id,
            subject_type,
            subject_id if subject_type == "scope_version" else None,
            subject_id if subject_type == "change_request" else None,
            decision,
            approver_name.strip(),
            approver_email,
            method,
            evidence_note,
            comment,
            ip_address,
            (user_agent or "")[:300] or None,
            share_link_id,
            now(),
        ),
    )


# ---------------------------------------------------------------- change requests


def change_cr_status(db, cr, new_status, actor):
    if new_status not in CR_TRANSITIONS[cr["status"]]:
        raise ValidationError([f"A {cr['status']} change request cannot become {new_status}."])
    ts = now()
    cur = db.execute(
        "UPDATE change_requests SET status = ?, updated_at = ?, completed_at = ?, submitted_at = ?"
        " WHERE id = ? AND status = ?",
        (new_status, ts, ts if new_status == "completed" else cr["completed_at"],
         ts if new_status == "submitted" else cr["submitted_at"], cr["id"], cr["status"]),
    )
    if cur.rowcount != 1:
        raise ValidationError(["This change request was already updated. Reload the page and try again."])
    log_event(
        db, cr["project_id"], "change_request_status",
        f"Change request #{cr['id']} “{cr['title']}”: {cr['status']} → {new_status}.", actor,
    )


# ---------------------------------------------------------------- revisions


def revision_usage(db, project, scope=None):
    """{deliverable_id: {'used': n, 'allowance': n, 'over': bool}} for all deliverables.

    With a published snapshot as ``scope``, allowances come from that version rather than the draft."""
    usage = {}
    published = {d["id"]: d for d in scope["deliverables"]} if scope else {}
    default = scope["terms"]["revision_allowance"] if scope else project["revision_allowance"]
    rows = db.execute(
        "SELECT d.id, d.revision_allowance, COUNT(r.id) AS used FROM deliverables d"
        " LEFT JOIN revision_requests r ON r.deliverable_id = d.id"
        " WHERE d.project_id = ? GROUP BY d.id",
        (project["id"],),
    )
    for r in rows:
        own = published[r["id"]]["revision_allowance"] if r["id"] in published else r["revision_allowance"]
        allowance = own if own is not None else default
        usage[r["id"]] = {"used": r["used"], "allowance": allowance, "over": r["used"] > allowance}
    return usage


# ---------------------------------------------------------------- share links


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_share_link(db, project_id, label, days, actor, recipient, recipient_email=None, can_decide=True):
    token = secrets.token_urlsafe(32)  # 256 bits of randomness
    expires = (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat()
    db.execute(
        "INSERT INTO share_links (account_id, project_id, token_hash, label, recipient_name, recipient_email,"
        " can_decide, created_at, expires_at) VALUES ((SELECT account_id FROM projects WHERE id = ?), ?, ?, ?, ?, ?, ?, ?, ?)",
        (project_id, project_id, hash_token(token), label or None, recipient, recipient_email, 1 if can_decide else 0,
         now(), expires),
    )
    log_event(db, project_id, "link_created",
              f"Created {'approval' if can_decide else 'view-only'} review link for {recipient}"
              f"{' (' + label + ')' if label else ''}, expires in {days} days.", actor)
    return token


def resolve_token(db, token):
    """Return the active share link row for a token, or None."""
    if not token or len(token) > 100:
        return None
    link = db.execute("SELECT * FROM share_links WHERE token_hash = ?", (hash_token(token),)).fetchone()
    if link is None or link["revoked_at"] is not None or link["expires_at"] <= now():
        return None
    return link


# ---------------------------------------------------------------- message templates


def render_text(text, context):
    """Substitute {placeholder} names only; unknown names are left as-is (no attribute access)."""
    return PLACEHOLDER_RE.sub(lambda m: str(context.get(m.group(1), m.group(0))), text)


def money(value, currency):
    return "no cost proposed" if value is None else f"{currency} {value:,.2f}"


PLACEHOLDERS = (
    "client_name", "project_name", "freelancer_name", "scope_version", "review_link",
    "revision_allowance", "deadline", "change_title", "change_reason", "change_hours", "change_cost",
)


def message_context(db, project, freelancer_name, change_request=None, review_link=None):
    latest = latest_version(db, project["id"])
    ctx = {
        "client_name": project["client_name"],
        "project_name": project["name"],
        "freelancer_name": freelancer_name,
        "scope_version": latest["version_no"] if latest else "(not yet published)",
        "review_link": review_link or "[create a client review link and paste it here]",
        "revision_allowance": project["revision_allowance"],
        "deadline": project["deadline"] or "(no deadline set)",
    }
    if change_request is not None:
        ctx.update(
            change_title=change_request["title"],
            change_reason=change_request["reason"] or "(not recorded)",
            change_hours=("not estimated" if change_request["estimated_hours"] is None
                          else f"{change_request['estimated_hours']:g}"),
            change_cost=money(change_request["proposed_cost"], project["currency"]),
        )
    return ctx
