"""Terms of Service, Privacy Policy and Refund Policy pages (required by payment providers)."""
from flask import Blueprint, current_app, render_template

bp = Blueprint("legal", __name__)

UPDATED = "November 23, 2026"


def _ctx():
    c = current_app.config
    return {"operator": c["LEGAL_NAME"], "support_email": c["SUPPORT_EMAIL"], "price": c["PRICE_TEXT"],
            "trial_days": c["TRIAL_DAYS"], "ai_mock": c["AI_MOCK"], "updated": UPDATED}


@bp.route("/terms")
def terms():
    return render_template("legal/terms.html", **_ctx())


@bp.route("/privacy")
def privacy():
    return render_template("legal/privacy.html", **_ctx())


@bp.route("/refunds")
def refunds():
    return render_template("legal/refunds.html", **_ctx())
