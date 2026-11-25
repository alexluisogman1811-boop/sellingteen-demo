import json
import os
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from functools import wraps
from urllib.parse import urlparse

from dotenv import load_dotenv
from flask import (
    Flask, abort, current_app, flash, g, redirect, render_template, request, session, url_for,
)
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import stripe

from . import account as account_pages
from . import analytics
from . import billing
from . import db as store
from . import legal
from . import mailer
from . import settings
from .ai import QuoteError, generate_quote
from .pricing import validate_and_normalize

CURRENCIES = ["USD", "EUR", "GBP", "CAD", "AUD", "NZD", "CHF", "SEK", "NOK", "DKK", "SGD", "THB", "ZAR", "INR"]
MAX_REQUEST_CHARS = 5000
MAX_PROFILE_FIELD_CHARS = 5000
MAX_PASSWORD_CHARS = 128

# Hash used when an email isn't registered, so a failed login takes the same
# time whether or not the account exists.
_DUMMY_HASH = generate_password_hash("not-a-real-password")

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    # Stripe's hosted pages are form-action targets because our upgrade/manage
    # forms redirect there.
    "form-action 'self' https://checkout.stripe.com https://billing.stripe.com; "
    "frame-ancestors 'none'; base-uri 'self'; object-src 'none'"
)
USER_COLUMNS = ("id, email, stripe_customer_id, subscription_status, subscription_period_end, "
                "cancel_at_period_end, payment_failed, onboarded, created_at, paid_until")

# Onboarding: (title, intro, profile fields saved on that step)
ONBOARDING_STEPS = [
    ("Your business", "The basics customers ask about.",
     ["business_name", "service_area", "opening_hours"]),
    ("Your services", "What you offer, and roughly how long each job takes.",
     ["services"]),
    ("Your prices", "Quotes are calculated only from these numbers — the AI never makes prices up.",
     ["currency", "basic_price", "deep_price", "bedrooms_included", "bathrooms_included",
      "extra_bedroom_price", "extra_bathroom_price", "addons"]),
]


class RateLimiter:
    """In-memory sliding-window limiter. Per process; resets on restart."""

    def __init__(self, limit, window_seconds):
        self.limit, self.window = limit, window_seconds
        self.hits = defaultdict(deque)
        self.lock = threading.Lock()

    def hit(self, key):
        """Record an attempt. Returns False if the key is over its limit."""
        now = time.monotonic()
        with self.lock:
            if len(self.hits) > 10_000:
                # Forget keys whose attempts have all expired, so memory can't grow without bound.
                for k in [k for k, v in self.hits.items() if not v or v[-1] <= now - self.window]:
                    del self.hits[k]
            q = self.hits[key]
            while q and q[0] <= now - self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True

    def clear(self, key):
        with self.lock:
            self.hits.pop(key, None)


def create_app():
    load_dotenv()
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)

    secret = os.environ.get("SECRET_KEY")
    if not secret or secret == "change-me":
        if os.environ.get("FLASK_DEBUG") != "1":
            raise RuntimeError("Set SECRET_KEY in .env (see .env.example).")
        secret = "dev-only-insecure-key"
    app.config.update(
        SECRET_KEY=secret,
        DATABASE_PATH=os.environ.get("DATABASE_PATH", os.path.join(app.instance_path, "quoteflow.db")),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
        MAX_CONTENT_LENGTH=256 * 1024,
        # Free trial + manual activation on top of the Free/Pro plans (see billing.py).
        # BILLING_ENABLED=0 turns the trial off (Free unless a Stripe subscription is active).
        BILLING_ENABLED=os.environ.get("BILLING_ENABLED", "1") == "1",
        TRIAL_DAYS=int(os.environ.get("TRIAL_DAYS", "30") or 30),
        PAYMENT_LINK=os.environ.get("PAYMENT_LINK", "").strip(),
        PRICE_TEXT=os.environ.get("PRICE_TEXT", "$15 per month for 100 quotes").strip(),
        SUPPORT_EMAIL=os.environ.get("SUPPORT_EMAIL", "").strip() or legal.DEFAULT_SUPPORT_EMAIL,
        LEGAL_NAME=os.environ.get("LEGAL_NAME", "sellingteen").strip(),
        RUNTIME_ENV=settings.snapshot(),   # see settings.py: survives the shared host's env reset
    )
    # Optional email, only for "Forgot password?" links. OFF unless SMTP_HOST and SMTP_FROM are set.
    app.config["MAIL"] = mailer.mail_settings()
    app.extensions["limits"] = {
        "login": RateLimiter(10, 15 * 60),     # failed logins per email+IP
        "signup": RateLimiter(10, 60 * 60),    # signups per IP
        "quote": RateLimiter(30, 60 * 60),     # AI generations per user
        "reset_ip": RateLimiter(10, 60 * 60),     # password-reset emails asked for, per IP
        "reset_email": RateLimiter(3, 60 * 60),   # password-reset emails asked for, per email
    }
    if os.environ.get("BEHIND_PROXY") == "1":
        # Behind one HTTPS reverse proxy (Render, Railway, nginx…): use its X-Forwarded-* headers so
        # rate limits see the real client IP and redirects use https. Don't enable without a proxy,
        # or clients could spoof their IP.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    store.init_db(app)
    register_routes(app)
    account_pages.register(app, login_required, limiter, MAX_PASSWORD_CHARS)
    app.before_request(billing.check_access)   # after load_user_and_check_csrf (sets g.user)
    legal.register(app)
    billing.register_cli(app)
    if os.environ.get("AI_MOCK", "").strip() != "0":
        app.logger.warning("DEMO MODE - AI is off (AI_MOCK is not 0): quotes are written from the built-in templates and "
                           "no AI API calls are made. Set AI_MOCK=0 and ANTHROPIC_API_KEY to use the real AI.")
    return app


# ---------- helpers ----------

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            # script_root keeps the address prefix when the app runs under one (e.g. /quoteflow)
            return redirect(url_for("login", next=(request.script_root + request.full_path).rstrip("?")))
        return view(*args, **kwargs)
    return wrapped


def limiter(name):
    return current_app.extensions["limits"][name]


def safe_next(target):
    """Only allow redirects to paths on this site."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return None
    parsed = urlparse(target)
    return target if not parsed.scheme and not parsed.netloc else None


def too_long(fields, limit):
    return any(len(v) > limit for v in fields)


def profile_completeness(profile):
    checks = [
        profile["business_name"],
        profile["services"],
        profile["basic_price"] or profile["deep_price"],
        profile["bedrooms_included"] and profile["bathrooms_included"],
        profile["service_area"],
        profile["contact_info"],
        profile["policies"],
    ]
    return sum(1 for c in checks if (c or "").strip()), len(checks)


def register_routes(app):

    @app.before_request
    def load_user_and_check_csrf():
        uid = session.get("user_id")
        g.user = None
        if uid is not None:
            g.user = store.get_db().execute(f"SELECT {USER_COLUMNS} FROM users WHERE id = ?", (uid,)).fetchone()
            if g.user is None:
                session.clear()
        # The Stripe webhook is authenticated by its signature instead of a CSRF token.
        if request.method == "POST" and request.endpoint != "stripe_webhook":
            token = request.form.get("csrf_token", "")
            if not token or not secrets.compare_digest(token, session.get("csrf_token", "")):
                abort(400, "Your session expired or the page was open too long. Go back, refresh the page, and try again.")

    @app.after_request
    def security_headers(resp):
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        if g.get("user") is not None:
            # Don't let the back button show private pages after logout.
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.errorhandler(HTTPException)
    def http_error(e):
        messages = {
            404: "We couldn't find that page.",
            413: "That submission is too large.",
        }
        return render_template(
            "error.html", code=e.code,
            message=messages.get(e.code) or (e.description if e.code == 400 else "Something went wrong."),
        ), e.code

    @app.errorhandler(Exception)
    def server_error(e):
        app.logger.exception("Unhandled error on %s", request.path)
        return render_template("error.html", code=500,
                               message="Something went wrong on our side. Please try again."), 500

    @app.context_processor
    def template_helpers():
        def csrf_token():
            if "csrf_token" not in session:
                session["csrf_token"] = secrets.token_urlsafe(32)
            return session["csrf_token"]

        def asset(filename):
            # Version static files by modification time so browsers pick up changes.
            mtime = int(os.path.getmtime(os.path.join(app.static_folder, filename)))
            return url_for("static", filename=filename, v=mtime)
        payment_problem = bool(g.get("user")) and (
            g.user["subscription_status"] in ("past_due", "unpaid") or bool(g.user["payment_failed"]))
        return {"csrf_token": csrf_token, "asset": asset, "payment_problem": payment_problem,
                "plans": billing.PLANS, "mail_on": account_pages.mail_on(), "analytics_admin": analytics.is_admin(g.get("user")),
                "support_email": (settings.get("SUPPORT_EMAIL") or "").strip() or app.config["SUPPORT_EMAIL"]}

    # ---------- public ----------

    @app.get("/")
    def landing():
        return render_template("landing.html")

    @app.route("/signup", methods=["GET", "POST"])
    def signup():
        if g.user:
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            error = None
            if "@" not in email or "." not in email.split("@")[-1] or len(email) > 254:
                error = "Please enter a valid email address."
            elif len(password) < 8:
                error = "Password must be at least 8 characters."
            elif len(password) > MAX_PASSWORD_CHARS:
                error = f"Password must be at most {MAX_PASSWORD_CHARS} characters."
            elif not limiter("signup").hit(request.remote_addr):
                error = "Too many sign-ups from this network. Please try again later."
            else:
                db = store.get_db()
                try:
                    cur = db.execute(
                        "INSERT INTO users (email, password_hash) VALUES (?, ?)",
                        (email, generate_password_hash(password)),
                    )
                    db.commit()
                except sqlite3.IntegrityError:
                    error = "An account with this email already exists. Try logging in."
                else:
                    session.clear()
                    session["user_id"] = cur.lastrowid
                    analytics.track("signup", cur.lastrowid)
                    return redirect(url_for("onboarding", step=1))
            flash(error, "error")
            return render_template("auth.html", mode="signup", email=email), 400
        return render_template("auth.html", mode="signup")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if g.user:
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")[:MAX_PASSWORD_CHARS]
            key = (email, request.remote_addr)
            if not limiter("login").hit(key):
                flash("Too many failed attempts. Please wait 15 minutes and try again.", "error")
                return render_template("auth.html", mode="login", email=email), 429
            user = store.get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            if check_password_hash(user["password_hash"] if user else _DUMMY_HASH, password) and user:
                limiter("login").clear(key)
                session.clear()
                session["user_id"] = user["id"]
                return redirect(safe_next(request.args.get("next")) or url_for("dashboard"))
            flash("Incorrect email or password.", "error")
            return render_template("auth.html", mode="login", email=email), 400
        return render_template("auth.html", mode="login")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("landing"))

    # ---------- app ----------

    @app.get("/dashboard")
    @login_required
    def dashboard():
        profile = store.get_profile(g.user["id"])
        quotes = store.get_db().execute(
            "SELECT id, customer_request, price, status, created_at FROM quotes "
            "WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT 50",
            (g.user["id"],),
        ).fetchall()
        done, total = profile_completeness(profile)
        return render_template("dashboard.html", profile=profile, quotes=quotes, done=done, total=total,
                               statuses=store.QUOTE_STATUSES, usage=billing.usage_summary(g.user))

    @app.route("/profile", methods=["GET", "POST"])
    @login_required
    def profile():
        if request.method == "POST":
            data = {f: request.form.get(f, "") for f in store.PROFILE_FIELDS}
            if data["currency"] not in CURRENCIES:
                data["currency"] = "USD"
            errors = validate_and_normalize(data)
            if too_long(data.values(), MAX_PROFILE_FIELD_CHARS):
                errors.append(f"Each field can be at most {MAX_PROFILE_FIELD_CHARS} characters.")
            if errors:
                for e in errors:
                    flash(e, "error")
                return render_template("profile.html", profile=data, currencies=CURRENCIES), 400
            store.save_profile(g.user["id"], data)
            flash("Business profile saved.")
            return redirect(url_for("profile"))
        return render_template("profile.html", profile=store.get_profile(g.user["id"]), currencies=CURRENCIES)

    @app.route("/quotes/new", methods=["GET", "POST"])
    @login_required
    def new_quote():
        profile = store.get_profile(g.user["id"])
        # Only warn about what quoting really needs; contact info and policies are optional.
        essentials_missing = [label for label, value in [
            ("business name", profile["business_name"]),
            ("services", profile["services"]),
            ("a basic or deep cleaning price", profile["basic_price"] or profile["deep_price"]),
        ] if not value.strip()]
        customer_request = ""

        def page(code=200):
            return render_template("quote_new.html", customer_request=customer_request,
                                   essentials_missing=essentials_missing, usage=billing.usage_summary(g.user)), code

        if request.method == "POST":
            customer_request = request.form.get("customer_request", "").strip()
            if not customer_request:
                flash("Paste the customer's message first.", "error")
            elif len(customer_request) > MAX_REQUEST_CHARS:
                flash(f"That message is too long (max {MAX_REQUEST_CHARS} characters).", "error")
            elif not limiter("quote").hit(g.user["id"]):
                flash("You've generated a lot of quotes in the last hour. Please wait a little and try again.", "error")
            elif not billing.reserve_quote(g.user):
                # The page shows the limit notice (with upgrade link) from `usage`.
                if billing.plan_for(g.user) == "free":
                    analytics.track("free_limit_reached", g.user["id"])
                return page(403)
            else:
                try:
                    result = generate_quote(profile, customer_request)
                except QuoteError as e:
                    billing.release_quote(g.user)  # failed attempts don't use up the allowance
                    flash(str(e), "error")
                except Exception:
                    billing.release_quote(g.user)
                    app.logger.exception("Quote generation failed")
                    flash("Something went wrong while creating the quote. Please try again.", "error")
                else:
                    db = store.get_db()
                    cur = db.execute(
                        "INSERT INTO quotes (user_id, customer_request, result_json, reply_text, price, duration, services, status) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 'draft')",
                        (g.user["id"], customer_request, json.dumps(result), result["customer_reply"],
                         result["suggested_price"], result["estimated_time"], "\n".join(result["services_included"])),
                    )
                    db.commit()
                    analytics.track("quote_generated", g.user["id"])
                    return redirect(url_for("view_quote", quote_id=cur.lastrowid))
            return page(400)
        return page()

    def _get_quote(quote_id):
        q = store.get_db().execute(
            "SELECT * FROM quotes WHERE id = ? AND user_id = ?", (quote_id, g.user["id"])
        ).fetchone()
        if q is None:
            abort(404)
        return q

    def _render_quote(q, code=200):
        return render_template("quote_view.html", quote=q, result=json.loads(q["result_json"]),
                               statuses=store.QUOTE_STATUSES), code

    @app.get("/quotes/<int:quote_id>")
    @login_required
    def view_quote(quote_id):
        return _render_quote(_get_quote(quote_id))

    @app.post("/quotes/<int:quote_id>")
    @login_required
    def save_quote(quote_id):
        q = _get_quote(quote_id)
        f = request.form
        status = f.get("status", "saved")
        if status not in store.QUOTE_STATUSES or status == "draft":
            status = "saved"
        values = {
            "price": f.get("price", "").strip(),
            "duration": f.get("duration", "").strip(),
            "services": "\n".join(s.strip() for s in f.get("services", "").splitlines() if s.strip()),
            "reply_text": f.get("reply_text", ""),
            "status": status,
        }
        if too_long([values["price"], values["duration"]], 200) or too_long(
                [values["services"], values["reply_text"]], 10000):
            flash("One of the fields is too long. Please shorten it and save again.", "error")
            return _render_quote({**dict(q), **values}, 400)
        db = store.get_db()
        db.execute(
            "UPDATE quotes SET price = ?, duration = ?, services = ?, reply_text = ?, status = ?, "
            "updated_at = datetime('now') WHERE id = ? AND user_id = ?",
            (values["price"], values["duration"], values["services"], values["reply_text"], status,
             quote_id, g.user["id"]),
        )
        db.commit()
        flash("Quote saved.")
        return redirect(url_for("view_quote", quote_id=quote_id))

    # ---------- onboarding ----------

    @app.route("/onboarding", methods=["GET", "POST"])
    @login_required
    def onboarding():
        step = request.args.get("step", 1, type=int)
        if not 1 <= step <= len(ONBOARDING_STEPS):
            return redirect(url_for("onboarding", step=1))
        title, intro, fields = ONBOARDING_STEPS[step - 1]
        profile = store.get_profile(g.user["id"])

        if request.method == "POST":
            data = {**profile, **{f: request.form.get(f, "") for f in fields}}
            if data["currency"] not in CURRENCIES:
                data["currency"] = "USD"
            errors = validate_and_normalize(data)
            if step == 1 and not data["business_name"].strip():
                errors.append("Please enter your business name.")
            if step == 3 and not (data["basic_price"] or data["deep_price"]):
                errors.append("Enter at least a basic or deep cleaning price so QuoteFlow can quote. "
                              "(Or choose “Skip for now” and add prices later.)")
            if too_long(data.values(), MAX_PROFILE_FIELD_CHARS):
                errors.append(f"Each field can be at most {MAX_PROFILE_FIELD_CHARS} characters.")
            if errors:
                for e in errors:
                    flash(e, "error")
                return render_template("onboarding.html", step=step, steps=ONBOARDING_STEPS, title=title,
                                       intro=intro, profile=data, currencies=CURRENCIES), 400
            store.save_profile(g.user["id"], data)
            if step < len(ONBOARDING_STEPS):
                return redirect(url_for("onboarding", step=step + 1))
            db = store.get_db()
            db.execute("UPDATE users SET onboarded = 1 WHERE id = ?", (g.user["id"],))
            db.commit()
            flash("You're all set! Paste a customer's message below to create your first quote.")
            return redirect(url_for("new_quote"))

        return render_template("onboarding.html", step=step, steps=ONBOARDING_STEPS, title=title,
                               intro=intro, profile=profile, currencies=CURRENCIES)

    # ---------- help & legal ----------

    @app.get("/help")
    def help_page():
        return render_template("help.html")

    # /terms, /privacy, /refunds: see legal.py

    @app.get("/admin/analytics")
    @login_required
    def admin_analytics():
        if not analytics.is_admin(g.user):
            abort(404)
        return render_template("admin_analytics.html", report=analytics.report())

    # ---------- billing ----------

    @app.get("/billing")
    @login_required
    def billing_page():
        if request.args.get("checkout") == "canceled":
            flash("Checkout was canceled. You haven't been charged.")
            return redirect(url_for("billing_page"))
        period_end = g.user["subscription_period_end"]
        return render_template(
            "billing.html", usage=billing.usage_summary(g.user), configured=billing.is_configured(),
            period_end=time.strftime("%B %d, %Y", time.gmtime(period_end)) if period_end else "",
        )

    @app.get("/subscribe")
    @login_required
    def subscribe():
        if not app.config["BILLING_ENABLED"]:
            return redirect(url_for("billing_page"))
        return render_template("subscribe.html", access=g.get("access") or billing.access_status(g.user),
                               stripe_pro=billing.has_stripe_pro(g.user), configured=billing.is_configured(),
                               payment_link=app.config["PAYMENT_LINK"], price=app.config["PRICE_TEXT"])

    @app.post("/billing/checkout")
    @login_required
    def billing_checkout():
        # A free trial is Pro too, but trial users may still subscribe.
        if billing.has_paid_pro(g.user):
            flash("You're already on Pro.")
            return redirect(url_for("billing_page"))
        try:
            return redirect(billing.create_checkout_url(g.user), 303)
        except billing.BillingError as e:
            flash(str(e), "error")
            return redirect(url_for("billing_page"))

    @app.post("/billing/portal")
    @login_required
    def billing_portal():
        try:
            return redirect(billing.create_portal_url(g.user), 303)
        except billing.BillingError as e:
            flash(str(e), "error")
            return redirect(url_for("billing_page"))

    @app.get("/billing/success")
    @login_required
    def billing_success():
        session_id = request.args.get("session_id", "")
        if not session_id.startswith("cs_"):
            return redirect(url_for("billing_page"))
        try:
            if billing.confirm_checkout(g.user, session_id):
                flash("You're on Pro! You now have 100 quotes per month.")
            else:
                flash("Thanks! Your payment is being processed — your plan will update in a moment.")
        except billing.BillingError as e:
            flash(str(e), "error")
        return redirect(url_for("billing_page"))

    @app.post("/stripe/webhook")
    def stripe_webhook():
        try:
            event = billing.construct_event(request.get_data(), request.headers.get("Stripe-Signature"))
        except (ValueError, stripe.SignatureVerificationError) as e:
            app.logger.warning("Rejected Stripe webhook: %s", type(e).__name__)
            return {"error": "invalid signature"}, 400
        billing.handle_event(event)  # an exception here returns 500, so Stripe retries
        return {"received": True}, 200

    @app.route("/account", methods=["GET", "POST"])
    @login_required
    def account():
        if request.method == "POST":
            user = store.get_db().execute("SELECT * FROM users WHERE id = ?", (g.user["id"],)).fetchone()
            current = request.form.get("current_password", "")[:MAX_PASSWORD_CHARS]
            new = request.form.get("new_password", "")
            if not check_password_hash(user["password_hash"], current):
                flash("Current password is incorrect.", "error")
            elif not 8 <= len(new) <= MAX_PASSWORD_CHARS:
                flash(f"New password must be 8–{MAX_PASSWORD_CHARS} characters.", "error")
            else:
                db = store.get_db()
                db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(new), user["id"]))
                db.commit()
                flash("Password updated.")
            return redirect(url_for("account"))
        return render_template("account.html")
