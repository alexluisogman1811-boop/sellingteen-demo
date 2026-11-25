"""Shared constants: platforms, statuses and the allowed status workflow."""

PLATFORMS = {
    "instagram": {"label": "Instagram", "limit": 2200},
    "tiktok": {"label": "TikTok", "limit": 2200},
    "facebook": {"label": "Facebook", "limit": 63206},
    "linkedin": {"label": "LinkedIn", "limit": 3000},
    "x": {"label": "X (Twitter)", "limit": 280},
    "youtube": {"label": "YouTube", "limit": 5000},
    "other": {"label": "Other", "limit": None},
}

STATUSES = ["draft", "review", "scheduled", "published", "archived"]

STATUS_LABELS = {
    "draft": "Draft",
    "review": "In review",
    "scheduled": "Scheduled",
    "published": "Published",
    "archived": "Archived",
}

# Statuses that still need work / publishing. Posts in these statuses with a
# planned date in the past are considered overdue.
ACTIVE_STATUSES = ("draft", "review", "scheduled")

# Allowed workflow moves. "published" is only reachable from "scheduled" and
# only when the user explicitly confirms they published the post themselves.
TRANSITIONS = {
    "draft": ("review", "scheduled", "archived"),
    "review": ("draft", "scheduled", "archived"),
    "scheduled": ("draft", "review", "published", "archived"),
    "published": ("scheduled", "archived"),
    "archived": ("draft",),
}

# Statuses a brand-new post may start in.
CREATE_STATUSES = ("draft", "review", "scheduled")

# Campaign colour tags map to CSS classes (no inline styles, so CSP stays strict).
COLORS = ["blue", "green", "amber", "rose", "violet", "teal", "slate"]

IDEA_STATUSES = ("new", "used")

MAX_TITLE = 120
MAX_CAPTION = 10000
MAX_NOTES = 2000
MAX_HASHTAGS = 30
MAX_NAME = 80
MAX_DESCRIPTION = 500
MAX_TEMPLATE = 5000
MAX_URL = 500
