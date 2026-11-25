"""Product pages: landing page, health check, short sign-up/login addresses."""
from flask import Blueprint, current_app, g, redirect, render_template, url_for

from .db import get_db

bp = Blueprint("site", __name__)


@bp.route("/")
def landing():
    return render_template("landing.html", price=current_app.config["PRICE_TEXT"],
                           trial_days=current_app.config["TRIAL_DAYS"],
                           billing=current_app.config["BILLING_ENABLED"])


@bp.route("/healthz")
def healthz():
    # For the hosting platform's health check: confirms the app and database respond.
    get_db().execute("SELECT 1").fetchone()
    return {"status": "ok"}


@bp.route("/signup")
def signup():
    return redirect(url_for("admin.signup"))


@bp.route("/login")
def login():
    return redirect(url_for("admin.dashboard" if g.get("account") else "admin.login"))
