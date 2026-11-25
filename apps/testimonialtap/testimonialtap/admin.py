"""Account routes: sign-up, login, landing page, dashboard, projects, testimonials, export, demo data.

Every query here is filtered by the logged-in account (``current_account_id()``), so one
freelancer or agency can never see or change another account's projects or testimonials,
even by editing ids in the URL: other accounts' ids simply return 404.
"""
import csv
import hashlib
import io
import json
import re
import zipfile
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from flask import (Blueprint, Response, abort, current_app, flash, redirect, render_template,
                   request, session, url_for)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

from . import demo, mailer
from .db import (PUBLISHABLE_SQL, current_account_id, get_account_by_email, get_db, get_profile,
                 log_activity, project_or_404, publishable_for_project, testimonial_or_404, utcnow)
from .security import client_ip, csrf_token, is_owner, login_required, new_public_token, safe_next
from .validation import (LIMITS, clean_text, parse_date, validate_permission_note, validate_profile,
                         validate_project, validate_testimonial)

bp = Blueprint("admin", __name__)

STATUSES = ("pending", "approved", "rejected", "archived")
EDITABLE_FIELDS = ("customer_name", "business", "feedback", "website")
CSV_COLUMNS = (
    "id", "project", "customer_name", "business", "feedback", "rating", "website", "status",
    "permission_granted", "permission_source", "permission_note", "permission_at",
    "submitted_at", "status_changed_at", "edited", "is_demo",
)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PASSWORD_MIN = 10


def public_url(endpoint, **values):
    """Absolute link for sharing. BASE_URL is the app's public address, including any prefix."""
    path = url_for(endpoint, **values)
    base = current_app.config.get("BASE_URL")
    if base:
        return base + path[len(request.script_root):]
    return url_for(endpoint, _external=True, **values)


# --------------------------------------------------------------------------- auth

def _start_session(account_id):
    session.clear()  # prevent session fixation; also rotates the CSRF token
    session["account_id"] = account_id
    session.permanent = True
    csrf_token()


def _password_error(password, confirm=None):
    if len(password) < PASSWORD_MIN:
        return f"Use at least {PASSWORD_MIN} characters."
    if len(password) > 200:
        return "Password is too long."
    if confirm is not None and password != confirm:
        return "Passwords do not match."
    return None


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if is_owner():
        return redirect(url_for("admin.dashboard"))
    values, errors = {}, {}
    if request.method == "POST":
        limit, window = current_app.config["SIGNUP_RATE_LIMIT"]
        if not current_app.limiter.hit(f"signup:{client_ip()}", limit, window):
            abort(429, description="Too many sign-ups from your network. Please try again later.")
        values = {"business_name": clean_text(request.form.get("business_name")),
                  "email": clean_text(request.form.get("email")).lower()}
        password = request.form.get("password", "")
        if not values["business_name"]:
            errors["business_name"] = "Business name is required."
        elif len(values["business_name"]) > LIMITS["business_name"]:
            errors["business_name"] = f"Business name must be {LIMITS['business_name']} characters or fewer."
        if len(values["email"]) > 254 or not EMAIL_RE.match(values["email"]):
            errors["email"] = "Enter a valid email address."
        elif get_account_by_email(values["email"]):
            errors["email"] = "An account with this email already exists. Log in instead."
        err = _password_error(password)
        if err:
            errors["password"] = err
        if not errors:
            db = get_db()
            cur = db.execute(
                "INSERT INTO accounts (business_name, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (values["business_name"], values["email"], generate_password_hash(password), utcnow()),
            )
            log_activity("account_created", "Account created", account_id=cur.lastrowid)
            db.commit()
            _start_session(cur.lastrowid)
            flash(f"Welcome! Your {current_app.config['TRIAL_DAYS']}-day free trial has started.", "success")
            return redirect(url_for("admin.dashboard"))
    return render_template("signup.html", values=values, errors=errors, limits=LIMITS), 400 if errors else 200


@bp.route("/setup")
def setup():
    """Old single-owner setup address: accounts are now created on the sign-up page."""
    return redirect(url_for("admin.signup"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    if is_owner():
        return redirect(url_for("admin.dashboard"))
    error, email = None, ""
    if request.method == "POST":
        limit, window = current_app.config["LOGIN_RATE_LIMIT"]
        if not current_app.limiter.hit(f"login:{client_ip()}", limit, window):
            abort(429, description="Too many login attempts. Please wait a few minutes.")
        email = clean_text(request.form.get("email")).lower()[:254]
        account = get_account_by_email(email) if email else None
        if account and check_password_hash(account["password_hash"], request.form.get("password", "")):
            _start_session(account["id"])
            return redirect(safe_next(request.args.get("next"), url_for("admin.dashboard")))
        error = "That email and password don't match."
    return render_template("login.html", error=error, email=email), 401 if error else 200


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("admin.login"))


# --------------------------------------------------------------------------- business profile

def _render_profile(values=None, errors=None, password_errors=None, status=200):
    return render_template("profile.html", values=values or get_profile(), errors=errors or {},
                           password_errors=password_errors or {}, limits=LIMITS), status


@bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "GET":
        return _render_profile()
    values, errors = validate_profile(request.form)
    if errors:
        return _render_profile(values, errors, status=400)
    db = get_db()
    db.execute("UPDATE accounts SET business_name = ?, website = ? WHERE id = ?",
               (values["business_name"], values["website"], current_account_id()))
    log_activity("profile_updated", f"Business profile saved as “{values['business_name']}”")
    db.commit()
    flash("Business profile saved. Your public pages and submission forms now show this name.", "success")
    return redirect(url_for("admin.profile"))


@bp.route("/profile/password", methods=["POST"])
@login_required
def change_password():
    errors = {}
    new = request.form.get("new", "")
    row = get_db().execute("SELECT password_hash FROM accounts WHERE id = ?", (current_account_id(),)).fetchone()
    if not check_password_hash(row["password_hash"], request.form.get("current", "")):
        errors["current"] = "Current password is incorrect."
    else:
        err = _password_error(new, request.form.get("confirm", ""))
        if err:
            errors["confirm" if err == "Passwords do not match." else "new"] = err
    if errors:
        return _render_profile(password_errors=errors, status=400)
    db = get_db()
    db.execute("UPDATE accounts SET password_hash = ? WHERE id = ?",
               (generate_password_hash(new), current_account_id()))
    log_activity("password_changed", "Password changed")
    db.commit()
    flash("Password changed.", "success")
    return redirect(url_for("admin.profile"))


# --------------------------------------------------------------------------- forgot password (optional email)

RESET_MAX_AGE = 3600  # reset links work for 1 hour
RESET_SENT_MESSAGE = "If an account exists for that email, we sent a link to reset your password."


def _mail_on():
    return mailer.mail_enabled(current_app.config.get("MAIL"))


def _reset_serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="pw-reset")


def _password_fingerprint(password_hash):
    # Changes whenever the password changes, so a used link stops working.
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()[:16]


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if not _mail_on():
        abort(404)
    sent = False
    if request.method == "POST":
        email = clean_text(request.form.get("email")).lower()[:254]
        limit, window = current_app.config["RESET_RATE_LIMIT"]
        ip_ok = current_app.limiter.hit(f"reset-ip:{client_ip()}", limit * 2, window)
        email_ok = current_app.limiter.hit(f"reset-email:{email}", limit, window) if email else False
        account = get_account_by_email(email) if (email and ip_ok and email_ok) else None
        if account and not account["is_demo"]:
            token = _reset_serializer().dumps({"e": account["email"],
                                               "h": _password_fingerprint(account["password_hash"])})
            link = public_url("admin.reset_password", token=token)
            mailer.send_mail(
                current_app.config["MAIL"], account["email"], "Reset your TestimonialTap password",
                "Someone asked to reset the password for your TestimonialTap account.\n\n"
                f"To choose a new password, open this link (it works for 1 hour):\n{link}\n\n"
                "If you did not ask for this, you can ignore this email. Your password stays the same.\n")
        sent = True  # always the same answer, so nobody can find out which emails have accounts
    return render_template("forgot_password.html", sent=sent, message=RESET_SENT_MESSAGE)


def _account_from_reset_token(token):
    try:
        data = _reset_serializer().loads(token, max_age=RESET_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict):
        return None
    account = get_account_by_email(str(data.get("e", "")))
    if account is None or data.get("h") != _password_fingerprint(account["password_hash"]):
        return None
    return account


@bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    if not _mail_on():
        abort(404)
    account = _account_from_reset_token(token)
    if account is None:
        return render_template("reset_password.html", invalid=True, errors={}), 400
    errors = {}
    if request.method == "POST":
        new = request.form.get("new", "")
        err = _password_error(new, request.form.get("confirm", ""))
        if err:
            errors["confirm" if err == "Passwords do not match." else "new"] = err
        else:
            db = get_db()
            db.execute("UPDATE accounts SET password_hash = ? WHERE id = ?",
                       (generate_password_hash(new), account["id"]))
            log_activity("password_reset", "Password reset by email link", account_id=account["id"])
            db.commit()
            session.clear()
            flash("Your password was changed. You can log in now.", "success")
            return redirect(url_for("admin.login"))
    return render_template("reset_password.html", invalid=False, errors=errors, token=token), \
        400 if errors else 200


# --------------------------------------------------------------------------- delete account & export everything

@bp.route("/account/delete", methods=["GET", "POST"])
@login_required
def delete_account():
    account = get_db().execute("SELECT * FROM accounts WHERE id = ?", (current_account_id(),)).fetchone()
    error = None
    if request.method == "POST":
        typed = (request.form.get("confirm") or "").strip()
        if typed != "DELETE" and typed.lower() != account["email"].lower():
            error = "Type DELETE (or your email address) to confirm. Your account has not been deleted."
        elif not current_app.limiter.hit(f"delete:{client_ip()}", *current_app.config["LOGIN_RATE_LIMIT"]):
            abort(429, description="Too many attempts. Please wait a few minutes.")
        elif not check_password_hash(account["password_hash"], request.form.get("password", "")):
            error = "That password isn't right. Your account has not been deleted."
        else:
            aid = account["id"]
            db = get_db()
            # Explicit deletes as well as ON DELETE CASCADE, in case foreign keys are off on an old database.
            db.execute("DELETE FROM activity WHERE account_id = ?", (aid,))
            db.execute("DELETE FROM testimonials WHERE account_id = ?", (aid,))
            db.execute("DELETE FROM projects WHERE account_id = ?", (aid,))
            db.execute("DELETE FROM accounts WHERE id = ?", (aid,))
            db.commit()
            session.clear()
            flash("Your account and all its data have been deleted.", "info")
            return redirect(url_for("admin.index"))
    return render_template("delete_account.html", error=error), 400 if error else 200


EXPORT_TABLES = (
    ("account.csv", "SELECT id, business_name, email, website, is_demo, paid_until, created_at "
                    "FROM accounts WHERE id = ?"),
    ("projects.csv", "SELECT id, name, client_name, description, intro_message, accepting, is_demo, "
                     "created_at, updated_at FROM projects WHERE account_id = ? ORDER BY id"),
    ("testimonials.csv", "SELECT t.id, p.name AS project, t.customer_name, t.business, t.feedback, t.rating, "
                         "t.website, t.status, t.permission_granted, t.permission_source, t.permission_note, "
                         "t.permission_at, t.edited, t.edited_at, t.original_json, t.is_demo, t.submitted_at, "
                         "t.status_changed_at, t.updated_at FROM testimonials t "
                         "LEFT JOIN projects p ON p.id = t.project_id AND p.account_id = t.account_id "
                         "WHERE t.account_id = ? ORDER BY t.id"),
    ("activity.csv", "SELECT id, kind, message, project_id, testimonial_id, is_demo, created_at "
                     "FROM activity WHERE account_id = ? ORDER BY id"),
)


@bp.route("/account/export.zip")
@login_required
def export_all():
    """All of this account's data as a ZIP of CSV files (works after the trial too)."""
    db = get_db()
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, sql in EXPORT_TABLES:
            cur = db.execute(sql, (current_account_id(),))
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow([d[0] for d in cur.description])
            for row in cur.fetchall():
                writer.writerow(["" if v is None else _csv_safe(v) for v in row])
            zf.writestr(name, "﻿" + buf.getvalue())
    filename = f"testimonialtap-export-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    return Response(mem.getvalue(), mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# --------------------------------------------------------------------------- landing & dashboard

@bp.route("/")
def index():
    """Public landing page; logged-in owners see their dashboard here too."""
    if is_owner():
        return dashboard()
    return render_template("landing.html", price=current_app.config["PRICE_TEXT"],
                           trial_days=current_app.config["TRIAL_DAYS"])


@bp.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    aid = current_account_id()
    stats = db.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(status = 'pending') AS pending, "
        "SUM(status = 'approved') AS approved, "
        "SUM(status = 'approved' AND permission_granted = 1) AS publishable, "
        "SUM(status = 'approved' AND permission_granted = 0) AS needs_permission, "
        "SUM(status = 'rejected') AS rejected, "
        "SUM(status = 'archived') AS archived "
        "FROM testimonials WHERE account_id = ?", (aid,)
    ).fetchone()
    stats = {k: (stats[k] or 0) for k in stats.keys()}
    activity = db.execute(
        "SELECT a.*, p.name AS project_name FROM activity a "
        "LEFT JOIN projects p ON p.id = a.project_id AND p.account_id = a.account_id "
        "WHERE a.account_id = ? ORDER BY a.id DESC LIMIT 15", (aid,)
    ).fetchall()
    pending = db.execute(
        "SELECT t.*, p.name AS project_name FROM testimonials t JOIN projects p ON p.id = t.project_id "
        "WHERE t.account_id = ? AND t.status = 'pending' ORDER BY t.submitted_at DESC LIMIT 5", (aid,)
    ).fetchall()
    return render_template("dashboard.html", stats=stats, activity=activity, pending=pending,
                           projects=_projects_with_counts(), has_demo=demo.has_demo_data(aid),
                           has_profile=bool(get_profile()["business_name"]))


def _projects_with_counts():
    return get_db().execute(
        "SELECT p.*, COUNT(t.id) AS total, "
        "COALESCE(SUM(t.status = 'pending'), 0) AS pending, "
        f"COALESCE(SUM({PUBLISHABLE_SQL}), 0) AS publishable "
        "FROM projects p LEFT JOIN testimonials t ON t.project_id = p.id AND t.account_id = p.account_id "
        "WHERE p.account_id = ? "
        "GROUP BY p.id ORDER BY p.created_at DESC, p.id DESC", (current_account_id(),)
    ).fetchall()


# --------------------------------------------------------------------------- projects

@bp.route("/projects")
@login_required
def projects():
    return render_template("projects.html", projects=_projects_with_counts())


@bp.route("/projects/new", methods=["GET", "POST"])
@login_required
def project_new():
    values, errors = {}, {}
    if request.method == "POST":
        values, errors = validate_project(request.form)
        if not errors:
            db = get_db()
            now = utcnow()
            cur = db.execute(
                "INSERT INTO projects (account_id, name, client_name, description, intro_message, submit_token, "
                "wall_token, accepting, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (current_account_id(), values["project_name"], values["client_name"], values["description"],
                 values["intro_message"], new_public_token(), new_public_token(), now, now),
            )
            log_activity("project_created", f"Project “{values['project_name']}” created",
                         project_id=cur.lastrowid)
            db.commit()
            flash("Project created. Share the submission link with your client.", "success")
            return redirect(url_for("admin.project_detail", project_id=cur.lastrowid))
    return render_template("project_form.html", values=values, errors=errors, project=None,
                           limits=LIMITS), 400 if errors else 200


@bp.route("/projects/<int:project_id>")
@login_required
def project_detail(project_id):
    project = project_or_404(project_id)
    db = get_db()
    counts = db.execute(
        "SELECT status, COUNT(*) AS n FROM testimonials WHERE project_id = ? AND account_id = ? GROUP BY status",
        (project_id, current_account_id()),
    ).fetchall()
    publishable = publishable_for_project(project_id)
    snippet = render_template("snippet.html", project=project, testimonials=publishable)
    links = {
        "submit": public_url("public.submit", token=project["submit_token"]),
        "wall": public_url("public.wall", token=project["wall_token"]),
        "embed": public_url("public.embed", token=project["wall_token"]),
    }
    iframe = (f'<iframe src="{links["embed"]}" title="Testimonials" loading="lazy" '
              'style="width:100%;min-height:420px;border:0;"></iframe>')
    host = (urlsplit(links["submit"]).hostname or "").lower()
    local_links = host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.endswith(".localhost")
    return render_template("project_detail.html", project=project,
                           counts={r["status"]: r["n"] for r in counts},
                           publishable=publishable, snippet=snippet.strip(), iframe=iframe,
                           links=links, local_links=local_links, limits=LIMITS)


@bp.route("/projects/<int:project_id>/edit", methods=["GET", "POST"])
@login_required
def project_edit(project_id):
    project = project_or_404(project_id)
    values = {"project_name": project["name"], "client_name": project["client_name"],
              "description": project["description"], "intro_message": project["intro_message"]}
    errors = {}
    if request.method == "POST":
        values, errors = validate_project(request.form)
        if not errors:
            db = get_db()
            db.execute(
                "UPDATE projects SET name = ?, client_name = ?, description = ?, intro_message = ?, "
                "updated_at = ? WHERE id = ? AND account_id = ?",
                (values["project_name"], values["client_name"], values["description"],
                 values["intro_message"], utcnow(), project_id, current_account_id()),
            )
            log_activity("project_updated", f"Project “{values['project_name']}” updated",
                         project_id=project_id, is_demo=project["is_demo"])
            db.commit()
            flash("Project updated.", "success")
            return redirect(url_for("admin.project_detail", project_id=project_id))
    return render_template("project_form.html", values=values, errors=errors, project=project,
                           limits=LIMITS), 400 if errors else 200


@bp.route("/projects/<int:project_id>/toggle", methods=["POST"])
@login_required
def project_toggle(project_id):
    project = project_or_404(project_id)
    accepting = 0 if project["accepting"] else 1
    db = get_db()
    db.execute("UPDATE projects SET accepting = ?, updated_at = ? WHERE id = ? AND account_id = ?",
               (accepting, utcnow(), project_id, current_account_id()))
    log_activity("project_toggled",
                 f"Submissions {'opened' if accepting else 'closed'} for “{project['name']}”",
                 project_id=project_id, is_demo=project["is_demo"])
    db.commit()
    flash("Submission link is now " + ("open." if accepting else "closed."), "success")
    return redirect(url_for("admin.project_detail", project_id=project_id))


@bp.route("/projects/<int:project_id>/rotate", methods=["POST"])
@login_required
def project_rotate(project_id):
    project = project_or_404(project_id)
    which = request.form.get("which")
    column = {"submit": "submit_token", "wall": "wall_token"}.get(which)
    if column is None:
        abort(400)
    db = get_db()
    db.execute(f"UPDATE projects SET {column} = ?, updated_at = ? WHERE id = ? AND account_id = ?",
               (new_public_token(), utcnow(), project_id, current_account_id()))
    label = "submission link" if which == "submit" else "public page / embed link"
    log_activity("token_rotated", f"New {label} generated for “{project['name']}”",
                 project_id=project_id, is_demo=project["is_demo"])
    db.commit()
    flash(f"A new {label} was generated. The old link no longer works.", "success")
    return redirect(url_for("admin.project_detail", project_id=project_id))


@bp.route("/projects/<int:project_id>/delete", methods=["POST"])
@login_required
def project_delete(project_id):
    project = project_or_404(project_id)
    if request.form.get("confirm_name", "").strip() != project["name"]:
        flash("Type the project name exactly to confirm deletion.", "error")
        return redirect(url_for("admin.project_detail", project_id=project_id))
    db = get_db()
    db.execute("DELETE FROM projects WHERE id = ? AND account_id = ?", (project_id, current_account_id()))
    log_activity("project_deleted", f"Project “{project['name']}” and its testimonials deleted",
                 is_demo=project["is_demo"])
    db.commit()
    flash("Project deleted.", "success")
    return redirect(url_for("admin.projects"))


# --------------------------------------------------------------------------- testimonials

def _build_filters(args):
    # Always scoped to the logged-in account first; the other filters only narrow it.
    where, params, active = ["t.account_id = ?"], [current_account_id()], {}
    project_id = args.get("project", type=int)
    if project_id:
        where.append("t.project_id = ?")
        params.append(project_id)
        active["project"] = project_id
    status = args.get("status", "")
    if status in STATUSES:
        where.append("t.status = ?")
        params.append(status)
        active["status"] = status
    elif status == "publishable":
        where.append(PUBLISHABLE_SQL)
        active["status"] = status
    permission = args.get("permission", "")
    if permission in ("yes", "no"):
        where.append("t.permission_granted = ?")
        params.append(1 if permission == "yes" else 0)
        active["permission"] = permission
    rating = args.get("rating", "")
    if rating in ("1", "2", "3", "4", "5"):
        where.append("t.rating >= ?")
        params.append(int(rating))
        active["rating"] = rating
    elif rating == "none":
        where.append("t.rating IS NULL")
        active["rating"] = rating
    date_from = parse_date(args.get("from"))
    if date_from:
        where.append("t.submitted_at >= ?")
        params.append(date_from + " 00:00:00")
        active["from"] = date_from
    date_to = parse_date(args.get("to"))
    if date_to:
        next_day = (datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        where.append("t.submitted_at < ?")
        params.append(next_day + " 00:00:00")
        active["to"] = date_to
    q = (args.get("q") or "").strip()[:100]
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(t.customer_name LIKE ? ESCAPE '\\' OR t.business LIKE ? ESCAPE '\\' "
                     "OR t.feedback LIKE ? ESCAPE '\\')")
        params.extend([like, like, like])
        active["q"] = q
    sort = args.get("sort", "newest")
    order = {
        "oldest": "t.submitted_at ASC, t.id ASC",
        "rating": "t.rating IS NULL, t.rating DESC, t.submitted_at DESC",
    }.get(sort, "t.submitted_at DESC, t.id DESC")
    if sort in ("oldest", "rating"):
        active["sort"] = sort
    return "WHERE " + " AND ".join(where), params, order, active


_SELECT_T = ("SELECT t.*, p.name AS project_name FROM testimonials t "
             "JOIN projects p ON p.id = t.project_id AND p.account_id = t.account_id ")


@bp.route("/testimonials")
@login_required
def testimonials():
    where, params, order, active = _build_filters(request.args)
    db = get_db()
    total = db.execute(f"SELECT COUNT(*) FROM testimonials t {where}", params).fetchone()[0]
    page_size = current_app.config["PAGE_SIZE"]
    pages = max(1, -(-total // page_size))
    page = min(max(1, request.args.get("page", 1, type=int)), pages)
    rows = db.execute(f"{_SELECT_T} {where} ORDER BY {order} LIMIT ? OFFSET ?",
                      params + [page_size, (page - 1) * page_size]).fetchall()
    project_list = db.execute("SELECT id, name, is_demo FROM projects WHERE account_id = ? ORDER BY name",
                              (current_account_id(),)).fetchall()
    return render_template("testimonials.html", rows=rows, total=total, page=page, pages=pages,
                           filters=active, projects=project_list, statuses=STATUSES)


@bp.route("/testimonials/export.csv")
@login_required
def export_filtered():
    where, params, order, _ = _build_filters(request.args)
    rows = get_db().execute(f"{_SELECT_T} {where} ORDER BY {order}", params).fetchall()
    return _csv_response(rows)


def _csv_safe(value):
    """Neutralise spreadsheet formula injection."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _csv_response(rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_COLUMNS)
    for r in rows:
        record = dict(r)
        record["project"] = r["project_name"]
        writer.writerow([_csv_safe(record.get(c, "")) if record.get(c) is not None else ""
                         for c in CSV_COLUMNS])
    filename = f"testimonials-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@bp.route("/testimonials/bulk", methods=["POST"])
@login_required
def bulk():
    ids = [i for i in request.form.getlist("ids", type=int) if i > 0][:1000]
    action = request.form.get("action", "")
    back = safe_next(request.form.get("next"), url_for("admin.testimonials"))
    if not ids:
        flash("Select at least one testimonial first.", "error")
        return redirect(back)
    db = get_db()
    aid = current_account_id()
    marks = ",".join("?" * len(ids))
    # Ids that belong to another account are silently ignored: they never match "account_id = ?".
    if action == "export":
        rows = db.execute(f"{_SELECT_T} WHERE t.id IN ({marks}) AND t.account_id = ? "
                          "ORDER BY t.submitted_at DESC", ids + [aid]).fetchall()
        return _csv_response(rows)
    rows = db.execute(f"SELECT id, customer_name, project_id, is_demo FROM testimonials "
                      f"WHERE id IN ({marks}) AND account_id = ?", ids + [aid]).fetchall()
    own_ids = [r["id"] for r in rows]
    if action in STATUSES:
        now = utcnow()
        for r in rows:
            db.execute("UPDATE testimonials SET status = ?, status_changed_at = ?, updated_at = ? "
                       "WHERE id = ? AND account_id = ?", (action, now, now, r["id"], aid))
            log_activity(action, f"Testimonial from {r['customer_name']} marked {action}",
                         project_id=r["project_id"], testimonial_id=r["id"], is_demo=r["is_demo"])
        flash(f"{len(rows)} testimonial(s) marked {action}.", "success")
    elif action == "delete":
        for r in rows:
            log_activity("deleted", f"Testimonial from {r['customer_name']} deleted",
                         project_id=r["project_id"], is_demo=r["is_demo"])
        if own_ids:
            own_marks = ",".join("?" * len(own_ids))
            db.execute(f"DELETE FROM testimonials WHERE id IN ({own_marks}) AND account_id = ?", own_ids + [aid])
        flash(f"{len(rows)} testimonial(s) deleted.", "success")
    else:
        abort(400)
    db.commit()
    return redirect(back)


@bp.route("/testimonials/<int:tid>")
@login_required
def testimonial_detail(tid):
    t = testimonial_or_404(tid)
    original = json.loads(t["original_json"]) if t["original_json"] else None
    return render_template("testimonial_detail.html", t=t, original=original, statuses=STATUSES,
                           limits=LIMITS, values=dict(t), errors={})


@bp.route("/testimonials/<int:tid>/status", methods=["POST"])
@login_required
def testimonial_status(tid):
    t = testimonial_or_404(tid)
    status = request.form.get("status")
    if status not in STATUSES:
        abort(400)
    db = get_db()
    now = utcnow()
    db.execute("UPDATE testimonials SET status = ?, status_changed_at = ?, updated_at = ? "
               "WHERE id = ? AND account_id = ?", (status, now, now, tid, current_account_id()))
    log_activity(status, f"Testimonial from {t['customer_name']} marked {status}",
                 project_id=t["project_id"], testimonial_id=tid, is_demo=t["is_demo"])
    db.commit()
    msg = f"Marked {status}."
    if status == "approved" and not t["permission_granted"]:
        msg += " It won't appear publicly until publication permission is recorded."
    flash(msg, "success")
    return redirect(safe_next(request.form.get("next"), url_for("admin.testimonial_detail", tid=tid)))


@bp.route("/testimonials/<int:tid>/permission", methods=["POST"])
@login_required
def testimonial_permission(tid):
    t = testimonial_or_404(tid)
    action = request.form.get("action")
    db = get_db()
    now = utcnow()
    aid = current_account_id()
    if action == "grant":
        note, err = validate_permission_note(request.form.get("note"))
        if err:
            flash(err, "error")
            return redirect(url_for("admin.testimonial_detail", tid=tid) + "#permission")
        db.execute(
            "UPDATE testimonials SET permission_granted = 1, permission_source = 'owner', "
            "permission_note = ?, permission_at = ?, updated_at = ? WHERE id = ? AND account_id = ?",
            (note, now, now, tid, aid),
        )
        log_activity("permission_recorded",
                     f"Publication permission recorded for {t['customer_name']}: {note}",
                     project_id=t["project_id"], testimonial_id=tid, is_demo=t["is_demo"])
        flash("Publication permission recorded.", "success")
    elif action == "revoke":
        db.execute(
            "UPDATE testimonials SET permission_granted = 0, permission_source = '', "
            "permission_note = '', permission_at = NULL, updated_at = ? WHERE id = ? AND account_id = ?",
            (now, tid, aid),
        )
        log_activity("permission_revoked",
                     f"Publication permission withdrawn for {t['customer_name']}",
                     project_id=t["project_id"], testimonial_id=tid, is_demo=t["is_demo"])
        flash("Publication permission withdrawn. The testimonial is no longer public.", "success")
    else:
        abort(400)
    db.commit()
    return redirect(url_for("admin.testimonial_detail", tid=tid))


@bp.route("/testimonials/<int:tid>/edit", methods=["POST"])
@login_required
def testimonial_edit(tid):
    t = testimonial_or_404(tid)
    data, errors = validate_testimonial(request.form, include_rating=False)
    if errors:
        original = json.loads(t["original_json"]) if t["original_json"] else None
        values = dict(t)
        values.update(data)
        return render_template("testimonial_detail.html", t=t, original=original, statuses=STATUSES,
                               limits=LIMITS, values=values, errors=errors, open_edit=True), 400
    changed = [f for f in EDITABLE_FIELDS if data[f] != t[f]]
    if not changed:
        flash("No changes to save.", "info")
        return redirect(url_for("admin.testimonial_detail", tid=tid))
    # Keep the client's original wording so edits are always auditable.
    original_json = t["original_json"] or json.dumps({f: t[f] for f in EDITABLE_FIELDS})
    now = utcnow()
    db = get_db()
    db.execute(
        "UPDATE testimonials SET customer_name = ?, business = ?, feedback = ?, website = ?, "
        "edited = 1, edited_at = ?, original_json = ?, updated_at = ? WHERE id = ? AND account_id = ?",
        (data["customer_name"], data["business"], data["feedback"], data["website"],
         now, original_json, now, tid, current_account_id()),
    )
    log_activity("edited", f"Testimonial from {data['customer_name']} edited ({', '.join(changed)})",
                 project_id=t["project_id"], testimonial_id=tid, is_demo=t["is_demo"])
    db.commit()
    flash("Changes saved. The original wording is kept for reference.", "success")
    if "feedback" in changed and t["permission_granted"]:
        flash("Permission was given for the original wording. If your edit changes the meaning, "
              "re-confirm with the client before publishing.", "warning")
    return redirect(url_for("admin.testimonial_detail", tid=tid))


@bp.route("/testimonials/<int:tid>/delete", methods=["POST"])
@login_required
def testimonial_delete(tid):
    t = testimonial_or_404(tid)
    db = get_db()
    log_activity("deleted", f"Testimonial from {t['customer_name']} deleted",
                 project_id=t["project_id"], is_demo=t["is_demo"])
    db.execute("DELETE FROM testimonials WHERE id = ? AND account_id = ?", (tid, current_account_id()))
    db.commit()
    flash("Testimonial deleted.", "success")
    return redirect(url_for("admin.testimonials"))


# --------------------------------------------------------------------------- demo data

@bp.route("/demo/load", methods=["POST"])
@login_required
def demo_load():
    demo.reset_demo_data(current_account_id(), reload=True)
    flash("Demo data loaded. Everything marked DEMO is sample data, not real testimonials.", "success")
    return redirect(url_for("admin.dashboard"))


@bp.route("/demo/clear", methods=["POST"])
@login_required
def demo_clear():
    demo.reset_demo_data(current_account_id(), reload=False)
    flash("Demo data removed. Your own projects and testimonials were not touched.", "success")
    return redirect(url_for("admin.dashboard"))
