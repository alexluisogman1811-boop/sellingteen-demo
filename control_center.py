"""Owner Control Center at /admin (one protected page for the owner).

OFF until site.env has BOTH:
    ADMIN_PASSWORD_HASH=...      (made by:  python3 manage.py set-admin-password)
    ADMIN_SESSION_SECRET=...     (made by the same command)

What it shows, per app and in total: sign-ups, trials, trials ending in the next 7 days, paid, ended,
free-plan accounts, and estimated monthly revenue (paid accounts x price in apps.json).
What it can change: ONLY "activate +1 month", "activate +12 months" and "deactivate" (each app's set_paid_until).
Everything else is read-only. Every login and every action is written to data/_site/admin_log.jsonl (append-only).

Safety: password hash only (never the password), login throttling, CSRF token on every form,
its own cookie limited to /admin (HttpOnly, SameSite=Strict, Secure), no caching, no framing.
"""
import csv
import hmac
import io
import json
import secrets
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, Response, abort, redirect, render_template_string, request, session, url_for
from werkzeug.security import check_password_hash

STATES = ("trial", "paid", "free", "ended")


# ---------------------------------------------------------------- append-only log + small record store
class AppendLog:
    """A log file that is only ever appended to (one JSON object per line). Nothing here edits or deletes lines."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def write(self, event, **fields):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "event": event, **fields}
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return entry

    def tail(self, n=50):
        if not self.path.is_file():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-n:]
        out = []
        for line in reversed(lines):
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out


class Store:
    """Tiny SQLite file for webhook bookkeeping: which events were already handled (so repeats do nothing),
    and which subscription / payment / order belongs to which app + email (so renewals and refunds can be matched)."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS processed_events (key TEXT PRIMARY KEY, provider TEXT, event TEXT, at TEXT);
                CREATE TABLE IF NOT EXISTS links (provider TEXT, kind TEXT, ref TEXT, app TEXT, email TEXT, months INTEGER,
                                                  PRIMARY KEY (provider, kind, ref));
                CREATE TABLE IF NOT EXISTS reminders (app TEXT, email TEXT, trial_end TEXT, sent_at TEXT,
                                                      PRIMARY KEY (app, email, trial_end));
            """)

    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def seen(self, key):
        with self._conn() as c:
            return c.execute("SELECT 1 FROM processed_events WHERE key = ?", (key,)).fetchone() is not None

    def mark(self, key, provider, event):
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO processed_events VALUES (?, ?, ?, ?)",
                      (key, provider, event, datetime.now(timezone.utc).isoformat()))

    def link(self, provider, kind, ref, app, email, months=1):
        if not ref:
            return
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO links VALUES (?, ?, ?, ?, ?, ?)",
                      (provider, kind, str(ref), app, (email or "").strip().lower(), int(months or 1)))

    def find(self, provider, kind, ref):
        if not ref:
            return None
        with self._conn() as c:
            row = c.execute("SELECT app, email, months FROM links WHERE provider = ? AND kind = ? AND ref = ?",
                            (provider, kind, str(ref))).fetchone()
        return dict(row) if row else None

    def reminder_sent(self, app, email, trial_end):
        with self._conn() as c:
            return c.execute("SELECT 1 FROM reminders WHERE app = ? AND email = ? AND trial_end = ?",
                             (app, email, trial_end)).fetchone() is not None

    def mark_reminder(self, app, email, trial_end):
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO reminders VALUES (?, ?, ?, ?)",
                      (app, email, trial_end, datetime.now(timezone.utc).isoformat()))


# ---------------------------------------------------------------- numbers for the dashboard
def _as_date(value):
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def summarize(registry, today=None):
    """Returns (rows per app, totals, all accounts, errors)."""
    today = today or date.today()
    soon = today + timedelta(days=7)
    rows, accounts, errors = [], [], []
    totals = {"signups": 0, "trial": 0, "ending_soon": 0, "paid": 0, "free": 0, "ended": 0, "revenue": 0.0}
    for slug in registry.slugs:
        try:
            accts = registry.list_accounts(slug)
        except Exception as exc:  # one broken app must not hide the others
            errors.append({"app": slug, "error": f"{type(exc).__name__}: {exc}"[:300]})
            continue
        r = {"app": slug, "name": registry.name(slug), "price": registry.price_usd(slug), "signups": len(accts),
             "trial": 0, "ending_soon": 0, "paid": 0, "free": 0, "ended": 0}
        for a in accts:
            state = a.get("state") if a.get("state") in STATES else "ended"
            r[state] += 1
            until = _as_date(a.get("until"))
            if state == "trial" and until and today <= until <= soon:
                r["ending_soon"] += 1
            accounts.append({"app": slug, "email": a.get("email", ""), "business": a.get("business", ""),
                             "state": state, "until": str(a.get("until") or ""), "created": str(a.get("created") or "")})
        r["revenue"] = round(r["paid"] * r["price"], 2)
        for k in ("signups", "trial", "ending_soon", "paid", "free", "ended", "revenue"):
            totals[k] += r[k]
        rows.append(r)
    totals["revenue"] = round(totals["revenue"], 2)
    return rows, totals, accounts, errors


# ---------------------------------------------------------------- the /admin web page
PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>sellingteen Control Center</title>
<style>
body{font-family:system-ui,sans-serif;margin:0;background:#f6f7f9;color:#1d2330}main{max-width:1100px;margin:0 auto;padding:16px}
h1{font-size:1.4rem}table{border-collapse:collapse;width:100%;background:#fff;margin:12px 0;font-size:.92rem}
th,td{border:1px solid #dde1e7;padding:6px 8px;text-align:left}th{background:#eef1f5}td.n{text-align:right}
.box{background:#fff;border:1px solid #dde1e7;border-radius:8px;padding:12px;margin:12px 0}
.msg{background:#e9f7ef;border:1px solid #9bd3ae;padding:8px;border-radius:6px}.err{background:#fdecea;border-color:#f3a6a0}
input,select,button{font:inherit;padding:6px}button{cursor:pointer}.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.wrap{overflow-x:auto}small{color:#5b6475}
</style></head><body><main>
{% if not logged_in %}
<h1>sellingteen Control Center</h1>
{% if error %}<p class="msg err">{{ error }}</p>{% endif %}
<form method="post" action="{{ url_for('login') }}" class="box">
<input type="hidden" name="csrf" value="{{ csrf }}">
<label>Owner password <input type="password" name="password" autocomplete="current-password" required autofocus></label>
<button type="submit">Log in</button></form>
{% else %}
<div class="row" style="justify-content:space-between"><h1>sellingteen Control Center</h1>
<form method="post" action="{{ url_for('logout') }}"><input type="hidden" name="csrf" value="{{ csrf }}"><button>Log out</button></form></div>
{% if message %}<p class="msg {{ 'err' if message_bad else '' }}">{{ message }}</p>{% endif %}
<p><small>Read-only, except the activate / deactivate buttons. "Monthly revenue" is an estimate: paid accounts x the app's price. It is not money received.</small></p>
<div class="wrap"><table><tr><th>App</th><th>Sign-ups</th><th>Trials</th><th>Trials ending in 7 days</th><th>Paid</th><th>Free plan</th><th>Ended</th><th>Price</th><th>Est. monthly revenue</th></tr>
{% for r in rows %}<tr><td>{{ r.name }} <small>/{{ r.app }}</small></td><td class="n">{{ r.signups }}</td><td class="n">{{ r.trial }}</td><td class="n">{{ r.ending_soon }}</td>
<td class="n">{{ r.paid }}</td><td class="n">{{ r.free }}</td><td class="n">{{ r.ended }}</td><td class="n">${{ '%.2f' % r.price }}</td><td class="n">${{ '%.2f' % r.revenue }}</td></tr>{% endfor %}
<tr><th>Total</th><th class="n">{{ t.signups }}</th><th class="n">{{ t.trial }}</th><th class="n">{{ t.ending_soon }}</th><th class="n">{{ t.paid }}</th><th class="n">{{ t.free }}</th><th class="n">{{ t.ended }}</th><th></th><th class="n">${{ '%.2f' % t.revenue }}</th></tr>
</table></div>
{% for e in errors %}<p class="msg err">Could not read {{ e.app }}: {{ e.error }}</p>{% endfor %}
<div class="box"><b>Switch an account on or off</b>
<form method="post" action="{{ url_for('change') }}" class="row">
<input type="hidden" name="csrf" value="{{ csrf }}">
<select name="app" required>{% for r in rows %}<option value="{{ r.app }}">{{ r.name }}</option>{% endfor %}</select>
<input type="email" name="email" placeholder="customer@email.com" required>
<button name="action" value="1">Activate +1 month</button><button name="action" value="12">Activate +12 months</button>
<button name="action" value="off" onclick="return confirm('Switch this account off?')">Deactivate</button></form></div>
<p><a href="{{ url_for('export_csv') }}">Download all accounts (CSV)</a></p>
<div class="wrap"><table><tr><th>App</th><th>Email</th><th>Business</th><th>State</th><th>Until</th><th>Created</th></tr>
{% for a in accounts %}<tr><td>{{ a.app }}</td><td>{{ a.email }}</td><td>{{ a.business }}</td><td>{{ a.state }}</td><td>{{ a.until }}</td><td>{{ a.created }}</td></tr>
{% else %}<tr><td colspan="6">No accounts yet.</td></tr>{% endfor %}</table></div>
<h2 style="font-size:1.1rem">Recent payment notices (webhooks)</h2>
<div class="wrap"><table><tr><th>Time (UTC)</th><th>Provider</th><th>Event</th><th>Result</th><th>App</th><th>Email</th></tr>
{% for w in hooks %}<tr><td>{{ w.time }}</td><td>{{ w.provider }}</td><td>{{ w.type }}</td><td>{{ w.result }}</td><td>{{ w.app }}</td><td>{{ w.email }}</td></tr>
{% else %}<tr><td colspan="6">None yet.</td></tr>{% endfor %}</table></div>
<h2 style="font-size:1.1rem">Recent Control Center actions</h2>
<div class="wrap"><table><tr><th>Time (UTC)</th><th>Event</th><th>Details</th></tr>
{% for l in log %}<tr><td>{{ l.time }}</td><td>{{ l.event }}</td><td>{{ l.app or '' }} {{ l.email or '' }} {{ l.result or '' }}</td></tr>{% endfor %}</table></div>
{% endif %}
<p><small>sellingteen</small></p></main></body></html>"""


class LoginThrottle:
    """At most `limit` failed logins per IP per window, and a global cap, kept in memory."""

    def __init__(self, limit=5, window=900, global_limit=30):
        self.limit, self.window, self.global_limit = limit, window, global_limit
        self.fails = {}
        self.lock = threading.Lock()

    def _recent(self, key, now):
        items = [t for t in self.fails.get(key, []) if now - t < self.window]
        self.fails[key] = items
        return items

    def blocked(self, ip, now=None):
        now = now or time.time()
        with self.lock:
            return len(self._recent(ip, now)) >= self.limit or len(self._recent("*", now)) >= self.global_limit

    def fail(self, ip, now=None):
        now = now or time.time()
        with self.lock:
            self._recent(ip, now).append(now)
            self._recent("*", now).append(now)

    def clear(self, ip):
        with self.lock:
            self.fails.pop(ip, None)


def create_admin_app(registry, settings, data_dir, testing=False):
    """settings: dict from site.env. Returns a Flask app meant to be mounted at /admin."""
    password_hash = (settings.get("ADMIN_PASSWORD_HASH") or "").strip()
    secret = (settings.get("ADMIN_SESSION_SECRET") or "").strip()
    enabled = bool(password_hash and len(secret) >= 32)
    data_dir = Path(data_dir)
    log = AppendLog(data_dir / "admin_log.jsonl")
    hook_log = AppendLog(data_dir / "webhook_log.jsonl")
    throttle = LoginThrottle()

    app = Flask("sellingteen_admin")
    app.config.update(
        SECRET_KEY=secret or secrets.token_hex(32),
        SESSION_COOKIE_NAME="st_admin",
        SESSION_COOKIE_PATH="/admin",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=(settings.get("ADMIN_COOKIE_SECURE", "1") != "0"),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        MAX_CONTENT_LENGTH=64 * 1024,
        TESTING=testing,
    )
    app.extensions["sellingteen_admin"] = {"log": log, "throttle": throttle, "enabled": enabled}

    def client_ip():
        # PythonAnywhere puts the visitor's address first in X-Real-IP / X-Forwarded-For.
        return (request.headers.get("X-Real-IP") or request.headers.get("X-Forwarded-For", "").split(",")[0]
                or request.remote_addr or "?").strip()

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    @app.before_request
    def guard():
        if not enabled:
            abort(404)  # Control Center is off until a password hash and secret are set
        if request.method == "POST":
            sent = request.form.get("csrf", "")
            if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
                abort(400)

    @app.after_request
    def headers(resp):
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                                                   "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        return resp

    def logged_in():
        return session.get("owner") is True

    @app.route("/", methods=["GET"])
    def index():
        if not logged_in():
            return render_template_string(PAGE, logged_in=False, csrf=csrf_token(), error=request.args.get("e") and
                                          "Wrong password, or too many tries. Wait 15 minutes and try again.")
        rows, totals, accounts, errors = summarize(registry)
        hooks = [{"time": h.get("time"), "provider": h.get("provider"), "type": h.get("type"), "result": h.get("result"),
                  "app": h.get("app"), "email": h.get("email")} for h in hook_log.tail(30)]
        return render_template_string(PAGE, logged_in=True, csrf=csrf_token(), rows=rows, t=totals, accounts=accounts,
                                      errors=errors, hooks=hooks, log=log.tail(30), message=request.args.get("m"),
                                      message_bad=request.args.get("bad") == "1")

    @app.route("/login", methods=["POST"])
    def login():
        ip = client_ip()
        if throttle.blocked(ip):
            log.write("login_blocked", ip=ip)
            return redirect(url_for("index", e=1))
        if check_password_hash(password_hash, request.form.get("password", "")):
            throttle.clear(ip)
            session.clear()
            session.permanent = True
            session["owner"] = True
            session["csrf"] = secrets.token_urlsafe(32)
            log.write("login_ok", ip=ip)
            return redirect(url_for("index"))
        throttle.fail(ip)
        log.write("login_failed", ip=ip)
        return redirect(url_for("index", e=1))

    @app.route("/logout", methods=["POST"])
    def logout():
        log.write("logout", ip=client_ip())
        session.clear()
        return redirect(url_for("index"))

    @app.route("/change", methods=["POST"])
    def change():
        if not logged_in():
            abort(403)
        slug = request.form.get("app", "")
        email = request.form.get("email", "").strip().lower()
        action = request.form.get("action", "")
        months = {"1": 1, "12": 12, "off": 0}.get(action)
        if slug not in registry.slugs or months is None or "@" not in email or len(email) > 254:
            return redirect(url_for("index", m="Pick an app, type the customer's email, and use one of the buttons.", bad=1))
        try:
            until = registry.set_paid_until(slug, email, months)
        except LookupError:
            log.write("change_failed", app=slug, email=email, months=months, result="no such account", ip=client_ip())
            return redirect(url_for("index", m=f"No account with {email} in {registry.name(slug)}.", bad=1))
        result = f"on until {until}" if until else "switched off"
        log.write("activate" if months else "deactivate", app=slug, email=email, months=months, result=result, ip=client_ip())
        return redirect(url_for("index", m=f"{registry.name(slug)}: {email} {result}."))

    @app.route("/accounts.csv")
    def export_csv():
        if not logged_in():
            abort(403)
        _, _, accounts, _ = summarize(registry)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["app", "email", "business", "state", "until", "created"])
        for a in accounts:
            # Neutralise spreadsheet formulas in customer-typed text
            w.writerow([("'" + str(v)) if str(v)[:1] in ("=", "+", "-", "@") else v
                        for v in (a["app"], a["email"], a["business"], a["state"], a["until"], a["created"])])
        log.write("export_csv", count=len(accounts), ip=client_ip())
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=sellingteen-accounts-{date.today()}.csv"})

    return app
