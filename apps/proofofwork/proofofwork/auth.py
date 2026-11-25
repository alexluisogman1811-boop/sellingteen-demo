"""Business accounts, sign-up, login/logout, roles, and team member management.

Each business is an *account*. Signing up creates the account plus its first manager. Managers add workers to
their own account only, and every user belongs to exactly one account.
"""
import functools
import sqlite3
import time

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db
from .utils import EMAIL_RE, Form, now_iso, safe_next

bp = Blueprint("auth", __name__)

MIN_PASSWORD_LENGTH = 10
MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 300
MAX_SIGNUPS_PER_HOUR = 10
ROLES = {"manager": "Manager", "worker": "Worker"}

# In-process throttles keyed by (ip, email) / ip. They reset when the process restarts.
_failed_logins = {}
_signups = {}

# Same cost as a real check so unknown emails take similar time to wrong passwords.
_DUMMY_HASH = generate_password_hash("not-a-real-password")


def _is_locked(key):
    count, first = _failed_logins.get(key, (0, 0.0))
    if time.monotonic() - first > LOCKOUT_SECONDS:
        _failed_logins.pop(key, None)
        return False
    return count >= MAX_FAILED_LOGINS


def _record_failure(key):
    now = time.monotonic()
    count, first = _failed_logins.get(key, (0, now))
    if now - first > LOCKOUT_SECONDS:
        count, first = 0, now
    _failed_logins[key] = (count + 1, first)


def _signup_allowed(ip):
    now = time.monotonic()
    recent = [t for t in _signups.get(ip, []) if now - t < 3600]
    _signups[ip] = recent
    return len(recent) < MAX_SIGNUPS_PER_HOUR


def validate_password(password, confirm=None):
    errors = []
    if len(password) < MIN_PASSWORD_LENGTH:
        errors.append(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password) > 200:
        errors.append("Password is too long.")
    if confirm is not None and password != confirm:
        errors.append("Passwords do not match.")
    return errors


def _user_errors(email, name, password, role):
    errors = validate_password(password)
    if role not in ROLES:
        errors.append("Unknown role.")
    if not EMAIL_RE.match(email or ""):
        errors.append("Enter a valid email address.")
    if not (name or "").strip():
        errors.append("Name is required.")
    return errors


def create_account(business_name, email, owner_name, password):
    """Create a business account and its first manager in one transaction. Returns (account_id, user_id)."""
    errors = _user_errors(email, owner_name, password, "manager")
    if not (business_name or "").strip():
        errors.insert(0, "Business name is required.")
    if errors:
        raise ValueError(" ".join(errors))
    db = get_db()
    try:
        with db:
            account_id = db.execute("INSERT INTO accounts (name, email, created_at) VALUES (?, ?, ?)",
                                    (business_name.strip(), email.strip(), now_iso())).lastrowid
            user_id = db.execute(
                "INSERT INTO users (account_id, email, name, password_hash, role, created_at)"
                " VALUES (?, ?, ?, ?, 'manager', ?)",
                (account_id, email.strip(), owner_name.strip(), generate_password_hash(password), now_iso())).lastrowid
    except sqlite3.IntegrityError:
        raise ValueError("An account with that email already exists. Try logging in instead.") from None
    return account_id, user_id


def _only_account_id(db):
    """For command-line use on an install with a single business."""
    rows = db.execute("SELECT id FROM accounts ORDER BY id LIMIT 2").fetchall()
    if len(rows) == 1:
        return rows[0][0]
    if not rows:
        return db.execute("INSERT INTO accounts (name, email, created_at) VALUES (?, '', ?)",
                          (current_app.config["COMPANY_NAME"], now_iso())).lastrowid
    raise ValueError("There are several business accounts; say which one the user belongs to.")


def create_user(email, name, password, role, account_id=None):
    """Insert a user into a business account. Raises ValueError with a readable message on bad input.

    account_id=None is only for the command line on a single-business install: the user joins the only account
    (one is created if none exists yet)."""
    errors = _user_errors(email, name, password, role)
    if errors:
        raise ValueError(" ".join(errors))
    db = get_db()
    try:
        with db:
            if account_id is None:
                account_id = _only_account_id(db)
                if not db.execute("SELECT email FROM accounts WHERE id = ?", (account_id,)).fetchone()[0] \
                        and role == "manager":
                    db.execute("UPDATE accounts SET email = ? WHERE id = ?", (email.strip(), account_id))
            cur = db.execute(
                "INSERT INTO users (account_id, email, name, password_hash, role, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (account_id, email.strip(), name.strip(), generate_password_hash(password), role, now_iso()),
            )
    except sqlite3.IntegrityError:
        raise ValueError("A user with that email already exists.") from None
    return cur.lastrowid


@bp.before_app_request
def load_logged_in_user():
    g.user = None
    g.account = None
    user_id = session.get("user_id")
    if user_id is not None:
        row = get_db().execute(
            "SELECT u.id, u.email, u.name, u.role, u.account_id, a.name AS account_name, a.email AS account_email,"
            " a.is_demo, a.created_at AS account_created_at, a.paid_until"
            " FROM users u JOIN accounts a ON a.id = u.account_id WHERE u.id = ? AND u.active = 1", (user_id,)
        ).fetchone()
        if row is None:
            session.clear()
        else:
            g.user = row
            g.account = {"id": row["account_id"], "name": row["account_name"], "email": row["account_email"],
                         "is_demo": row["is_demo"], "created_at": row["account_created_at"],
                         "paid_until": row["paid_until"]}


def _login_redirect():
    # Include the URL prefix (e.g. /proofofwork) so the user comes back to the same page after logging in.
    return redirect(url_for("auth.login", next=request.script_root + request.full_path.rstrip("?")))


def login_required(view):
    @functools.wraps(view)
    def wrapped(**kwargs):
        if g.user is None:
            return _login_redirect()
        return view(**kwargs)
    return wrapped


def manager_required(view):
    @functools.wraps(view)
    def wrapped(**kwargs):
        if g.user is None:
            return _login_redirect()
        if g.user["role"] != "manager":
            abort(403)
        return view(**kwargs)
    return wrapped


def _start_session(user_id):
    session.clear()  # prevents session fixation; a fresh CSRF token is issued next render
    session.permanent = True
    session["user_id"] = user_id


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user is not None:
        return redirect(url_for("main.index"))
    next_url = safe_next(request.values.get("next"))
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()[:254]
        password = request.form.get("password") or ""
        key = (request.remote_addr or "", email.lower())
        if _is_locked(key):
            flash("Too many failed attempts. Wait a few minutes and try again.", "error")
            return render_template("auth/login.html", email=email, next=next_url), 429
        user = get_db().execute(
            "SELECT * FROM users WHERE email = ? AND active = 1", (email,)
        ).fetchone()
        if user is None:
            check_password_hash(_DUMMY_HASH, password)
        elif check_password_hash(user["password_hash"], password):
            _failed_logins.pop(key, None)
            _start_session(user["id"])
            return redirect(next_url or url_for("main.index"))
        _record_failure(key)
        flash("Incorrect email or password.", "error")
        return render_template("auth/login.html", email=email, next=next_url), 401
    return render_template("auth/login.html", email="", next=next_url)


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for("auth.login"))


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    """Public sign-up: creates a new business account and its first manager."""
    if g.user is not None:
        return redirect(url_for("main.dashboard"))
    values = {"business_name": "", "name": "", "email": ""}
    if request.method == "POST":
        form = Form(request.form)
        values = {
            "business_name": form.text("business_name", "Business name", 200, True),
            "name": form.text("name", "Your name", 100, True),
            "email": form.text("email", "Email", 254, True),
        }
        password = request.form.get("password") or ""
        errors = form.errors + validate_password(password, request.form.get("confirm") or "")
        if not errors and not _signup_allowed(request.remote_addr or ""):
            errors.append("Too many new accounts from this connection. Please try again later.")
        if not errors:
            try:
                account_id, user_id = create_account(values["business_name"], values["email"], values["name"],
                                                     password)
            except ValueError as exc:
                errors.append(str(exc))
            else:
                _signups.setdefault(request.remote_addr or "", []).append(time.monotonic())
                _start_session(user_id)
                flash("Welcome to ProofOfWork. Your business account is ready"
                      + (f" and your {current_app.config['TRIAL_DAYS']}-day free trial has started."
                         if current_app.config["BILLING_ENABLED"] else "."), "success")
                if form.checkbox("load_demo"):
                    from .demo import seed_demo
                    try:
                        seed_demo(account_id)
                    except Exception:
                        current_app.logger.exception("Demo data could not be loaded")
                        flash("Demo data could not be loaded; see the server log. You can retry from Admin.", "error")
                return redirect(url_for("main.dashboard"))
        for error in errors:
            flash(error, "error")
        return render_template("auth/signup.html", values=values), 400
    return render_template("auth/signup.html", values=values)


@bp.route("/setup")
def setup():
    """The old one-time setup page. Every business now signs up for its own account."""
    return redirect(url_for("auth.signup"))


@bp.route("/users")
@manager_required
def users():
    rows = get_db().execute("SELECT * FROM users WHERE account_id = ? ORDER BY active DESC, name",
                            (g.user["account_id"],)).fetchall()
    return render_template("auth/users.html", users=rows, roles=ROLES)


@bp.route("/users/new", methods=["GET", "POST"])
@manager_required
def user_new():
    values = {"name": "", "email": "", "role": "worker"}
    if request.method == "POST":
        form = Form(request.form)
        values = {
            "name": form.text("name", "Name", 100, True),
            "email": form.text("email", "Email", 254, True),
            "role": request.form.get("role", "worker"),
        }
        errors = form.errors
        if not errors:
            try:
                # New team members always join the manager's own business.
                create_user(values["email"], values["name"], request.form.get("password") or "", values["role"],
                            account_id=g.user["account_id"])
            except ValueError as exc:
                errors.append(str(exc))
            else:
                flash(f"Account created for {values['name']}.", "success")
                return redirect(url_for("auth.users"))
        for error in errors:
            flash(error, "error")
        return render_template("auth/user_form.html", values=values, roles=ROLES), 400
    return render_template("auth/user_form.html", values=values, roles=ROLES)


@bp.route("/users/<int:user_id>/toggle", methods=["POST"])
@manager_required
def user_toggle(user_id):
    if user_id == g.user["id"]:
        flash("You cannot deactivate your own account.", "error")
        return redirect(url_for("auth.users"))
    db = get_db()
    row = db.execute("SELECT id, active, name FROM users WHERE id = ? AND account_id = ?",
                     (user_id, g.user["account_id"])).fetchone()
    if row is None:
        abort(404)
    db.execute("UPDATE users SET active = ? WHERE id = ? AND account_id = ?",
               (0 if row["active"] else 1, user_id, g.user["account_id"]))
    db.commit()
    flash(f"{row['name']} {'deactivated' if row['active'] else 'reactivated'}.", "success")
    return redirect(url_for("auth.users"))


@bp.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        db = get_db()
        row = db.execute("SELECT password_hash FROM users WHERE id = ?", (g.user["id"],)).fetchone()
        new = request.form.get("new_password") or ""
        errors = []
        if not check_password_hash(row["password_hash"], request.form.get("current_password") or ""):
            errors.append("Current password is incorrect.")
        errors += validate_password(new, request.form.get("confirm") or "")
        if not errors:
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(new), g.user["id"]))
            db.commit()
            flash("Password updated.", "success")
            return redirect(url_for("main.dashboard"))
        for error in errors:
            flash(error, "error")
        return render_template("auth/password.html"), 400
    return render_template("auth/password.html")


@bp.route("/account/business", methods=["POST"])
@manager_required
def business_name():
    """Managers can rename their business (it is shown at the top of customer reports)."""
    form = Form(request.form)
    name = form.text("business_name", "Business name", 200, True)
    if form.errors:
        for error in form.errors:
            flash(error, "error")
        return render_template("auth/password.html"), 400
    db = get_db()
    db.execute("UPDATE accounts SET name = ? WHERE id = ?", (name, g.user["account_id"]))
    db.commit()
    flash("Business name updated.", "success")
    return redirect(url_for("auth.change_password"))
