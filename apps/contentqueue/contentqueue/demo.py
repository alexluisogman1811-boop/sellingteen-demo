"""Demo data plus safe workspace reset (always backs up first). Everything is per account."""
import json
import os
import shutil
import sqlite3
from datetime import date, datetime, timedelta

from . import timeutil
from .db import now_iso
from .uploads import delete_upload, is_stored_name

DEMO_CAMPAIGNS = [
    ("Autumn Launch", "New seasonal menu and product line.", "amber", -14, 21),
    ("Customer Stories", "Real reviews and photos from regulars.", "teal", None, None),
    ("Holiday Promo", "Gift cards and holiday bundles.", "rose", 20, 60),
]

DEMO_CATEGORIES = [
    ("Product", "Products, menu items and launches."),
    ("Behind the scenes", "Team, process and workspace."),
    ("Education", "Tips, how-tos and FAQs."),
    ("Promotion", "Offers, discounts and events."),
    ("Community", "Customers, partners and local news."),
]

# (title, platform, status, campaign, category, day_offset, time, caption, hashtags, notes)
DEMO_POSTS = [
    ("Pumpkin spice latte is back", "instagram", "published", "Autumn Launch", "Product", -9, "08:30",
     "It's officially autumn ☕🍂\nOur pumpkin spice latte is back from today — made with real pumpkin and house-made spice syrup.",
     "#pumpkinspice #autumn #coffeetime", "Photo: latte art on wooden table."),
    ("Meet our head baker", "facebook", "published", "Customer Stories", "Behind the scenes", -6, "12:00",
     "Say hi to Maya! She's been baking our sourdough since 5am every day for three years. Drop her a 👋 in the comments.",
     "#meettheteam #smallbusiness", ""),
    ("How we roast our beans", "tiktok", "published", None, "Education", -4, "17:00",
     "From green bean to your cup in 60 seconds. Watch the roast!",
     "#coffeeroasting #behindthescenes", "15-45s vertical clip."),
    ("Weekend brunch reminder", "instagram", "scheduled", "Autumn Launch", "Promotion", -2, "09:00",
     "Brunch is served all weekend long 🥞 Tables fill up fast — book via the link in bio.",
     "#brunch #weekendvibes", "Was planned for last weekend — confirm whether it went out."),
    ("Hiring: weekend barista", "linkedin", "draft", None, "Community", -1, None,
     "We're hiring! Looking for a friendly weekend barista to join our small team. Experience welcome but not required.",
     "#hiring #barista #localjobs", ""),
    ("Autumn menu carousel", "instagram", "scheduled", "Autumn Launch", "Product", 1, "11:00",
     "Swipe to see everything new on our autumn menu ➡️\nWhich one are you trying first?",
     "#autumnmenu #newmenu #foodie", "5 slides; export from Canva."),
    ("Customer review spotlight", "facebook", "review", "Customer Stories", "Community", 2, "13:00",
     "\"Best flat white in town and the friendliest staff.\" — Sam R.\nThank you for the kind words, Sam!",
     "#customerlove #reviews", "Ask Sam for permission before posting."),
    ("Latte art tutorial", "tiktok", "draft", None, "Education", 3, "18:30",
     "Can you pour a heart? Here's the 3-step trick our baristas use.",
     "#latteart #coffeetok #tutorial", ""),
    ("Thank you, 1,000 followers", "instagram", "review", None, "Community", 5, "10:00",
     "We just hit 1,000 followers! Thank you for supporting a small local café. Free cookie with every drink this Friday 🍪",
     "#thankyou #milestone #smallbusiness", ""),
    ("Bean subscription launch", "linkedin", "scheduled", "Autumn Launch", "Product", 7, "08:00",
     "Our beans, delivered monthly. Launching a local coffee subscription for offices and homes.",
     "#coffee #subscription #localbusiness", ""),
    ("Gift card guide", "facebook", "draft", "Holiday Promo", "Promotion", 22, None,
     "Stuck for gift ideas? Our gift cards never go out of style. Available in store and online.",
     "#giftideas #holidays #shoplocal", ""),
    ("Holiday opening hours", "instagram", "draft", "Holiday Promo", "Promotion", 28, "09:00",
     "Our holiday opening hours are here! Save this post so you don't miss out.",
     "#holidayhours #shoplocal", "Confirm hours with the team first."),
    ("Old summer menu post", "instagram", "archived", None, "Product", -40, "10:00",
     "Iced drinks are here!", "#summer", "Archived — season over."),
    ("Ideas for winter blend name", "other", "draft", None, None, None, None,
     "Poll idea: let followers name our winter blend.", "", "No date yet."),
]

DEMO_TEMPLATES = [
    ("New product announcement", "instagram",
     "✨ New in: {product}! ✨\n{benefit}\n\nAvailable {when} — {cta}",
     "#new #shoplocal"),
    ("Limited-time offer", "facebook",
     "This week only: {offer} on {product}. Ends {end_date}.\n{cta}",
     "#deal #limitedtime"),
    ("Customer testimonial", None,
     "\"{quote}\" — {customer}\n\nThank you for choosing us! 💛",
     "#customerlove #reviews"),
    ("Event invite", "linkedin",
     "Join us for {event} on {date} at {location}. {details}\nRSVP: {cta}",
     "#event #community"),
]

DEMO_IDEAS = [
    ("Day-in-the-life reel of the opening shift", "Film from 5am bake to first customer.", "Behind the scenes"),
    ("FAQ: are your pastries vegan?", "Turn the most common question into a carousel.", "Education"),
    ("Local artist feature", "Showcase the artist whose work hangs on our walls.", "Community"),
    ("Loyalty card reminder", "Short story post reminding about stamp cards.", "Promotion"),
    ("Coffee origin map", "Infographic of where each bean comes from.", "Education"),
]


def demo_present(conn, account_id):
    for table in ("posts", "campaigns", "categories", "templates", "ideas"):
        if conn.execute(f"SELECT 1 FROM {table} WHERE is_demo = 1 AND account_id = ? LIMIT 1",
                        (account_id,)).fetchone():
            return True
    return False


def _insert_or_get(conn, account_id, table, name, columns, values):
    """Insert a named demo row; if this account already has one with that name, reuse it."""
    try:
        cur = conn.execute(
            f"INSERT INTO {table} (account_id, {', '.join(columns)}) "
            f"VALUES (?, {', '.join('?' for _ in columns)})",
            (account_id, *values),
        )
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return conn.execute(f"SELECT id FROM {table} WHERE name = ? AND account_id = ?",
                            (name, account_id)).fetchone()["id"]


def load_demo(conn, account_id, today=None):
    """Insert demo content into one account, dated around `today`. Returns False if already loaded."""
    if demo_present(conn, account_id):
        return False
    today = today or timeutil.today()
    ts = now_iso()

    def day(offset):
        return (today + timedelta(days=offset)).isoformat() if offset is not None else None

    campaigns = {}
    for name, desc, color, start, end in DEMO_CAMPAIGNS:
        campaigns[name] = _insert_or_get(
            conn, account_id, "campaigns", name,
            ("name", "description", "color", "start_date", "end_date", "is_demo", "created_at"),
            (name, desc, color, day(start), day(end), 1, ts),
        )
    categories = {}
    for name, desc in DEMO_CATEGORIES:
        categories[name] = _insert_or_get(
            conn, account_id, "categories", name,
            ("name", "description", "is_demo", "created_at"),
            (name, desc, 1, ts),
        )
    for (title, platform, status, campaign, category, offset, time_, caption, tags, notes) in DEMO_POSTS:
        conn.execute(
            """INSERT INTO posts (account_id, title, caption, hashtags, platform, status, campaign_id, category_id,
                                  planned_date, planned_time, notes, published_at, is_demo, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (account_id, title, caption, tags, platform, status, campaigns.get(campaign), categories.get(category),
             day(offset), time_, notes, ts if status == "published" else None, ts, ts),
        )
    for name, platform, body, tags in DEMO_TEMPLATES:
        conn.execute(
            "INSERT INTO templates (account_id, name, body, hashtags, platform, is_demo, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
            (account_id, name, body, tags, platform, ts, ts),
        )
    for title, notes, category in DEMO_IDEAS:
        conn.execute(
            "INSERT INTO ideas (account_id, title, notes, category_id, is_demo, created_at) VALUES (?, ?, ?, ?, 1, ?)",
            (account_id, title, notes, categories.get(category), ts),
        )
    conn.commit()
    return True


def remove_demo(conn, account_id, upload_folder):
    """Delete only this account's rows flagged as demo data. User-created content is untouched."""
    for row in conn.execute("SELECT image_filename FROM posts WHERE is_demo = 1 AND account_id = ? "
                            "AND image_filename IS NOT NULL", (account_id,)):
        delete_upload(upload_folder, row["image_filename"])
    counts = {}
    for table in ("posts", "ideas", "templates", "campaigns", "categories"):
        counts[table] = conn.execute(f"DELETE FROM {table} WHERE is_demo = 1 AND account_id = ?",
                                     (account_id,)).rowcount
    conn.commit()
    return counts


def account_backup_folder(backup_folder, account_id):
    return os.path.join(backup_folder, f"account-{int(account_id)}")


def reset_workspace(conn, account_id, upload_folder, backup_folder):
    """Back up ONE account's content (JSON + its images), then delete that account's content.

    Other accounts are never touched, and the backup contains only this account's rows.
    Returns the backup directory path."""
    stamp = timeutil.now().strftime("%Y%m%d-%H%M%S")
    root = account_backup_folder(backup_folder, account_id)
    backup_dir = os.path.join(root, f"reset-{stamp}")
    suffix = 1
    while os.path.exists(backup_dir):
        suffix += 1
        backup_dir = os.path.join(root, f"reset-{stamp}-{suffix}")
    os.makedirs(backup_dir)

    tables = ("posts", "ideas", "templates", "campaigns", "categories")
    dump = {t: [dict(r) for r in conn.execute(f"SELECT * FROM {t} WHERE account_id = ? ORDER BY id",
                                              (account_id,))] for t in tables}
    with open(os.path.join(backup_dir, "content.json"), "w", encoding="utf-8") as fh:
        json.dump(dump, fh, ensure_ascii=False, indent=1)

    images = [p["image_filename"] for p in dump["posts"] if p.get("image_filename")]
    if images and os.path.isdir(upload_folder):
        moved_dir = os.path.join(backup_dir, "uploads")
        for name in images:
            src = os.path.join(upload_folder, name)
            if is_stored_name(name) and os.path.isfile(src):
                os.makedirs(moved_dir, exist_ok=True)
                shutil.move(src, os.path.join(moved_dir, name))

    for table in tables:
        conn.execute(f"DELETE FROM {table} WHERE account_id = ?", (account_id,))
    conn.commit()
    return backup_dir
