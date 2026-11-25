"""Home page: the public landing page for visitors, the dashboard for a signed-in account."""
from flask import Blueprint, current_app, g, render_template

bp = Blueprint("public", __name__)


@bp.route("/")
def index():
    if g.account is not None:
        # Signed in: "/" is the dashboard (billing.check_access already sent ended accounts to Subscribe).
        from .views import dashboard

        return dashboard()
    c = current_app.config
    return render_template("landing.html", price=c["PRICE_TEXT"], trial_days=c["TRIAL_DAYS"],
                           billing=c["BILLING_ENABLED"])
