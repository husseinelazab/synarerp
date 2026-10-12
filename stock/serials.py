"""الأرقام التسلسلية: التحقق منها وتحديث حالتها مع كل حركة مخزون، واستعادتها عند الإلغاء."""
import re

from django.core.exceptions import ValidationError

from core.utils import flt


def parse(text):
    """قائمة الأرقام من نص (سطر أو فاصلة أو مسافة بينها)، بلا تكرار."""
    values = [v.strip() for v in re.split(r"[\s,،;]+", text or "") if v.strip()]
    duplicates = sorted({v for v in values if values.count(v) > 1})
    if duplicates:
        raise ValidationError(f"أرقام تسلسلية مكررة: {'، '.join(duplicates)}.")
    return values


def apply(doc, item, warehouse, qty, text, sle):
    """تحديث حالة الأرقام التسلسلية لحركة واحدة وربطها بسطر دفتر المخزون."""
    from django.utils import timezone

    from stock.models import SerialNo, SerialStatus

    if not item.has_serial_no:
        if (text or "").strip():
            raise ValidationError(f"الصنف «{item.name}» لا يتتبع الأرقام التسلسلية؛ احذف الأرقام من سطره.")
        return
    if text is None:
        raise ValidationError(f"«{item.name}» صنف بأرقام تسلسلية ولا يُدعم في هذا المستند؛ "
                              "استخدم الفواتير أو الأذون أو الحركات المخزنية مع إدخال الأرقام.")
    serials = parse(text)
    qty = flt(qty, 3)
    if qty != int(qty) or len(serials) != abs(int(qty)):
        raise ValidationError(f"«{item.name}»: الكمية {abs(qty)} تتطلب {abs(qty)} رقماً تسلسلياً (المُدخل {len(serials)}).")
    existing = {s.serial_no: s for s in SerialNo.objects.filter(item=item, serial_no__in=serials)}
    objs = []
    for value in serials:
        obj = existing.get(value)
        if qty > 0:
            if obj is not None and obj.status == SerialStatus.IN_STOCK:
                raise ValidationError(f"الرقم التسلسلي {value} لـ«{item.name}» موجود بالفعل في المخزون ({obj.warehouse}).")
            if obj is None:
                obj = SerialNo(company=doc.company, item=item, serial_no=value, purchase_document=doc.name,
                               purchase_date=doc.posting_date)
            obj.status, obj.warehouse = SerialStatus.IN_STOCK, warehouse
        else:
            if obj is None or obj.status != SerialStatus.IN_STOCK or obj.warehouse_id != warehouse.pk:
                where = "غير مسجل" if obj is None else f"حالته {obj.get_status_display()}" + (
                    f" في {obj.warehouse}" if obj.warehouse_id else "")
                raise ValidationError(f"الرقم التسلسلي {value} لـ«{item.name}» غير متاح في «{warehouse}» ({where}).")
            obj.status, obj.warehouse = SerialStatus.DELIVERED, None
            obj.delivery_document, obj.delivery_date = doc.name, doc.posting_date
            customer = getattr(doc, "customer", None)
            if customer is not None:
                obj.customer = customer
                obj.warranty_expiry = (doc.posting_date + timezone.timedelta(days=item.warranty_days)
                                       if item.warranty_days else None)
        obj.save()
        objs.append(obj)
    sle.serials.add(*objs)


def restore(entries):
    """بعد إلغاء حركات: حالة كل رقم = آخر حركة نشطة عليه (أو غير نشط إن لم يبق له شيء)."""
    from stock.models import SerialNo, SerialStatus

    for serial in SerialNo.objects.filter(ledger_entries__in=entries).distinct():
        last = serial.ledger_entries.filter(is_cancelled=False).order_by("-id").first()
        if last is None:
            serial.status, serial.warehouse = SerialStatus.INACTIVE, None
        elif last.actual_qty > 0:
            serial.status, serial.warehouse = SerialStatus.IN_STOCK, last.warehouse
        else:
            serial.status, serial.warehouse = SerialStatus.DELIVERED, None
        serial.save(update_fields=["status", "warehouse"])
