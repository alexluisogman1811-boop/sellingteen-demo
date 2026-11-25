"""Password changes and one-time reset links. Nothing is emailed: links are shown once to be copied."""
from datetime import timedelta

from flask import current_app
from werkzeug.security import generate_password_hash

from .db import get_db
from .links import shareable_url
from .security import audit, new_token
from .util import now_iso, utcnow


def set_password(user, new_password):
    """Store a new password hash and end every existing session of this user."""
    get_db().execute("UPDATE users SET password_hash = ?, session_epoch = session_epoch + 1 WHERE id = ?",
                     (generate_password_hash(new_password), user["id"]))


def create_reset_link(user, created_by):
    """Create a one-time reset link valid RESET_LINK_HOURS. Earlier unused links stop working.
    Caller commits. Returns the full link (shown once; only its hash is stored)."""
    db = get_db()
    now = now_iso()
    db.execute("UPDATE password_resets SET used_at = ? WHERE user_id = ? AND used_at IS NULL", (now, user["id"]))
    raw, token_hash = new_token()
    expires = (utcnow() + timedelta(hours=current_app.config["RESET_LINK_HOURS"])).isoformat()
    db.execute("INSERT INTO password_resets (company_id, user_id, token_hash, created_by, created_at, expires_at)"
               " VALUES (?, ?, ?, ?, ?, ?)", (user["company_id"], user["id"], token_hash, created_by, now, expires))
    audit(user["company_id"], "user.reset_link_created", details={"user_id": user["id"]},
          actor=created_by, user_id=None)
    return shareable_url("auth.reset_password", token=raw)
