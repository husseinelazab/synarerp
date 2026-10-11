"""الرسائل الآلية عند اعتماد فاتورة آجلة أو استلام دفعة (إن فعّلها المدير وكان المزوّد مفعّلاً)."""
import logging

from django.db import transaction
from django.dispatch import receiver

from core.models import document_submitted

log = logging.getLogger(__name__)


@receiver(document_submitted)
def auto_messages(sender, doc, **kwargs):
    from accounts.models import PaymentEntry
    from selling.models import SalesInvoice

    if isinstance(doc, SalesInvoice):
        if doc.is_return or doc.outstanding_amount <= 0:
            return
        transaction.on_commit(lambda: _send(doc.company, "invoice", doc))
    elif isinstance(doc, PaymentEntry) and doc.payment_type == "receive" and doc.customer_id:
        transaction.on_commit(lambda: _send(doc.company, "payment", doc))


def _send(company, event, doc):
    from messaging.models import MessagingSettings
    from messaging.service import compose, customer_balance, invoice_context, send

    cfg = MessagingSettings.all_objects.filter(company=company).first()
    if cfg is None or not cfg.active or not getattr(cfg, f"auto_{event}"):
        return
    customer = doc.customer
    if not customer.phone:
        return
    try:
        if event == "invoice":
            body = compose(company, event, **invoice_context(doc))
        else:
            body = compose(company, event, customer=customer.name, amount=doc.amount, balance=customer_balance(customer))
        send(company, customer.phone, body, event=event, reference=doc.name, customer=customer, mode="provider")
    except Exception:  # noqa: BLE001 — الرسالة لا تؤثر على المستند
        log.exception("تعذر إرسال رسالة آلية للمستند %s", doc.name)
