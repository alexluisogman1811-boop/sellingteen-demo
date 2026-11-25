"""Plans, monthly usage limits, and Stripe subscriptions.

- Usage is counted per calendar month (UTC). A new month means a new row, so
  the counter "resets" without any scheduled job.
- Card details are entered on Stripe's hosted Checkout page and never touch
  this server. We only store Stripe IDs and the subscription status.
- Stripe webhooks are the source of truth for subscription status. Every
  webhook's signature is verified with STRIPE_WEBHOOK_SECRET.

Free trial and manual activation (on top of Free/Pro):
- Every new account gets Pro for TRIAL_DAYS (default 30) from its created_at.
- After the trial, an account that isn't paid simply falls back to the Free
  plan limits (5 quotes/month). Nothing is locked: all pages stay open.
- Pro comes from an active Stripe subscription OR from a manual activation
  (users.paid_until, a YYYY-MM-DD date) after a payment via PAYMENT_LINK:

    flask --app quoteflow accounts                      list accounts and their status
    flask --app quoteflow activate someone@shop.com     Pro for 1 more month (--months N)
    flask --app quoteflow deactivate someone@shop.com   switch manual Pro off (e.g. after a refund)

- BILLING_ENABLED=0 turns the trial and manual activation off: the app then
  behaves exactly as before (Free unless a Stripe subscription is active).
"""
import logging
from datetime import date, datetime, timedelta, timezone

import click
import stripe
from flask import current_app, g, has_app_context

from . import analytics
from . import db as store
from . import settings

log = logging.getLogger(__name__)

PLANS = {
    "free": {"name": "Free", "quota": 5, "price": 0},
    "pro": {"name": "Pro", "quota": 100, "price": 15},
}
# past_due keeps Pro during Stripe's automatic payment retries; once Stripe
# gives up the status becomes unpaid/canceled and the account drops to Free.
PRO_STATUSES = {"active", "trialing", "past_due"}
# APP_BASE_URL is required so Stripe return links never depend on the request's Host header.
REQUIRED_ENV = ["STRIPE_SECRET_KEY", "STRIPE_PRICE_ID", "STRIPE_WEBHOOK_SECRET", "APP_BASE_URL"]


class BillingError(Exception):
    """A problem we can show to the user as-is."""


# ---------- plans & usage ----------

def _field(user, name, default=None):
    try:
        return user[name]
    except (KeyError, IndexError):
        return default


def _created_date(user):
    try:
        return datetime.fromisoformat(str(user["created_at"])).date()
    except (TypeError, ValueError, KeyError, IndexError):
        return date.today()


def billing_enabled():
    return has_app_context() and bool(current_app.config.get("BILLING_ENABLED"))


def access_status(user, today=None):
    """Trial / manual-payment state of an account (Stripe status is separate).

    state = off (billing disabled) | paid | trial | ended (falls back to Free), with dates."""
    if not billing_enabled():
        return {"state": "off"}
    today = today or date.today()
    paid_until = _field(user, "paid_until")
    until = None
    if paid_until:
        try:
            until = date.fromisoformat(paid_until)
        except ValueError:
            until = None
        if until and until >= today:
            return {"state": "paid", "until": until, "days_left": (until - today).days}
    trial_end = _created_date(user) + timedelta(days=current_app.config["TRIAL_DAYS"])
    if trial_end >= today and not paid_until:
        return {"state": "trial", "until": trial_end, "days_left": (trial_end - today).days}
    return {"state": "ended", "until": until if paid_until and until else trial_end,
            "was_paid": bool(paid_until)}


def has_stripe_pro(user):
    return _field(user, "subscription_status") in PRO_STATUSES


def has_paid_pro(user):
    """Paid for Pro (Stripe subscription or manual activation) - not just trialling."""
    return has_stripe_pro(user) or access_status(user)["state"] == "paid"


def plan_for(user):
    if has_stripe_pro(user):
        return "pro"
    return "pro" if access_status(user)["state"] in ("trial", "paid") else "free"


def check_access():
    """before_request: work out the trial/subscription state for the trial bar.

    Never redirects: after the trial an unpaid account keeps working on the Free plan,
    and the quota check in reserve_quote() enforces the limits."""
    g.access = None
    user = g.get("user")
    if user is not None and billing_enabled():
        g.access = access_status(user)
        g.access["stripe_pro"] = has_stripe_pro(user)


def current_month(now=None):
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m")


def next_reset(now=None):
    now = now or datetime.now(timezone.utc)
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return datetime(year, month, 1, tzinfo=timezone.utc)


def usage_count(user_id):
    row = store.get_db().execute(
        "SELECT count FROM usage WHERE user_id = ? AND month = ?", (user_id, current_month())
    ).fetchone()
    return row["count"] if row else 0


def usage_summary(user):
    plan = plan_for(user)
    used = usage_count(user["id"])
    quota = PLANS[plan]["quota"]
    reset = next_reset()
    access = access_status(user)
    trial = plan == "pro" and not has_stripe_pro(user) and access["state"] == "trial"
    return {
        "plan": plan,
        "plan_name": PLANS[plan]["name"] + (" (free trial)" if trial else ""),
        "trial": trial,
        "access": access,
        "used": used,
        "quota": quota,
        "remaining": max(0, quota - used),
        "resets_on": f"{reset:%B} {reset.day}",
    }


def reserve_quote(user):
    """Atomically take one quote from this month's allowance. Returns False
    if the allowance is used up. Call release_quote() if generation fails."""
    db = store.get_db()
    month = current_month()
    db.execute("INSERT OR IGNORE INTO usage (user_id, month, count) VALUES (?, ?, 0)", (user["id"], month))
    cur = db.execute(
        "UPDATE usage SET count = count + 1 WHERE user_id = ? AND month = ? AND count < ?",
        (user["id"], month, PLANS[plan_for(user)]["quota"]),
    )
    db.commit()
    return cur.rowcount == 1


def release_quote(user):
    db = store.get_db()
    db.execute(
        "UPDATE usage SET count = count - 1 WHERE user_id = ? AND month = ? AND count > 0",
        (user["id"], current_month()),
    )
    db.commit()


# ---------- Stripe ----------

def missing_config():
    return [k for k in REQUIRED_ENV if not settings.get(k)]


def is_configured():
    return not missing_config()


def _client():
    missing = missing_config()
    if missing:
        log.error("Stripe is not configured; missing environment variables: %s", ", ".join(missing))
        raise BillingError("Upgrades aren't available yet. Please try again later.")
    return stripe.StripeClient(settings.get("STRIPE_SECRET_KEY"))


def _base_url():
    return settings.get("APP_BASE_URL").rstrip("/")


def _stripe_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except stripe.AuthenticationError:
        log.error("Stripe rejected STRIPE_SECRET_KEY")
        raise BillingError("Upgrades aren't available right now. Please try again later.")
    except stripe.StripeError as e:
        log.error("Stripe error: %s (request_id=%s)", type(e).__name__, getattr(e, "request_id", None))
        raise BillingError("We couldn't reach the payment provider. Please try again in a minute.")


def create_checkout_url(user):
    """Stripe-hosted Checkout page for the Pro subscription."""
    client = _client()
    base = _base_url()
    customer_id = user["stripe_customer_id"] or _create_customer(client, user)
    session = _stripe_call(client.v1.checkout.sessions.create, params={
        "mode": "subscription",
        "customer": customer_id,
        "client_reference_id": str(user["id"]),
        "line_items": [{"price": settings.get("STRIPE_PRICE_ID"), "quantity": 1}],
        "subscription_data": {"metadata": {"user_id": str(user["id"])}},
        "success_url": f"{base}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{base}/billing?checkout=canceled",
    })
    return session.url


def _create_customer(client, user):
    customer = _stripe_call(client.v1.customers.create, params={
        "email": user["email"], "metadata": {"user_id": str(user["id"])},
    })
    db = store.get_db()
    db.execute("UPDATE users SET stripe_customer_id = ? WHERE id = ? AND stripe_customer_id = ''",
               (customer.id, user["id"]))
    db.commit()
    return db.execute("SELECT stripe_customer_id FROM users WHERE id = ?", (user["id"],)).fetchone()[0]


def create_portal_url(user):
    """Stripe-hosted page where the customer updates their card or cancels."""
    if not user["stripe_customer_id"]:
        raise BillingError("You don't have a subscription to manage yet.")
    client = _client()
    session = _stripe_call(client.v1.billing_portal.sessions.create, params={
        "customer": user["stripe_customer_id"],
        "return_url": f"{_base_url()}/billing",
    })
    return session.url


def confirm_checkout(user, session_id):
    """Called when Stripe redirects back after Checkout, so the plan updates
    immediately instead of waiting for the webhook. Returns True if Pro is active."""
    client = _client()
    session = _plain(_stripe_call(client.v1.checkout.sessions.retrieve, session_id,
                                  params={"expand": ["subscription"]}))
    if session.get("client_reference_id") != str(user["id"]) or session.get("customer") != user["stripe_customer_id"]:
        log.warning("Checkout session %s does not belong to user %s", session_id, user["id"])
        raise BillingError("We couldn't confirm that payment. If you were charged, your plan will update shortly.")
    sub = session.get("subscription")
    if session.get("status") == "complete" and sub:
        apply_subscription(user["id"], sub, int(datetime.now(timezone.utc).timestamp()))
    row = store.get_db().execute("SELECT subscription_status FROM users WHERE id = ?", (user["id"],)).fetchone()
    return row["subscription_status"] in PRO_STATUSES


def _period_end(sub):
    # Newer Stripe API versions keep the billing period on the subscription item.
    items = (sub.get("items") or {}).get("data") or []
    value = (items[0].get("current_period_end") if items else None) or sub.get("current_period_end")
    return int(value or 0)


def apply_subscription(user_id, sub, event_time):
    """Store a subscription's state. Ignores events older than what we have,
    and non-active updates for a subscription that isn't the user's current one."""
    db = store.get_db()
    user = db.execute("SELECT stripe_subscription_id, subscription_status, subscription_event_at FROM users "
                      "WHERE id = ?", (user_id,)).fetchone()
    if user is None or event_time < user["subscription_event_at"]:
        return
    current = user["stripe_subscription_id"]
    if current and current != sub["id"] and sub["status"] not in PRO_STATUSES:
        return
    db.execute(
        "UPDATE users SET stripe_subscription_id = ?, subscription_status = ?, subscription_period_end = ?, "
        "cancel_at_period_end = ?, subscription_event_at = ? WHERE id = ?",
        (sub["id"], sub["status"], _period_end(sub), int(bool(sub.get("cancel_at_period_end"))),
         event_time, user_id),
    )
    db.commit()
    if user["subscription_status"] not in PRO_STATUSES and sub["status"] in PRO_STATUSES:
        analytics.track("upgrade", user_id)


def construct_event(payload, sig_header):
    """Verify a webhook's signature. Raises ValueError / SignatureVerificationError."""
    secret = settings.get("STRIPE_WEBHOOK_SECRET")
    if not secret:
        raise ValueError("STRIPE_WEBHOOK_SECRET is not set")
    return stripe.StripeClient(settings.get("STRIPE_SECRET_KEY") or "unused").construct_event(
        payload, sig_header, secret)


def _user_by_customer(customer_id):
    if not customer_id:
        return None
    return store.get_db().execute("SELECT * FROM users WHERE stripe_customer_id = ?", (customer_id,)).fetchone()


def _plain(obj):
    # Stripe SDK objects aren't dicts; work with plain (recursively converted) dicts.
    return obj.to_dict() if hasattr(obj, "to_dict") else obj


def handle_event(event):
    """Apply a verified Stripe event. Safe to receive the same event twice."""
    event = _plain(event)
    kind, obj, created = event["type"], event["data"]["object"], int(event["created"])
    db = store.get_db()

    if kind == "checkout.session.completed" and obj.get("mode") == "subscription":
        user_id = int(obj.get("client_reference_id") or 0)
        db.execute("UPDATE users SET stripe_customer_id = ? WHERE id = ? AND stripe_customer_id = ''",
                   (obj["customer"], user_id))
        db.commit()
        user = _user_by_customer(obj["customer"])
        if user is None or user["id"] != user_id:
            log.warning("Checkout %s: customer %s doesn't match user %s", obj.get("id"), obj["customer"], user_id)
        # Status itself arrives with customer.subscription.created/updated.

    elif kind in ("customer.subscription.created", "customer.subscription.updated",
                  "customer.subscription.deleted"):
        user = _user_by_customer(obj.get("customer"))
        if user is None:
            log.warning("%s for unknown customer %s", kind, obj.get("customer"))
            return
        apply_subscription(user["id"], obj, created)

    elif kind in ("invoice.payment_failed", "invoice.paid"):
        user = _user_by_customer(obj.get("customer"))
        if user is None:
            return
        failed = kind == "invoice.payment_failed"
        db.execute("UPDATE users SET payment_failed = ?, payment_event_at = ? WHERE id = ? AND payment_event_at <= ?",
                   (int(failed), created, user["id"], created))
        db.commit()
        log.log(logging.WARNING if failed else logging.INFO, "%s for user %s", kind, user["id"])


# ---------- manual activation (CLI) ----------

def _iso(value):
    return value.isoformat() if value else None


def list_accounts():
    """All accounts with their billing state. Call inside an app context.

    Each item: email, business, state ("trial" | "paid" | "free"), until ("YYYY-MM-DD" or None),
    created ("YYYY-MM-DD" or None). QuoteFlow has a Free plan, so an account whose trial is over
    and that isn't paid is "free" ("ended" is never used). QuoteFlow has no demo accounts.
    """
    rows = store.get_db().execute(
        "SELECT u.id, u.email, u.created_at, u.paid_until, u.subscription_status, u.subscription_period_end, "
        "COALESCE(p.business_name, '') AS business_name "
        "FROM users u LEFT JOIN business_profiles p ON p.user_id = u.id ORDER BY u.id").fetchall()
    result = []
    for r in rows:
        s = access_status(r)
        if has_stripe_pro(r):
            state = "paid"
            end = int(r["subscription_period_end"] or 0)
            until = (datetime.fromtimestamp(end, timezone.utc).date() if end
                     else (s.get("until") if s["state"] == "paid" else None))
        elif s["state"] in ("trial", "paid"):
            state, until = s["state"], s["until"]
        else:
            state, until = "free", None
        try:
            created = datetime.fromisoformat(str(r["created_at"])).date().isoformat()
        except (TypeError, ValueError):
            created = None
        result.append({"email": (r["email"] or "").strip().lower(), "business": r["business_name"] or "",
                       "state": state, "until": _iso(until), "created": created})
    return result


def set_paid_until(email, months):
    """Switch Pro on for `months` more months (31 days each), or off when months <= 0.

    Extends from the later of today and the current paid-until date. Returns the new date,
    or None when switched off. Raises LookupError if no account has this email.
    """
    db = store.get_db()
    row = db.execute("SELECT id, paid_until FROM users WHERE lower(email) = ?",
                     ((email or "").strip().lower(),)).fetchone()
    if row is None:
        raise LookupError(f"No account with the email {email}.")
    if months <= 0:
        db.execute("UPDATE users SET paid_until = ? WHERE id = ?",
                   ((date.today() - timedelta(days=1)).isoformat(), row["id"]))
        db.commit()
        return None
    start = date.today()
    if row["paid_until"]:
        try:
            start = max(start, date.fromisoformat(row["paid_until"]))
        except ValueError:
            pass
    until = start + timedelta(days=31 * months)
    db.execute("UPDATE users SET paid_until = ? WHERE id = ?", (until.isoformat(), row["id"]))
    db.commit()
    return until


# Older name, kept so existing scripts keep working.
_set_paid_until = set_paid_until


def _set_paid_until_cli(email, months):
    try:
        return set_paid_until(email, months)
    except LookupError as exc:
        raise click.ClickException(str(exc)) from exc


def register_cli(app):
    @app.cli.command("activate")
    @click.argument("email")
    @click.option("--months", default=1, show_default=True, help="How many paid months of Pro to add.")
    def activate(email, months):
        """Switch Pro on for an account after a payment."""
        if months <= 0:
            raise click.BadParameter("must be at least 1", param_hint="--months")
        until = _set_paid_until_cli(email, months)
        click.echo(f"{email}: Pro switched on until {until.isoformat()}")

    @app.cli.command("deactivate")
    @click.argument("email")
    def deactivate(email):
        """Switch manual Pro off (for example after a refund). The account falls back to Free."""
        _set_paid_until_cli(email, 0)
        click.echo(f"{email}: switched off (Free plan limits apply)")

    @app.cli.command("accounts")
    def accounts():
        """List accounts and their trial / subscription status."""
        rows = list_accounts()
        if not rows:
            click.echo("No accounts yet.")
        users = {r["email"].strip().lower(): r for r in store.get_db().execute(
            "SELECT email, created_at, paid_until, subscription_status FROM users").fetchall()}
        for a in rows:
            u = users.get(a["email"])
            plan = plan_for(u) if u is not None else ""
            extra = f"until {a['until']}" if a["until"] else ""
            stripe_note = f"stripe:{u['subscription_status']}" if u is not None and u["subscription_status"] else ""
            click.echo(f"{a['email']:40} {a['state']:6} {extra:17} {plan:4} {stripe_note}".rstrip())
