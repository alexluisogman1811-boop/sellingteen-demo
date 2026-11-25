"""In-app reminders for documents approaching expiration.

A reminder is created once per document per threshold (30, 14, 7 days, and on
expiry). Generation is idempotent, runs whenever a user loads the dashboard, and
can also be run from cron:  python -m contractordocs.reminders
No email is sent in this MVP.
"""
import sys
from datetime import date, datetime

from .status import parse_iso

THRESHOLDS = [30, 14, 7, 0]

# A document is superseded when a newer reviewed document of the same type for the
# same subcontractor covers a later date (or never expires).
NOT_SUPERSEDED = """NOT EXISTS (SELECT 1 FROM documents d2
    WHERE d2.subcontractor_id = d.subcontractor_id AND d2.doc_type IS d.doc_type
      AND d2.id <> d.id AND d2.needs_review = 0
      AND (d2.no_expiration = 1 OR d2.expiration_date > d.expiration_date))"""


def threshold_for(days):
    """Most urgent threshold crossed, or None if not yet within 30 days."""
    crossed = [t for t in THRESHOLDS if days <= t]
    return min(crossed) if crossed else None


def generate(conn, user_id=None, today=None):
    today = today or date.today()
    sql = ("SELECT d.id, d.user_id, d.expiration_date FROM documents d "
           "WHERE d.needs_review = 0 AND d.no_expiration = 0 AND d.expiration_date IS NOT NULL AND "
           + NOT_SUPERSEDED)
    params = []
    if user_id is not None:
        sql += " AND d.user_id = ?"
        params.append(user_id)
    created = 0
    for doc in conn.execute(sql, params).fetchall():
        exp = parse_iso(doc["expiration_date"])
        if exp is None:
            continue
        t = threshold_for((exp - today).days)
        if t is None:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO reminders (user_id, document_id, threshold_days, expiration_date, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (doc["user_id"], doc["id"], t, doc["expiration_date"], datetime.now().isoformat(timespec="seconds")),
        )
        created += cur.rowcount
    conn.commit()
    return created


def active(conn, user_id):
    """Undismissed reminders, newest threshold per document only."""
    return conn.execute(
        """SELECT r.id, r.threshold_days, r.created_at, d.id AS document_id, d.name AS document_name,
                  d.doc_type, d.expiration_date, s.name AS subcontractor_name, s.id AS subcontractor_id
           FROM reminders r
           JOIN documents d ON d.id = r.document_id AND d.expiration_date = r.expiration_date
           JOIN subcontractors s ON s.id = d.subcontractor_id
           WHERE r.user_id = ? AND r.dismissed = 0 AND d.needs_review = 0 AND d.no_expiration = 0
             AND """ + NOT_SUPERSEDED + """
             AND r.threshold_days = (SELECT MIN(r2.threshold_days) FROM reminders r2
                                     WHERE r2.document_id = r.document_id
                                       AND r2.expiration_date = r.expiration_date)
           ORDER BY d.expiration_date""",
        (user_id,),
    ).fetchall()


def main(argv=None):
    import os
    from .db import connect, init_db
    path = os.environ.get("DATABASE", os.path.join("instance", "contractordocs.sqlite3"))
    init_db(path)
    conn = connect(path)
    n = generate(conn)
    print(f"Created {n} new reminder(s).")
    for u in conn.execute("SELECT id, email FROM users").fetchall():
        for r in active(conn, u["id"]):
            print(f"  {u['email']}: {r['subcontractor_name']} - {r['document_name']} expires {r['expiration_date']}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
