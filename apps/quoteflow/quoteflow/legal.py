"""Terms of Service, Privacy Policy and Refund Policy pages (required by payment providers).

Registered with plain endpoint names (terms, privacy, refunds) so existing url_for() calls keep working.
"""
from flask import current_app, render_template

UPDATED = "November 24, 2026"
DEFAULT_SUPPORT_EMAIL = "sellerteen096@gmail.com"   # used when SUPPORT_EMAIL isn't set


def _ctx():
    c = current_app.config
    return {"operator": c["LEGAL_NAME"], "price": c["PRICE_TEXT"], "trial_days": c["TRIAL_DAYS"],
            "billing_enabled": c["BILLING_ENABLED"], "updated": UPDATED}


def terms():
    return render_template("terms.html", **_ctx())


def privacy():
    return render_template("privacy.html", **_ctx())


def refunds():
    return render_template("refunds.html", **_ctx())


def register(app):
    app.add_url_rule("/terms", "terms", terms, methods=["GET"])
    app.add_url_rule("/privacy", "privacy", privacy, methods=["GET"])
    app.add_url_rule("/refunds", "refunds", refunds, methods=["GET"])
