"""Pricing plans shown on the marketing site.

Edit this list to change plans; the landing and pricing pages render from it. The limits are
enforced in billing.py (FREE_ACTIVE_JOBS, AI summaries Pro-only). The Pro price comes from the
PRICE_TEXT setting, so the price is only ever changed in one place.
"""


def split_price(price_text: str) -> tuple[str, str]:
    """'$12 per month' -> ('$12', 'per month'): big number on the card, small text after it."""
    price, _, period = price_text.strip().partition(" ")
    return price, period.strip()


def get_plans(price_text: str) -> list[dict]:
    pro_price, pro_period = split_price(price_text)
    return [
        {
            "name": "Free",
            "price": "$0",
            "period": "forever",
            "features": ["3 active jobs", "Basic job reports"],
            "cta": "Start free",
            "highlight": False,
        },
        {
            "name": "Pro",
            "price": pro_price,
            "period": pro_period,
            "features": ["Unlimited jobs", "Unlimited photos", "AI job summaries",
                         "Professional customer pages"],
            "cta": "Start 30-day free trial",
            "highlight": True,
        },
    ]
