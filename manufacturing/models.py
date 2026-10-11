"""التصنيع: قوائم المواد (BOM) وأوامر الإنتاج.

أمر الإنتاج عند اعتماده يصرف المواد الخام من مستودعها (بالأقرب انتهاءً للتشغيلات)، ويُدخل المنتج التام
لمستودعه بتكلفة = تكلفة المواد الفعلية + تكاليف التشغيل، ويمكن أن يُنشئ له تشغيلة بتاريخ انتهاء.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models, transaction

from accounts.ledger import cancel_gl_entries, make_gl_entries
from core.models import SubmittableDocument, TenantModel
from core.utils import flt
from stock.ledger import cancel_stock_ledger_entries, make_stock_ledger_entry

OVERHEAD_ACCOUNT = "تكاليف التشغيل المحملة على الإنتاج"


def overhead_account(company):
    """حساب تكاليف التشغيل المحملة (يُنشأ تحت «تكلفة المبيعات» إن لم يوجد)."""
    from accounts.models import Account

    account = Account.all_objects.filter(company=company, name=OVERHEAD_ACCOUNT, is_group=False).first()
    if account:
        return account
    parent = Account.all_objects.filter(company=company, number="51", is_group=True).first()
    if parent is None:
        raise ValidationError(f"أضف حساب «{OVERHEAD_ACCOUNT}» في دليل الحسابات.")
    used = set(Account.all_objects.filter(company=company).values_list("number", flat=True))
    number = next((str(n) for n in range(5103, 5199) if str(n) not in used), "")
    return Account.all_objects.create(company=company, number=number, name=OVERHEAD_ACCOUNT, parent=parent,
                                      is_group=False, root_type="expense", account_type="")


def unit_cost_estimate(item, warehouse=None):
    """تكلفة الوحدة التقديرية: متوسط التكلفة في المستودع، أو متوسطها في كل المستودعات، أو آخر سعر شراء."""
    from django.db.models import Sum

    from stock.models import Bin

    if warehouse is not None:
        b = Bin.objects.filter(item=item, warehouse=warehouse).first()
        if b and b.actual_qty > 0:
            return flt(b.valuation_rate)
    agg = Bin.objects.filter(item=item, actual_qty__gt=0).aggregate(v=Sum("stock_value"), q=Sum("actual_qty"))
    if agg["q"]:
        return flt(agg["v"] / agg["q"])
    return flt(item.buying_rate)


class BOM(TenantModel):
    """قائمة المواد: ما يلزم لإنتاج كمية معينة من المنتج."""

    item = models.ForeignKey("stock.Item", models.PROTECT, related_name="boms", verbose_name="المنتج",
                             limit_choices_to={"disabled": False, "is_stock_item": True})
    quantity = models.DecimalField("الكمية المنتجة", max_digits=18, decimal_places=3, default=1,
                                   help_text="المواد أدناه تكفي لإنتاج هذه الكمية (مثلاً 1 قطعة أو 100 كجم)")
    operating_cost = models.DecimalField("تكاليف التشغيل لهذه الكمية", max_digits=18, decimal_places=2, default=0,
                                         help_text="عمالة وكهرباء وتعبئة ومصاريف غير مباشرة؛ تُضاف لتكلفة المنتج")
    is_active = models.BooleanField("فعّالة", default=True)
    is_default = models.BooleanField("الافتراضية للمنتج", default=True)
    remarks = models.TextField("ملاحظات", blank=True)

    class Meta:
        verbose_name = "قائمة مواد"
        verbose_name_plural = "قوائم المواد"
        ordering = ["item__code", "-is_default", "id"]

    def __str__(self):
        return f"قائمة مواد {self.item.name} ({flt(self.quantity, 3).normalize():f} {self.item.uom})"

    def clean(self):
        if self.quantity is not None and self.quantity <= 0:
            raise ValidationError({"quantity": "الكمية المنتجة يجب أن تكون أكبر من صفر."})

    def save(self, *args, **kwargs):
        with transaction.atomic():
            super().save(*args, **kwargs)
            if self.is_default:
                BOM.objects.filter(item=self.item_id, is_default=True).exclude(pk=self.pk).update(is_default=False)

    def scaled(self, qty):
        """المواد اللازمة لإنتاج كمية qty: [(صنف، كمية)]."""
        factor = Decimal(qty) / self.quantity if self.quantity else Decimal(0)
        return [(row.item, flt(row.qty * factor, 3)) for row in self.items.select_related("item", "item__uom")]

    def cost_breakdown(self, warehouse=None):
        rows, material = [], flt(0)
        for row in self.items.select_related("item", "item__uom"):
            rate = unit_cost_estimate(row.item, warehouse)
            amount = flt(rate * row.qty)
            material += amount
            rows.append({"item": row.item, "qty": row.qty, "rate": rate, "amount": amount})
        from stock.pricing import selling_net

        total = material + flt(self.operating_cost)
        unit = flt(total / self.quantity) if self.quantity else flt(0)
        net = selling_net(self.item, self.company)
        return {"rows": rows, "material": material, "operating": flt(self.operating_cost), "total": total,
                "unit": unit, "selling_net": net, "unit_profit": net - unit,
                "margin": flt((net - unit) * 100 / net) if net else None}

    def producible(self, warehouse):
        """أقصى كمية يمكن إنتاجها من رصيد المواد في مستودع، والمادة المقيِّدة."""
        from stock.models import Bin

        best, limiting = None, None
        for row in self.items.select_related("item"):
            if row.qty <= 0:
                continue
            if row.item.has_batch_no:  # الصالح فقط؛ المنتهي لا يُصرف في الإنتاج
                from django.utils import timezone

                from stock.batches import available_batches

                available = sum((q for _b, q in available_batches(row.item, warehouse, timezone.localdate())),
                                flt(0, 3))
            else:
                b = Bin.objects.filter(item=row.item, warehouse=warehouse).first()
                available = flt(b.actual_qty, 3) if b else flt(0, 3)
            possible = flt(available / row.qty * self.quantity, 3)
            if best is None or possible < best:
                best, limiting = possible, row.item
        return best or flt(0, 3), limiting


    def producible_by_warehouse(self):
        from stock.models import Warehouse

        result = []
        for wh in Warehouse.objects.filter(disabled=False):
            qty, limiting = self.producible(wh)
            result.append({"warehouse": wh, "qty": qty, "limiting": limiting})
        return result

    @property
    def cost_summary(self):
        return self.cost_breakdown()


class BOMItem(models.Model):
    parent = models.ForeignKey(BOM, models.CASCADE, related_name="items")
    item = models.ForeignKey("stock.Item", models.PROTECT, verbose_name="المادة",
                             limit_choices_to={"disabled": False, "is_stock_item": True})
    qty = models.DecimalField("الكمية", max_digits=18, decimal_places=3, default=1)

    class Meta:
        verbose_name = "مادة"
        verbose_name_plural = "المواد الخام"


class Production(SubmittableDocument):
    """أمر إنتاج: يصرف المواد ويُدخل المنتج التام بتكلفته الفعلية."""

    naming_prefix = "MFG"

    bom = models.ForeignKey(BOM, models.PROTECT, related_name="productions", verbose_name="قائمة المواد",
                            limit_choices_to={"is_active": True})
    item = models.ForeignKey("stock.Item", models.PROTECT, related_name="+", verbose_name="المنتج", editable=False)
    qty = models.DecimalField("الكمية المطلوب إنتاجها", max_digits=18, decimal_places=3, default=1)
    source_warehouse = models.ForeignKey("stock.Warehouse", models.PROTECT, related_name="+",
                                         verbose_name="مستودع المواد الخام")
    target_warehouse = models.ForeignKey("stock.Warehouse", models.PROTECT, related_name="+",
                                         verbose_name="مستودع المنتج التام")
    batch_no = models.CharField("رقم تشغيلة المنتج", max_length=40, blank=True,
                                help_text="للمنتجات المتتبعة؛ يُترك فارغاً لرقم تلقائي")
    expiry_date = models.DateField("تاريخ انتهاء المنتج", null=True, blank=True,
                                   help_text="يُحسب تلقائياً من مدة صلاحية المنتج إن تُرك فارغاً")
    manual_consumption = models.BooleanField(
        "تعديل الكميات المستهلكة يدوياً", default=False,
        help_text="بدونه تُحسب كميات المواد من قائمة المواد حسب الكمية المنتجة")
    additional_cost = models.DecimalField("تكاليف إضافية", max_digits=18, decimal_places=2, default=0,
                                          help_text="تكاليف خاصة بهذا الأمر فوق تكاليف التشغيل في القائمة")
    operating_cost = models.DecimalField("تكاليف التشغيل", max_digits=18, decimal_places=2, default=0,
                                         editable=False)
    material_cost = models.DecimalField("تكلفة المواد", max_digits=18, decimal_places=2, default=0, editable=False)
    total_cost = models.DecimalField("إجمالي تكلفة الإنتاج", max_digits=18, decimal_places=2, default=0,
                                     editable=False)
    unit_cost = models.DecimalField("تكلفة الوحدة", max_digits=18, decimal_places=4, default=0, editable=False)

    class Meta(SubmittableDocument.Meta):
        verbose_name = "أمر إنتاج"
        verbose_name_plural = "أوامر الإنتاج"

    def save(self, *args, **kwargs):
        if self.bom_id:
            self.item_id = self.bom.item_id
        super().save(*args, **kwargs)

    def calculate_totals(self):
        if not self.pk or not self.bom_id:
            return
        if self.is_draft:
            required = {}
            for item, qty in self.bom.scaled(self.qty or 0):
                required[item.pk] = required.get(item.pk, flt(0, 3)) + qty
            rows = {row.item_id: row for row in self.items.all()}
            for item_id, qty in required.items():
                row = rows.get(item_id) or ProductionItem(parent=self, item_id=item_id, qty=qty)
                row.required_qty = qty
                if not self.manual_consumption:
                    row.qty = qty
                row.save()
            if not self.manual_consumption:
                self.items.exclude(item_id__in=required).delete()
            else:
                self.items.exclude(item_id__in=required).update(required_qty=0)
            self.operating_cost = flt(self.bom.operating_cost * (self.qty or 0) / self.bom.quantity) \
                if self.bom.quantity else flt(0)
            material = flt(0)
            for row in self.items.select_related("item"):
                row.rate = unit_cost_estimate(row.item, self.source_warehouse if self.source_warehouse_id else None)
                row.amount = flt(row.rate * row.qty)
                row.save(update_fields=["rate", "amount"])
                material += row.amount
            self.material_cost = material
        self.total_cost = flt(self.material_cost + self.operating_cost + flt(self.additional_cost))
        self.unit_cost = flt(self.total_cost / self.qty, 4) if self.qty else flt(0)

    def validate_for_submit(self):
        if not self.bom.is_active:
            raise ValidationError("قائمة المواد غير فعّالة.")
        if self.qty <= 0:
            raise ValidationError("الكمية المطلوب إنتاجها يجب أن تكون أكبر من صفر.")
        if self.additional_cost < 0:
            raise ValidationError("التكاليف الإضافية لا تكون سالبة.")
        rows = list(self.items.select_related("item"))
        if not rows or not any(r.qty > 0 for r in rows):
            raise ValidationError("لا توجد مواد لصرفها؛ راجع قائمة المواد.")
        for row in rows:
            if row.qty < 0:
                raise ValidationError(f"الكمية المستهلكة من «{row.item.name}» لا تكون سالبة.")
            if row.item_id == self.item_id:
                raise ValidationError("المنتج لا يكون ضمن مواده.")
            if not row.item.is_stock_item:
                raise ValidationError(f"«{row.item.name}» ليس صنفاً مخزنياً.")

    def on_submit(self):
        material = flt(0)
        for row in self.items.select_related("item").order_by("id"):
            if row.qty <= 0:
                continue
            cost = -make_stock_ledger_entry(self, row.item, self.source_warehouse, -row.qty, batch_no=row.batch_no)
            row.rate = flt(cost / row.qty) if row.qty else flt(0)
            row.amount = cost
            row.save(update_fields=["rate", "amount"])
            material += cost
        self.material_cost = material
        overhead = flt(self.operating_cost + flt(self.additional_cost))
        self.total_cost = flt(material + overhead)
        self.unit_cost = flt(self.total_cost / self.qty, 4)
        self.save(update_fields=["material_cost", "total_cost", "unit_cost"])
        value = make_stock_ledger_entry(self, self.item, self.target_warehouse, self.qty, self.total_cost / self.qty,
                                        batch_no=self.batch_no, expiry_date=self.expiry_date,
                                        manufacturing_date=self.posting_date)
        stock = self.company.require_account("default_stock_account")
        # المواد خرجت من المخزون والمنتج دخل بقيمة المواد + تكاليف التشغيل المحملة
        entries = [{"account": stock, "debit": value}, {"account": stock, "credit": material}]
        rounding = flt(value - material - overhead)
        if overhead:
            entries.append({"account": overhead_account(self.company), "credit": overhead})
        if rounding:
            entries.append({"account": self.company.require_account("default_stock_adjustment_account"),
                            "credit": rounding})
        if overhead or rounding:
            make_gl_entries(self, entries, remarks=self.remarks or f"إنتاج {self.qty} {self.item.name}")

    def on_cancel(self):
        cancel_stock_ledger_entries(self)
        cancel_gl_entries(self)

    @property
    def availability(self):
        """توفر المواد في مستودع المواد الخام (الصالح فقط للأصناف المتتبعة بالتشغيلات)."""
        from stock.batches import available_batches
        from stock.models import Bin

        rows = []
        for row in self.items.select_related("item", "item__uom").order_by("id"):
            if row.item.has_batch_no:
                available = sum((q for _b, q in available_batches(row.item, self.source_warehouse, self.posting_date)),
                                flt(0, 3))
            else:
                b = Bin.objects.filter(item=row.item, warehouse=self.source_warehouse).first()
                available = flt(b.actual_qty, 3) if b else flt(0, 3)
            rows.append({"row": row, "available": available, "short": max(row.qty - available, flt(0, 3))})
        return rows

    @property
    def has_shortage(self):
        return any(r["short"] > 0 for r in self.availability)

    @property
    def output_batch(self):
        from stock.models import StockLedgerEntry

        sle = StockLedgerEntry.objects.filter(voucher_type=self.doctype_slug, voucher_id=self.pk, item=self.item,
                                              actual_qty__gt=0, is_cancelled=False, batch__isnull=False).first()
        return sle.batch if sle else None


class ProductionItem(models.Model):
    parent = models.ForeignKey(Production, models.CASCADE, related_name="items")
    item = models.ForeignKey("stock.Item", models.PROTECT, verbose_name="المادة",
                             limit_choices_to={"disabled": False, "is_stock_item": True})
    required_qty = models.DecimalField("الكمية حسب القائمة", max_digits=18, decimal_places=3, default=0,
                                       editable=False)
    qty = models.DecimalField("الكمية المستهلكة", max_digits=18, decimal_places=3, default=0)
    batch_no = models.CharField("التشغيلة", max_length=40, blank=True,
                                help_text="فارغ = الأقرب انتهاءً تلقائياً")
    rate = models.DecimalField("تكلفة الوحدة", max_digits=18, decimal_places=4, default=0, editable=False)
    amount = models.DecimalField("التكلفة", max_digits=18, decimal_places=2, default=0, editable=False)

    class Meta:
        verbose_name = "مادة مستهلكة"
        verbose_name_plural = "المواد المستهلكة"
