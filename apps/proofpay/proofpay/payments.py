"""Payment abstraction.

The MVP ships only `ManualProvider`: the customer is shown the amount and the business's
own payment instructions, and the owner marks the invoice paid by hand.

To add Stripe later, implement `PaymentProvider` (create_checkout + a webhook that calls
`record_payment` only after the provider confirms the charge) and register it in PROVIDERS.
Nothing here ever marks an invoice paid without an explicit confirmation.
"""
from dataclasses import dataclass

from flask import current_app

from .models import Invoice, InvoiceStatus, db, utcnow


@dataclass
class CheckoutResult:
    """What the customer-facing Pay button should do."""
    online: bool              # True if the customer can pay online right now
    redirect_url: str | None  # where to send the customer (provider checkout page)
    message: str              # shown when online payment is unavailable


class PaymentProvider:
    name = "base"

    def create_checkout(self, invoice: Invoice) -> CheckoutResult:
        raise NotImplementedError


class ManualProvider(PaymentProvider):
    """No online payments. Never claims a payment happened."""
    name = "manual"

    def create_checkout(self, invoice: Invoice) -> CheckoutResult:
        return CheckoutResult(
            online=False,
            redirect_url=None,
            message="Online payment isn't available for this invoice yet. "
                    "Please pay the business directly using the details below.",
        )


PROVIDERS: dict[str, type[PaymentProvider]] = {"manual": ManualProvider}


def get_provider() -> PaymentProvider:
    name = current_app.config.get("PAYMENT_PROVIDER", "manual")
    return PROVIDERS.get(name, ManualProvider)()


def ensure_invoice(job) -> Invoice:
    """Create the job's invoice if missing and keep its amount in sync while unpaid."""
    if job.invoice is None:
        job.invoice = Invoice(business_id=job.business_id, amount_cents=job.price_cents)
    elif not job.invoice.is_paid:
        job.invoice.amount_cents = job.price_cents
    return job.invoice


def record_payment(invoice: Invoice, via: str, reference: str | None = None) -> None:
    """Mark paid. Callers: the owner's manual action, or a verified provider webhook."""
    invoice.status = InvoiceStatus.PAID
    invoice.paid_via = via
    invoice.provider_reference = reference
    invoice.paid_at = utcnow()
    db.session.commit()


def mark_unpaid(invoice: Invoice) -> None:
    invoice.status = InvoiceStatus.UNPAID
    invoice.paid_via = None
    invoice.provider_reference = None
    invoice.paid_at = None
    db.session.commit()
