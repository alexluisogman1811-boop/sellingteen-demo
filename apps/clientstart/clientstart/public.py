"""Public pages: the landing page and the hosting health check."""
from flask import Blueprint, redirect, render_template, url_for

from .auth import is_owner
from .db import get_db

bp = Blueprint("public", __name__)


@bp.get("/")
def index():
    if is_owner():
        return redirect(url_for("owner.dashboard"))
    return render_template("landing.html")


@bp.get("/healthz")
def healthz():
    # For the hosting platform's health check: confirms the app and database respond.
    get_db().execute("SELECT 1").fetchone()
    return {"status": "ok"}
