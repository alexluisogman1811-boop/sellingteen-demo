"""Post filtering shared by the list, calendar and CSV export."""
from .constants import ACTIVE_STATUSES, PLATFORMS, STATUSES
from .validation import parse_date, today_and_now

POST_SELECT = """
SELECT p.*, c.name AS campaign_name, c.color AS campaign_color, k.name AS category_name
FROM posts p
LEFT JOIN campaigns c ON c.id = p.campaign_id AND c.account_id = p.account_id
LEFT JOIN categories k ON k.id = p.category_id AND k.account_id = p.account_id
"""

DEFAULT_ORDER = (
    "p.planned_date IS NULL, p.planned_date, COALESCE(p.planned_time, ''), p.id"
)

SPECIAL_STATUS_FILTERS = ("all", "active", "overdue")


def empty_filters():
    return {
        "platform": "",
        "campaign_id": None,
        "category_id": None,
        "status": "",
        "date_from": None,
        "date_to": None,
        "q": "",
        "undated": False,
    }


def parse_filters(args):
    """Read filters from a query string. Invalid values are dropped with a message."""
    f = empty_filters()
    errors = []

    platform = (args.get("platform") or "").strip()
    if platform:
        if platform in PLATFORMS:
            f["platform"] = platform
        else:
            errors.append("Unknown platform filter was ignored.")

    for key in ("campaign_id", "category_id"):
        raw = (args.get(key) or "").strip()
        if raw:
            if raw.isdigit() and len(raw) <= 18:
                f[key] = int(raw)
            else:
                errors.append("Invalid campaign/category filter was ignored.")

    status = (args.get("status") or "").strip()
    if status:
        if status in STATUSES or status in SPECIAL_STATUS_FILTERS:
            f["status"] = status
        else:
            errors.append("Unknown status filter was ignored.")

    for key in ("date_from", "date_to"):
        try:
            f[key] = parse_date(args.get(key))
        except ValueError:
            errors.append("Invalid date filter was ignored.")
    if f["date_from"] and f["date_to"] and f["date_from"] > f["date_to"]:
        errors.append("“From” date was after “to” date, so the dates were swapped.")
        f["date_from"], f["date_to"] = f["date_to"], f["date_from"]

    f["q"] = " ".join((args.get("q") or "").split())[:100]
    f["undated"] = args.get("undated") == "1"
    return f, errors


def filter_query_args(f):
    """Filters as string query args (for links and the export URL)."""
    out = {}
    for key, value in f.items():
        if value in (None, "", False):
            continue
        if key == "undated":
            out[key] = "1"
        elif hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        else:
            out[key] = str(value)
    return out


def overdue_clause():
    today, hm = today_and_now()
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    sql = (
        f"(p.status IN ({placeholders}) AND p.planned_date IS NOT NULL AND "
        "(p.planned_date < ? OR (p.planned_date = ? AND p.planned_time IS NOT NULL AND p.planned_time < ?)))"
    )
    return sql, [*ACTIVE_STATUSES, today, today, hm]


def _escape_like(value):
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def build_where(account_id, f):
    """WHERE clause for the post filters. The account filter is always first and never optional."""
    if account_id is None:
        raise ValueError("account_id is required")
    clauses, params = ["p.account_id = ?"], [account_id]
    if f["platform"]:
        clauses.append("p.platform = ?")
        params.append(f["platform"])
    if f["campaign_id"] is not None:
        clauses.append("p.campaign_id = ?")
        params.append(f["campaign_id"])
    if f["category_id"] is not None:
        clauses.append("p.category_id = ?")
        params.append(f["category_id"])

    status = f["status"]
    if status == "":
        clauses.append("p.status != 'archived'")
    elif status == "active":
        clauses.append(f"p.status IN ({','.join('?' for _ in ACTIVE_STATUSES)})")
        params.extend(ACTIVE_STATUSES)
    elif status == "overdue":
        sql, extra = overdue_clause()
        clauses.append(sql)
        params.extend(extra)
    elif status != "all":
        clauses.append("p.status = ?")
        params.append(status)

    if f["date_from"]:
        clauses.append("p.planned_date >= ?")
        params.append(f["date_from"].isoformat())
    if f["date_to"]:
        clauses.append("p.planned_date <= ?")
        params.append(f["date_to"].isoformat())
    if f["undated"]:
        clauses.append("p.planned_date IS NULL")
    if f["q"]:
        like = f"%{_escape_like(f['q'])}%"
        clauses.append(
            "(p.title LIKE ? ESCAPE '\\' OR p.caption LIKE ? ESCAPE '\\' "
            "OR p.hashtags LIKE ? ESCAPE '\\' OR p.notes LIKE ? ESCAPE '\\')"
        )
        params.extend([like] * 4)

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def fetch_posts(conn, account_id, f, order=DEFAULT_ORDER, limit=None):
    where, params = build_where(account_id, f)
    sql = POST_SELECT + where + " ORDER BY " + order  # order is always a code constant
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return conn.execute(sql, params).fetchall()


def is_overdue(post):
    """Python mirror of overdue_clause() for display."""
    if post["status"] not in ACTIVE_STATUSES or not post["planned_date"]:
        return False
    today, hm = today_and_now()
    if post["planned_date"] < today:
        return True
    return post["planned_date"] == today and bool(post["planned_time"]) and post["planned_time"] < hm
