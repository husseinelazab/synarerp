"""إنشاء فاتورة المبيعات (وسند القبض للطلب المدفوع) من طلب متجر، مع قائمة مراجعة لما لا يكتمل."""
import datetime
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from core.utils import flt
from ecommerce.models import CustomerMode, ExternalOrder, OrderStatus, SkuMapping
from ecommerce.platforms import CANCELLED_STATUSES, NORMALIZERS, UNPAID_METHODS

TOLERANCE = Decimal("0.05")


def receive(connection, body, event_hint=""):
    """معالجة حمولة Webhook. تعيد الطلب، أو None لحدث لا يخص الطلبات (يُقبل ويُتجاهل)."""
    order_data = NORMALIZERS[connection.platform](body)
    event = order_data["event"] or event_hint
    if connection.store_id and order_data["store_id"] and order_data["store_id"] != connection.store_id:
        raise PermissionDenied("الطلب من متجر آخر.")
    if event and not event.startswith("order") or not order_data["external_id"]:
        return None
    order, created = ExternalOrder.objects.get_or_create(
        connection=connection, external_id=order_data["external_id"],
        defaults={"company": connection.company, "payload": body})
    order.payload = body
    order.number = order_data["number"][:64]
    order.customer_name = (order_data["customer"]["name"] or "")[:200]
    order.total = order_data["total"] or 0
    order.save()
    connection.last_event_at = timezone.now()
    connection.save(update_fields=["last_event_at"])
    if order_data["status"] in CANCELLED_STATUSES:
        cancel(order)
    elif order.status == OrderStatus.REVIEW and not order.invoice_id:
        process(order, order_data)
    return order


def retry(order):
    if order.status != OrderStatus.REVIEW:
        raise ValidationError("يُعاد تنفيذ الطلبات التي تحتاج مراجعة فقط.")
    if order.invoice_id:
        invoice = order.invoice
        if not invoice.is_draft:
            raise ValidationError("للطلب فاتورة معتمدة بالفعل.")
        _submit(order, invoice, NORMALIZERS[order.connection.platform](order.payload))
        return
    process(order, NORMALIZERS[order.connection.platform](order.payload))


def _review(order, message):
    order.status, order.error = OrderStatus.REVIEW, message
    order.save(update_fields=["status", "error", "invoice", "payment", "updated_at"])


def order_date(value):
    if isinstance(value, datetime.datetime):
        return value.date()
    text = str(value or "").strip()
    parsed = parse_datetime(text.replace(" ", "T", 1)[:26]) if text else None
    if parsed is None and text:
        parsed = parse_date(text[:10])
    if isinstance(parsed, datetime.datetime):
        return parsed.date()
    return parsed or timezone.localdate()


def find_customer(connection, info):
    from messaging.service import normalize_phone
    from selling.models import Customer

    if connection.customer_mode == CustomerMode.SINGLE:
        return connection.default_customer
    phone = normalize_phone(info.get("phone"))
    if phone:
        match = Customer.objects.filter(phone__endswith=phone[-9:]).first()
        if match:
            return match
    if info.get("email"):
        match = Customer.objects.filter(email__iexact=info["email"]).first()
        if match:
            return match
    if not info.get("name") and not phone:
        return connection.default_customer
    name = (info.get("name") or f"عميل {phone}")[:180]
    if Customer.objects.filter(name=name).exists():
        name = f"{name} ({phone[-4:] or connection.name})"[:200]
    return Customer.objects.create(name=name, customer_type="individual", phone=info.get("phone") or "",
                                   email=info.get("email") or "", city=str(info.get("city") or "")[:100])


def resolve_item(connection, sku, name):
    from stock.models import Item

    mapping = SkuMapping.objects.filter(connection=connection, sku=sku).select_related("item").first() if sku else None
    if mapping:
        return mapping.item, mapping.uom_id
    if sku:
        item, unit = Item.match_code(sku)
        if item:
            return item, (unit.uom_id if unit else None)
    return None, None


def shipping_item(connection):
    from stock.models import UOM, Item, ItemGroup

    if connection.shipping_item_id:
        return connection.shipping_item
    item = Item.objects.filter(code="SHIPPING").first()
    if item is None:
        item = Item.objects.create(code="SHIPPING", name="رسوم الشحن والتوصيل", item_group=ItemGroup.objects.first(),
                                   uom=UOM.objects.filter(name="خدمة").first() or UOM.objects.first(),
                                   is_stock_item=False)
    connection.shipping_item = item
    connection.save(update_fields=["shipping_item"])
    return item


def _rate(connection, line):
    """سعر الوحدة بصيغة أسعار المتجر (شاملة أو غير شاملة الضريبة)."""
    factor = 1 + Decimal(connection.tax_rate) / 100
    gross, net = line["unit_gross"], line["unit_net"]
    if gross is None and line["line_total"] is not None and line["qty"]:
        gross = (line["line_total"] / line["qty"]).quantize(Decimal("0.01"))
    if connection.prices_include_tax:
        return gross if gross is not None else (flt(net * factor) if net is not None else None)
    return net if net is not None else (flt(gross / factor) if gross is not None else None)


def process(order, data):
    from selling.models import SalesInvoice

    connection = order.connection
    if not data["items"]:
        return _review(order, "الطلب بلا أصناف.")
    lines, missing = [], []
    for line in data["items"]:
        item, uom_id = resolve_item(connection, line["sku"], line["name"])
        rate = _rate(connection, line)
        if item is None:
            missing.append(line["sku"] or f"(بلا SKU) {line['name']}")
        elif rate is None or line["qty"] <= 0:
            return _review(order, f"تعذر قراءة الكمية أو السعر للصنف {line['sku'] or line['name']}.")
        else:
            lines.append((item, uom_id, line["qty"], rate))
    if missing:
        return _review(order, "أصناف غير معرّفة في النظام (أضف SKU في كود الصنف أو باركوده، أو اربطها): "
                       + "، ".join(missing))
    customer = find_customer(connection, data["customer"])
    if customer is None:
        return _review(order, "الطلب بلا بيانات مشترٍ؛ حدد «العميل الافتراضي» في إعدادات المتجر.")
    with transaction.atomic():
        invoice = SalesInvoice.objects.create(
            company=connection.company, customer=customer, posting_date=order_date(data["date"]),
            warehouse=connection.warehouse, update_stock=True, cost_center=connection.cost_center,
            tax_rate=connection.tax_rate, prices_include_tax=connection.prices_include_tax,
            discount_amount=data["discount"] or 0,
            remarks=f"طلب {connection.get_platform_display()} رقم {data['number']} — {connection.name}")
        for item, uom_id, qty, rate in lines:
            invoice.items.create(item=item, uom_id=uom_id, qty=qty, rate=rate)
        if data["shipping"]:
            shipping = data["shipping"] if connection.prices_include_tax else flt(
                data["shipping"] / (1 + Decimal(connection.tax_rate) / 100))
            invoice.items.create(item=shipping_item(connection), qty=1, rate=shipping)
        invoice.calculate_totals()
        invoice.save()
    order.invoice = invoice
    order.save(update_fields=["invoice"])
    _submit(order, invoice, data)


def _submit(order, invoice, data):
    from accounts.models import PaymentEntry

    connection = order.connection
    invoice.refresh_from_db()
    expected = data["total"]
    if expected is not None and abs(invoice.grand_total - expected) > TOLERANCE:
        return _review(order, f"إجمالي الفاتورة {invoice.grand_total} يختلف عن إجمالي الطلب في المتجر {expected}؛ "
                              f"راجع الأسعار أو الخصم أو الشحن ثم اعتمد الفاتورة يدوياً.")
    if not connection.auto_submit:
        return _review(order, "الاعتماد التلقائي معطّل: راجع الفاتورة واعتمدها.")
    try:
        with transaction.atomic():
            invoice.submit()
    except ValidationError as exc:
        return _review(order, "تعذر اعتماد الفاتورة: " + " ".join(exc.messages))
    note = ""
    if data["payment_method"] not in UNPAID_METHODS and connection.payment_account_id:
        try:
            with transaction.atomic():
                payment = PaymentEntry.objects.create(
                    company=connection.company, payment_type="receive", customer=invoice.customer, sales_invoice=invoice,
                    account=connection.payment_account, amount=invoice.grand_total, posting_date=invoice.posting_date,
                    reference_no=f"{connection.get_platform_display()} {data['number']}"[:50],
                    remarks=f"تحصيل طلب {data['number']} ({data['payment_method'] or 'دفع إلكتروني'})")
                payment.submit()
            order.payment = payment
        except ValidationError as exc:
            note = "أُنشئت الفاتورة لكن تعذر تسجيل التحصيل: " + " ".join(exc.messages)
    order.status, order.error = OrderStatus.IMPORTED, note
    order.save(update_fields=["status", "error", "payment", "updated_at"])


def cancel(order):
    """طلب أُلغي أو استُرجع في المتجر: تُحذف مسودته، أو تُنشأ مسودة مرتجع لفاتورته المعتمدة مع تنبيه للمدير."""
    from core.notify import notify_managers

    invoice = order.invoice
    if order.status == OrderStatus.CANCELLED:
        return
    note = ""
    if invoice is not None and invoice.is_draft:
        order.invoice = None
        order.save(update_fields=["invoice"])
        invoice.delete()
    elif invoice is not None and invoice.is_submitted:
        ret = invoice.make_return()
        note = f"أُنشئت مسودة المرتجع {ret.name}؛ راجعها واعتمدها (ورد المبلغ للعميل إن كان مدفوعاً)."
        transaction.on_commit(lambda: notify_managers(
            order.company, f"أُلغي طلب {order.connection.name} رقم {order.number} في المتجر — {note}",
            ret.get_absolute_url()))
    order.status, order.error = OrderStatus.CANCELLED, note
    order.save(update_fields=["status", "error", "updated_at"])
