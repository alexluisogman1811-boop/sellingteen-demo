"""Automatic activation after payment: /webhooks/stripe, /webhooks/lemonsqueezy and /webhooks/dodo

Each provider is OFF (404) until its secret is in site.env:
    STRIPE_WEBHOOK_SECRET=whsec_...   (Stripe > Developers > Webhooks > your endpoint > "Signing secret")
    LEMONSQUEEZY_WEBHOOK_SECRET=...   (the "Signing secret" you type when creating the webhook in Lemon Squeezy)
    DODO_WEBHOOK_SECRET=whsec_...     (the secret Dodo Payments shows for your webhook endpoint)

Stripe (payment links): webhooks.json maps each payment link to its app:
    {"stripe": {"payment_links": {"plink_123": {"app": "reviewboost", "months": 1}}}}
  Events to send: checkout.session.completed, invoice.paid, customer.subscription.deleted.
  Signature: header Stripe-Signature "t=<time>,v1=<hex>", HMAC-SHA256 of "<t>.<raw body>" keyed with the
  whole signing secret text. Timestamps more than 5 minutes away are refused.

Which product belongs to which app is in webhooks.json (the owner fills it in), for example:
    {"lemonsqueezy": {"variants": {"123456": {"app": "reviewboost", "months": 1}}, "products": {}},
     "dodo": {"products": {"pdt_abc123": {"app": "reviewboost", "months": 1}}},
     "accept_test_mode": false}
Use "app": "all" for the "All apps" bundle: it switches on every app where the customer's email has an account.

Rules (same for both providers):
- The signature is checked first (official schemes). Unsigned or wrong -> 401, nothing happens.
- A payment / renewal switches the matching account on (+1 month, or +12 for a yearly product), found by the customer's email.
- A refund switches it off. A cancellation does NOTHING right away: the customer keeps what they paid for until it runs out.
  When the subscription finally expires, the account is switched off.
- The same event sent twice does nothing the second time. Everything is written to data/_site/webhook_log.jsonl.

Signature schemes (from the providers' docs):
- Lemon Squeezy: header X-Signature = hex HMAC-SHA256 of the raw request body, keyed with the signing secret.
- Dodo Payments (Standard Webhooks): headers webhook-id, webhook-timestamp, webhook-signature ("v1,<base64>" items,
  space separated). Signed text = "<id>.<timestamp>.<raw body>", HMAC-SHA256, key = base64-decoded secret
  (after removing "whsec_"). Timestamps more than 5 minutes away are refused.
"""
import base64
import binascii
import hashlib
import hmac
import json
import time
from pathlib import Path

from flask import Flask, abort, jsonify, request

from control_center import AppendLog, Store

TOLERANCE_SECONDS = 300


# ---------------------------------------------------------------- signature checks
def verify_lemonsqueezy(secret, raw_body, signature_header):
    if not secret or not signature_header:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.strip().lower())


def dodo_key(secret):
    s = secret.strip()
    if s.startswith("whsec_"):
        s = s[len("whsec_"):]
    try:
        return base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError):
        return None


def verify_dodo(secret, raw_body, msg_id, timestamp, signature_header, now=None):
    if not secret or not msg_id or not timestamp or not signature_header:
        return False
    key = dodo_key(secret)
    if not key:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    now = int(now if now is not None else time.time())
    if abs(now - ts) > TOLERANCE_SECONDS:
        return False
    signed = msg_id.encode() + b"." + str(ts).encode() + b"." + raw_body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    for item in signature_header.split():
        version, _, sig = item.partition(",")
        if version == "v1" and hmac.compare_digest(expected, sig):
            return True
    return False


def verify_stripe(secret, raw_body, signature_header, now=None):
    if not secret or not signature_header:
        return False
    parts = [p.split("=", 1) for p in signature_header.split(",") if "=" in p]
    stamps = [v for k, v in parts if k.strip() == "t"]
    sigs = [v.strip() for k, v in parts if k.strip() == "v1"]
    if not stamps or not sigs:
        return False
    try:
        ts = int(stamps[0])
    except ValueError:
        return False
    now = int(now if now is not None else time.time())
    if abs(now - ts) > TOLERANCE_SECONDS:
        return False
    expected = hmac.new(secret.strip().encode("utf-8"), str(ts).encode() + b"." + raw_body, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in sigs)


def _stripe_invoice_subscription(inv):
    """Older Stripe API versions put it at invoice.subscription, newer ones under invoice.parent."""
    sub = inv.get("subscription")
    if isinstance(sub, dict):
        sub = sub.get("id")
    if not sub:
        details = ((inv.get("parent") or {}).get("subscription_details") or {})
        sub = details.get("subscription")
    return sub


# ---------------------------------------------------------------- product mapping
def load_mapping(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _target(entry):
    """Mapping values may be "reviewboost" or {"app": "reviewboost", "months": 12}."""
    if isinstance(entry, str):
        return entry, 1
    if isinstance(entry, dict) and entry.get("app"):
        try:
            months = int(entry.get("months", 1))
        except (TypeError, ValueError):
            months = 1
        return entry["app"], max(1, min(months, 24))
    return None, None


def _lookup(mapping, provider, ids):
    section = mapping.get(provider, {}) if isinstance(mapping.get(provider), dict) else {}
    for kind, value in ids:
        if value in (None, ""):
            continue
        table = section.get(kind, {})
        if isinstance(table, dict) and str(value) in table:
            app, months = _target(table[str(value)])
            if app:
                return app, months
    return None, None


# ---------------------------------------------------------------- the web endpoint
def create_webhooks_app(registry, settings, data_dir, mapping_path, now=None):
    data_dir = Path(data_dir)
    store = Store(data_dir / "control.sqlite3")
    log = AppendLog(data_dir / "webhook_log.jsonl")
    secrets_ = {"lemonsqueezy": (settings.get("LEMONSQUEEZY_WEBHOOK_SECRET") or "").strip(),
                "dodo": (settings.get("DODO_WEBHOOK_SECRET") or "").strip(),
                "stripe": (settings.get("STRIPE_WEBHOOK_SECRET") or "").strip()}
    clock = now or time.time

    app = Flask("sellingteen_webhooks")
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024

    def act(provider, kind, app_slug, email, months, extra):
        """Switch on (months > 0) or off (months == 0). Returns a short result text."""
        if app_slug == "all" and email:
            # The "All apps" bundle: switch on/off every app where this email has an account.
            done = []
            for slug in registry.slugs:
                try:
                    registry.set_paid_until(slug, email, months)
                    done.append(slug)
                except LookupError:
                    continue
            if not done:
                return "no account with this email in any app: switch on by hand in /admin"
            return ("on (+%d months) in: " % months if months else "switched off in: ") + ", ".join(done)
        if not app_slug or app_slug not in registry.slugs:
            return "unknown product (add it to webhooks.json)"
        if not email:
            return "no customer email in event"
        try:
            until = registry.set_paid_until(app_slug, email, months)
        except LookupError:
            return "no account with this email: switch on by hand in /admin"
        return f"on until {until}" if until else "switched off"

    def handle_lemonsqueezy(payload, mapping):
        meta = payload.get("meta") or {}
        event = meta.get("event_name", "")
        data = payload.get("data") or {}
        attrs = data.get("attributes") or {}
        ref = str(data.get("id") or "")
        email = (attrs.get("user_email") or "").strip().lower()
        if meta.get("test_mode") and not mapping.get("accept_test_mode"):
            return event, "ignored (test mode)", None, email
        if event == "order_created":
            item = attrs.get("first_order_item") or {}
            app_slug, months = _lookup(mapping, "lemonsqueezy", [("variants", item.get("variant_id")), ("products", item.get("product_id"))])
            if attrs.get("status") != "paid":
                return event, f"ignored (order status {attrs.get('status')})", app_slug, email
            store.link("lemonsqueezy", "order", ref, app_slug, email, months)
            return event, act("lemonsqueezy", "order", app_slug, email, months, attrs), app_slug, email
        if event == "subscription_created":
            app_slug, months = _lookup(mapping, "lemonsqueezy", [("variants", attrs.get("variant_id")), ("products", attrs.get("product_id"))])
            store.link("lemonsqueezy", "subscription", ref, app_slug, email, months)
            return event, "remembered (first payment arrives as order_created)", app_slug, email
        if event == "subscription_payment_success":
            if attrs.get("billing_reason") == "initial":
                return event, "ignored (first payment already handled by order_created)", None, email
            link = store.find("lemonsqueezy", "subscription", attrs.get("subscription_id"))
            if not link:
                return event, "unknown subscription: switch on by hand in /admin", None, email
            return event, act("lemonsqueezy", "renewal", link["app"], link["email"] or email, link["months"], attrs), link["app"], link["email"]
        if event == "order_refunded":
            link = store.find("lemonsqueezy", "order", ref)
            if not link:
                item = attrs.get("first_order_item") or {}
                app_slug, _ = _lookup(mapping, "lemonsqueezy", [("variants", item.get("variant_id")), ("products", item.get("product_id"))])
                link = {"app": app_slug, "email": email}
            return event, act("lemonsqueezy", "refund", link["app"], link["email"] or email, 0, attrs), link["app"], link["email"]
        if event in ("subscription_payment_refunded", "subscription_expired"):
            sub_ref = attrs.get("subscription_id") if event == "subscription_payment_refunded" else ref
            link = store.find("lemonsqueezy", "subscription", sub_ref)
            if not link:
                app_slug, _ = _lookup(mapping, "lemonsqueezy", [("variants", attrs.get("variant_id")), ("products", attrs.get("product_id"))])
                link = {"app": app_slug, "email": email}
            return event, act("lemonsqueezy", event, link["app"], link["email"] or email, 0, attrs), link["app"], link["email"]
        if event == "subscription_cancelled":
            return event, "noted: stays on until the paid time runs out", None, email
        return event, "ignored (not needed)", None, email

    def handle_dodo(payload, mapping):
        event = payload.get("type", "")
        data = payload.get("data") or {}
        customer = data.get("customer") or {}
        email = (customer.get("email") or "").strip().lower()
        if event in ("subscription.active", "subscription.renewed"):
            app_slug, months = _lookup(mapping, "dodo", [("products", data.get("product_id"))])
            store.link("dodo", "subscription", data.get("subscription_id"), app_slug, email, months)
            return event, act("dodo", event, app_slug, email, months, data), app_slug, email
        if event == "payment.succeeded":
            sub_id = data.get("subscription_id")
            cart = data.get("product_cart") or []
            product = cart[0].get("product_id") if cart and isinstance(cart[0], dict) else None
            app_slug, months = _lookup(mapping, "dodo", [("products", product)])
            if not app_slug and sub_id:
                link = store.find("dodo", "subscription", sub_id)
                app_slug = link["app"] if link else None
            store.link("dodo", "payment", data.get("payment_id"), app_slug, email, months or 1)
            if sub_id:   # subscription payments are handled by subscription.active / subscription.renewed
                store.link("dodo", "payment_sub", data.get("payment_id"), str(sub_id), email)
                return event, "remembered (subscription events switch the account on)", app_slug, email
            return event, act("dodo", event, app_slug, email, months or 1, data), app_slug, email
        if event == "refund.succeeded":
            link = store.find("dodo", "payment", data.get("payment_id"))
            app_slug, mail = (link["app"], link["email"]) if link else (None, email)
            if not app_slug:
                sub = store.find("dodo", "payment_sub", data.get("payment_id"))
                sublink = store.find("dodo", "subscription", sub["app"]) if sub else None
                if sublink:
                    app_slug, mail = sublink["app"], sublink["email"]
            if not app_slug:
                return event, "unknown payment: check it and switch off by hand in /admin", None, mail
            return event, act("dodo", event, app_slug, mail or email, 0, data), app_slug, mail
        if event == "subscription.expired":
            link = store.find("dodo", "subscription", data.get("subscription_id"))
            app_slug = link["app"] if link else _lookup(mapping, "dodo", [("products", data.get("product_id"))])[0]
            mail = (link["email"] if link else "") or email
            return event, act("dodo", event, app_slug, mail, 0, data), app_slug, mail
        if event == "subscription.cancelled":
            return event, "noted: stays on until the paid time runs out", None, email
        return event, "ignored (not needed)", None, email

    def handle_stripe(payload, mapping):
        event = payload.get("type", "")
        obj = (payload.get("data") or {}).get("object") or {}
        if payload.get("livemode") is False and not mapping.get("accept_test_mode"):
            return event, "ignored (test mode)", None, ""
        if event == "checkout.session.completed":
            email = ((obj.get("customer_details") or {}).get("email") or obj.get("customer_email") or "").strip().lower()
            app_slug, months = _lookup(mapping, "stripe", [("payment_links", obj.get("payment_link"))])
            if obj.get("payment_status") not in ("paid", "no_payment_required"):
                return event, f"ignored (payment status {obj.get('payment_status')})", app_slug, email
            store.link("stripe", "subscription", obj.get("subscription"), app_slug, email, months or 1)
            store.link("stripe", "session", obj.get("id"), app_slug, email, months or 1)
            return event, act("stripe", event, app_slug, email, months or 1, obj), app_slug, email
        if event == "invoice.paid":
            email = (obj.get("customer_email") or "").strip().lower()
            if obj.get("billing_reason") == "subscription_create":
                return event, "ignored (first payment already handled by checkout.session.completed)", None, email
            link = store.find("stripe", "subscription", _stripe_invoice_subscription(obj))
            if not link:
                return event, "unknown subscription: switch on by hand in /admin", None, email
            return event, act("stripe", "renewal", link["app"], link["email"] or email, link["months"], obj), link["app"], link["email"]
        if event == "customer.subscription.deleted":
            link = store.find("stripe", "subscription", obj.get("id"))
            if not link:
                return event, "unknown subscription: check it and switch off by hand in /admin", None, ""
            return event, act("stripe", event, link["app"], link["email"], 0, obj), link["app"], link["email"]
        if event == "customer.subscription.updated" and obj.get("cancel_at_period_end"):
            return event, "noted: stays on until the paid time runs out", None, ""
        return event, "ignored (not needed)", None, ""

    @app.route("/<provider>", methods=["POST"])
    def receive(provider):
        if provider not in secrets_ or not secrets_[provider]:
            abort(404)  # this provider is switched off
        raw = request.get_data(cache=False)
        if provider == "lemonsqueezy":
            ok = verify_lemonsqueezy(secrets_[provider], raw, request.headers.get("X-Signature", ""))
            key = "ls:" + hashlib.sha256(raw).hexdigest()
        elif provider == "stripe":
            ok = verify_stripe(secrets_[provider], raw, request.headers.get("Stripe-Signature", ""), now=clock())
            key = None  # set from the event id below
        else:
            ok = verify_dodo(secrets_[provider], raw, request.headers.get("webhook-id", ""),
                             request.headers.get("webhook-timestamp", ""), request.headers.get("webhook-signature", ""), now=clock())
            key = "dodo:" + request.headers.get("webhook-id", "")
        if not ok:
            log.write("rejected", provider=provider, type="", result="bad or missing signature", ip=request.remote_addr)
            return jsonify({"error": "invalid signature"}), 401
        try:
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
        except ValueError:
            log.write("rejected", provider=provider, type="", result="not JSON")
            return jsonify({"error": "bad payload"}), 400
        if provider == "stripe":
            key = "stripe:" + str(payload.get("id") or hashlib.sha256(raw).hexdigest())
        if store.seen(key):
            log.write("duplicate", provider=provider, type=str(payload.get("type") or (payload.get("meta") or {}).get("event_name", "")),
                      result="already handled, nothing done")
            return jsonify({"status": "duplicate"}), 200
        mapping = load_mapping(mapping_path)
        handler = {"lemonsqueezy": handle_lemonsqueezy, "stripe": handle_stripe}.get(provider, handle_dodo)
        event, result, app_slug, email = handler(payload, mapping)
        store.mark(key, provider, event)
        log.write("webhook", provider=provider, type=event, result=result, app=app_slug or "", email=email or "")
        return jsonify({"status": "ok", "result": result}), 200

    return app
