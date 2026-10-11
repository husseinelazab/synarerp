"""التصنيع: قوائم المواد (BOM) وأوامر الإنتاج.

أمر الإنتاج عند اعتماده يصرف المواد الخام من مستودعها (بالأقرب انتهاءً للتشغيلات)، ويُدخل المنتج التام
لمستودعه بتكلفة = تكلفة المواد الفعلية + تكاليف التشغيل، ويمكن أن يُنشئ له تشغيلة بتاريخ انتهاء.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models, transaction

from accounts.ledger import cancel_gl_entries, make_gl_entries
from core.models import SubmittableDocument, TenantModel
from core.tenancy import tenant_unique
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


def unit_cost_estimate(item, warehouse=None, _depth=0):
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
    # مكوّن نصف مصنّع بلا رصيد: تكلفته من قائمة مواده (متعدد المستويات)
    if _depth < 8:
        bom = default_bom(item)
        if bom is not None:
            return bom.cost_breakdown(warehouse, _depth=_depth + 1)["unit"]
    return flt(item.buying_rate)


def default_bom(item):
    return BOM.objects.filter(item=item, is_active=True).order_by("-is_default", "-id").first()


class Workstation(TenantModel):
    """محطة عمل (آلة، خط إنتاج، قسم) وتكلفة ساعتها: عمالة + كهرباء + إهلاك."""

    name = models.CharField("اسم المحطة", max_length=100)
    hourly_rate = models.DecimalField("تكلفة الساعة", max_digits=18, decimal_places=2, default=0)
    description = models.TextField("الوصف", blank=True)

    class Meta:
        verbose_name = "محطة عمل"
        verbose_name_plural = "محطات العمل"
        ordering = ["name"]
        constraints = [tenant_unique("name", model_label="workstation")]

    def __str__(self):
        return self.name


class Routing(TenantModel):
    """مسار التشغيل: العمليات ومحطاتها والوقت اللازم لكل وحدة من المنتج."""

    name = models.CharField("اسم المسار", max_length=100)

    class Meta:
        verbose_name = "مسار تشغيل"
        verbose_name_plural = "مسارات التشغيل"
        ordering = ["name"]
        constraints = [tenant_unique("name", model_label="routing")]

    def __str__(self):
        return self.name

    @property
    def cost_per_unit(self):
        return sum((flt(op.minutes * op.workstation.hourly_rate / 60) for op in
                    self.operations.select_related("workstation")), flt(0))

    @property
    def minutes_per_unit(self):
        return sum((op.minutes for op in self.operations.all()), Decimal(0))


class RoutingOperation(models.Model):
    parent = models.ForeignKey(Routing, models.CASCADE, related_name="operations")
    operation = models.CharField("العملية", max_length=100)
    workstation = models.ForeignKey(Workstation, models.PROTECT, verbose_name="محطة العمل")
    minutes = models.DecimalField("الدقائق لكل وحدة", max_digits=10, decimal_places=3, default=1)

    class Meta:
        verbose_name = "عملية"
        verbose_name_plural = "العمليات"


class BOM(TenantModel):
    """قائمة المواد: ما يلزم لإنتاج كمية معينة من المنتج."""

    item = models.ForeignKey("stock.Item", models.PROTECT, related_name="boms", verbose_name="المنتج",
                             limit_choices_to={"disabled": False, "is_stock_item": True})
    quantity = models.DecimalField("الكمية المنتجة", max_digits=18, decimal_places=3, default=1,
                                   help_text="المواد أدناه تكفي لإنتاج هذه الكمية (مثلاً 1 قطعة أو 100 كجم)")
    operating_cost = models.DecimalField("تكاليف التشغيل لهذه الكمية", max_digits=18, decimal_places=2, default=0,
                                         help_text="عمالة وكهرباء وتعبئة ومصاريف غير مباشرة؛ تُضاف لتكلفة المنتج")
    routing = models.ForeignKey(Routing, models.PROTECT, null=True, blank=True, related_name="boms",
                                verbose_name="مسار التشغيل",
                                help_text="اختياري: تُضاف تكلفة وقت العمليات على محطات العمل لتكاليف التشغيل")
    scrap_item = models.ForeignKey("stock.Item", models.PROTECT, null=True, blank=True, related_name="+",
                                   verbose_name="صنف المخلفات القابلة للبيع",
                                   limit_choices_to={"disabled": False, "is_stock_item": True},
                                   help_text="مثل النخالة أو بقايا القص؛ تدخل المخزون وتُخصم قيمتها من تكلفة المنتج")
    scrap_qty = models.DecimalField("كمية المخلفات لهذه الكمية", max_digits=18, decimal_places=3, default=0)
    scrap_rate = models.DecimalField("قيمة وحدة المخلفات", max_digits=18, decimal_places=2, default=0)
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

    def factor(self, qty):
        return Decimal(qty) / self.quantity if self.quantity else Decimal(0)

    def scaled(self, qty):
        """المواد اللازمة لإنتاج كمية qty شاملة نسبة الهالك: [(صنف، كمية)]."""
        f = self.factor(qty)
        return [(row.item, row.gross_qty(f)) for row in self.items.select_related("item", "item__uom")]

    def operating_for(self, qty):
        """تكاليف التشغيل لكمية: المبلغ الثابت في القائمة + وقت العمليات على محطات العمل."""
        cost = flt(self.operating_cost * self.factor(qty))
        if self.routing_id:
            cost += flt(self.routing.cost_per_unit * Decimal(qty))
        return cost

    def scrap_for(self, qty):
        return flt(self.scrap_qty * self.factor(qty), 3) if self.scrap_item_id else flt(0, 3)

    def cost_breakdown(self, warehouse=None, _depth=0):
        rows, material = [], flt(0)
        for row in self.items.select_related("item", "item__uom"):
            qty = row.gross_qty(1)
            rate = unit_cost_estimate(row.item, warehouse, _depth)
            amount = flt(rate * qty)
            material += amount
            rows.append({"item": row.item, "qty": qty, "rate": rate, "amount": amount,
                         "scrap_percent": row.scrap_percent, "sub_bom": default_bom(row.item)})
        from stock.pricing import selling_net

        operating = self.operating_for(self.quantity)
        scrap_value = flt(self.scrap_qty * self.scrap_rate) if self.scrap_item_id else flt(0)
        total = material + operating - scrap_value
        unit = flt(total / self.quantity) if self.quantity else flt(0)
        net = selling_net(self.item, self.company)
        return {"rows": rows, "material": material, "operating": operating, "fixed_operating": flt(self.operating_cost),
                "routing": operating - flt(self.operating_cost), "scrap_value": scrap_value, "total": total,
                "unit": unit, "selling_net": net, "unit_profit": net - unit,
                "margin": flt((net - unit) * 100 / net) if net else None}

    def exploded(self, qty=None, level=0, seen=None):
        """شجرة المواد متعددة المستويات: [{item, qty, level, bom}]."""
        seen = (seen or set()) | {self.item_id}
        rows = []
        for item, q in self.scaled(self.quantity if qty is None else qty):
            sub = default_bom(item) if item.pk not in seen and level < 8 else None
            rows.append({"item": item, "qty": q, "level": level, "bom": sub})
            if sub is not None:
                rows += sub.exploded(q, level + 1, seen)
        return rows

    @property
    def tree(self):
        return self.exploded()

    @property
    def is_multilevel(self):
        return any(r["level"] for r in self.tree)

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
    scrap_percent = models.DecimalField("نسبة الهالك %", max_digits=5, decimal_places=2, default=0,
                                        help_text="فاقد التصنيع؛ تزيد به الكمية المصروفة")

    def gross_qty(self, factor):
        return flt(self.qty * Decimal(factor) * (100 + flt(self.scrap_percent)) / 100, 3)

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
    rejected_qty = models.DecimalField("الكمية التالفة من المنتج", max_digits=18, decimal_places=3, default=0,
                                       help_text="وحدات خرجت تالفة؛ تُحمّل تكلفتها على الوحدات السليمة")
    scrap_qty = models.DecimalField("كمية المخلفات القابلة للبيع", max_digits=18, decimal_places=3, default=0,
                                    help_text="تُحسب من قائمة المواد؛ عدّلها بالفعلي مع «تعديل الكميات يدوياً»")
    scrap_value = models.DecimalField("قيمة المخلفات", max_digits=18, decimal_places=2, default=0, editable=False)
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
            self.operating_cost = self.bom.operating_for(self.qty or 0)
            if not self.manual_consumption:
                self.scrap_qty = self.bom.scrap_for(self.qty or 0)
            self.scrap_value = flt(flt(self.scrap_qty) * self.bom.scrap_rate) if self.bom.scrap_item_id else flt(0)
            material = flt(0)
            for row in self.items.select_related("item"):
                row.rate = unit_cost_estimate(row.item, self.source_warehouse if self.source_warehouse_id else None)
                row.amount = flt(row.rate * row.qty)
                row.save(update_fields=["rate", "amount"])
                material += row.amount
            self.material_cost = material
        self.total_cost = flt(self.material_cost + self.operating_cost + flt(self.additional_cost) - self.scrap_value)
        good = self.good_qty
        self.unit_cost = flt(self.total_cost / good, 4) if good > 0 else flt(0)

    @property
    def good_qty(self):
        return flt(flt(self.qty or 0, 3) - flt(self.rejected_qty or 0, 3), 3)

    @property
    def planned_minutes(self):
        return flt(self.bom.routing.minutes_per_unit * self.qty, 1) if self.bom.routing_id else None

    @property
    def waste_percent(self):
        return flt(flt(self.rejected_qty) * 100 / self.qty) if self.qty else flt(0)

    def validate_for_submit(self):
        if not self.bom.is_active:
            raise ValidationError("قائمة المواد غير فعّالة.")
        if self.qty <= 0:
            raise ValidationError("الكمية المطلوب إنتاجها يجب أن تكون أكبر من صفر.")
        if self.additional_cost < 0:
            raise ValidationError("التكاليف الإضافية لا تكون سالبة.")
        if self.rejected_qty < 0 or self.rejected_qty >= self.qty:
            raise ValidationError("الكمية التالفة يجب أن تكون أقل من الكمية المنتجة.")
        if self.scrap_qty < 0:
            raise ValidationError("كمية المخلفات لا تكون سالبة.")
        if self.scrap_value >= flt(self.material_cost + self.operating_cost + flt(self.additional_cost)) and self.scrap_value:
            raise ValidationError("قيمة المخلفات لا تتجاوز تكلفة الإنتاج.")
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
        scrap_in = flt(0)
        if self.bom.scrap_item_id and self.scrap_qty > 0:
            # المخلفات القابلة للبيع تدخل المخزون بقيمتها وتُخصم من تكلفة المنتج
            scrap_in = make_stock_ledger_entry(self, self.bom.scrap_item, self.target_warehouse, self.scrap_qty,
                                               self.bom.scrap_rate)
        self.scrap_value = scrap_in
        self.total_cost = flt(material + overhead - scrap_in)
        good = self.good_qty
        self.unit_cost = flt(self.total_cost / good, 4)
        self.save(update_fields=["material_cost", "total_cost", "unit_cost", "scrap_value"])
        value = make_stock_ledger_entry(self, self.item, self.target_warehouse, good, self.total_cost / good,
                                        batch_no=self.batch_no, expiry_date=self.expiry_date,
                                        manufacturing_date=self.posting_date)
        stock = self.company.require_account("default_stock_account")
        # المواد خرجت من المخزون، والمنتج السليم والمخلفات دخلا بقيمة المواد + تكاليف التشغيل المحملة
        entries = [{"account": stock, "debit": value + scrap_in}, {"account": stock, "credit": material}]
        rounding = flt(value + scrap_in - material - overhead)
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
    def draft_actions(self):
        if self.is_draft and any(a["short"] > 0 and default_bom(a["row"].item) for a in self.availability):
            return [("make_sub_productions", "إنشاء أوامر إنتاج للمكونات الناقصة")]
        return []

    def make_sub_productions(self, user=None):
        """أوامر إنتاج (مسودات) للمكونات نصف المصنعة الناقصة بكمية العجز."""
        created = []
        for a in self.availability:
            bom = default_bom(a["row"].item)
            if a["short"] <= 0 or bom is None:
                continue
            sub = Production.objects.create(company=self.company, bom=bom, qty=a["short"],
                                            source_warehouse=self.source_warehouse,
                                            target_warehouse=self.source_warehouse, created_by=user,
                                            remarks=f"مكوّن لأمر الإنتاج {self.name}")
            sub.calculate_totals()
            sub.save()
            created.append(sub)
        if not created:
            raise ValidationError("لا توجد مكونات نصف مصنعة ناقصة.")
        return created[0]

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


# ---------------- إعادة التعبئة والتفكيك ----------------
# صنف واحد بالجملة (مثل فلينة طماطم كبيرة) يُفكك إلى عدة منتجات (فلين وسط، فلين صغير، صحون)، ولكل منتج تغليفه.
# تكلفة المادة المفككة (والتكاليف الإضافية) تُوزع على المنتجات، وتكلفة تغليف كل منتج تُحمّل عليه وحده.

class RepackRowType(models.TextChoices):
    INPUT = "input", "مادة تُفكك"
    OUTPUT = "output", "منتج ناتج"
    PACKAGING = "packaging", "تغليف لمنتج"


class CostBasis(models.TextChoices):
    SELLING = "selling", "بنسبة قيمة البيع (موصى به)"
    MANUAL = "manual", "بنسب يدوية لكل منتج"
    QTY = "qty", "بالتساوي على عدد الوحدات"


class RepackTemplate(TenantModel):
    """طريقة تفكيك ثابتة تُعاد كل مرة: ماذا ينتج عن الصنف وما يلزم كل منتج من تغليف."""

    name = models.CharField("اسم القالب", max_length=100)
    source_item = models.ForeignKey("stock.Item", models.PROTECT, related_name="+", verbose_name="الصنف الذي يُفكك",
                                    limit_choices_to={"disabled": False, "is_stock_item": True})
    source_qty = models.DecimalField("لكل كمية", max_digits=18, decimal_places=3, default=1,
                                     help_text="الكميات أدناه ناتجة عن هذه الكمية من الصنف (مثلاً 1 فلينة كبيرة)")
    cost_basis = models.CharField("توزيع التكلفة", max_length=10, choices=CostBasis.choices, default=CostBasis.SELLING)
    is_active = models.BooleanField("فعّال", default=True)
    remarks = models.TextField("ملاحظات", blank=True)

    class Meta:
        verbose_name = "قالب إعادة تعبئة"
        verbose_name_plural = "قوالب إعادة التعبئة"
        ordering = ["name"]
        constraints = [tenant_unique("name", model_label="repacktemplate")]

    def __str__(self):
        return self.name


class RepackLine(models.Model):
    """حقول مشتركة لأسطر القالب والمستند."""

    item = models.ForeignKey("stock.Item", models.PROTECT, related_name="+", verbose_name="الصنف",
                             limit_choices_to={"disabled": False, "is_stock_item": True})
    qty = models.DecimalField("الكمية", max_digits=18, decimal_places=3, default=1)
    for_item = models.ForeignKey("stock.Item", models.PROTECT, null=True, blank=True, related_name="+",
                                 verbose_name="لأي منتج", limit_choices_to={"disabled": False},
                                 help_text="لأسطر التغليف: المنتج الذي يُغلّف به")
    cost_percent = models.DecimalField("نسبة التكلفة %", max_digits=6, decimal_places=2, default=0,
                                       help_text="للمنتجات عند التوزيع بنسب يدوية فقط")

    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        if self.row_type == RepackRowType.PACKAGING and not self.for_item_id:
            raise ValidationError({"for_item": "حدد المنتج الذي يُغلّف بهذا الصنف."})
        if self.row_type != RepackRowType.PACKAGING:
            self.for_item = None


class RepackTemplateItem(RepackLine):
    parent = models.ForeignKey(RepackTemplate, models.CASCADE, related_name="items")
    row_type = models.CharField("النوع", max_length=10, default=RepackRowType.OUTPUT,
                                choices=[(RepackRowType.OUTPUT, RepackRowType.OUTPUT.label),
                                         (RepackRowType.PACKAGING, RepackRowType.PACKAGING.label)])

    class Meta:
        verbose_name = "منتج أو تغليف"
        verbose_name_plural = "المنتجات والتغليف"


class Repack(SubmittableDocument):
    """إعادة تعبئة / تفكيك: يصرف الصنف وتغليفه، ويُدخل المنتجات بتكلفة موزعة."""

    naming_prefix = "RPK"

    template = models.ForeignKey(RepackTemplate, models.PROTECT, null=True, blank=True, related_name="repacks",
                                 verbose_name="القالب", limit_choices_to={"is_active": True},
                                 help_text="اختره واترك جدول الأصناف فارغاً واحفظ لتعبئته تلقائياً")
    source_qty = models.DecimalField("الكمية المفككة", max_digits=18, decimal_places=3, default=1,
                                     help_text="عدد الوحدات التي تُفكك (لتعبئة الجدول من القالب)")
    source_warehouse = models.ForeignKey("stock.Warehouse", models.PROTECT, related_name="+",
                                         verbose_name="مستودع الصنف والتغليف")
    target_warehouse = models.ForeignKey("stock.Warehouse", models.PROTECT, related_name="+",
                                         verbose_name="مستودع المنتجات")
    cost_basis = models.CharField("توزيع التكلفة", max_length=10, choices=CostBasis.choices, default=CostBasis.SELLING)
    additional_cost = models.DecimalField("تكاليف إضافية", max_digits=18, decimal_places=2, default=0,
                                          help_text="عمالة الفرز والتعبئة ونحوها؛ تُوزع مع تكلفة الصنف على المنتجات")
    input_cost = models.DecimalField("تكلفة الصنف المفكك", max_digits=18, decimal_places=2, default=0, editable=False)
    packaging_cost = models.DecimalField("تكلفة التغليف", max_digits=18, decimal_places=2, default=0, editable=False)
    total_cost = models.DecimalField("إجمالي تكلفة المنتجات", max_digits=18, decimal_places=2, default=0,
                                     editable=False)

    class Meta(SubmittableDocument.Meta):
        verbose_name = "إعادة تعبئة / تفكيك"
        verbose_name_plural = "إعادة التعبئة والتفكيك"

    def rows(self, row_type):
        return [r for r in self.items.select_related("item", "for_item").order_by("id") if r.row_type == row_type]

    def fill_from_template(self):
        """أسطر الصنف ومنتجاته وتغليفها من القالب بحسب الكمية المفككة."""
        template = self.template
        factor = Decimal(self.source_qty or 0) / Decimal(template.source_qty or 1)
        self.cost_basis = template.cost_basis  # يمكن تغييره بعد التعبئة
        self.items.create(row_type=RepackRowType.INPUT, item=template.source_item, qty=flt(self.source_qty, 3))
        for row in template.items.order_by("id"):
            self.items.create(row_type=row.row_type, item_id=row.item_id, qty=flt(row.qty * factor, 3),
                              for_item_id=row.for_item_id, cost_percent=row.cost_percent)

    def calculate_totals(self):
        if not self.pk:
            return
        if self.is_draft:
            if self.template_id and not self.items.exists():
                self.fill_from_template()
            # تقدير قبل الاعتماد؛ عند الاعتماد تُحسب بالتكلفة الفعلية لما صُرف
            costs = {}
            for row in self.items.select_related("item"):
                if row.row_type == RepackRowType.OUTPUT:
                    continue
                row.rate = unit_cost_estimate(row.item, self.source_warehouse if self.source_warehouse_id else None)
                row.amount = flt(row.rate * row.qty)
                row.save(update_fields=["rate", "amount"])
                costs[row.pk] = row.amount
            self._allocate(*self._consumed(costs), save_rows=True)

    def _consumed(self, costs):
        """(تكلفة الصنف المفكك، تكلفة التغليف لكل منتج) من تكلفة كل سطر."""
        input_cost, packaging = flt(0), {}
        for row in self.items.all():
            if row.row_type == RepackRowType.INPUT:
                input_cost += costs.get(row.pk, flt(0))
            elif row.row_type == RepackRowType.PACKAGING:
                packaging[row.for_item_id] = packaging.get(row.for_item_id, flt(0)) + costs.get(row.pk, flt(0))
        return input_cost, packaging

    def _allocate(self, input_cost, packaging, save_rows):
        """توزيع تكلفة الصنف والتكاليف الإضافية على المنتجات + تغليف كل منتج عليه. يعيد {سطر: تكلفته}."""
        from core.utils import allocate

        outputs = self.rows(RepackRowType.OUTPUT)
        if self.cost_basis == CostBasis.MANUAL:
            weights = {r.pk: flt(r.cost_percent) for r in outputs}
        elif self.cost_basis == CostBasis.SELLING:
            weights = {r.pk: flt(r.qty * r.item.selling_rate) for r in outputs}
        else:
            weights = {}
        if not any(weights.values()):
            weights = {r.pk: flt(r.qty, 3) for r in outputs}  # بلا أسعار بيع: بالتساوي على الوحدات
        shared = flt(input_cost + flt(self.additional_cost))
        shares = allocate(shared, weights) if outputs else {}
        result = {}
        for row in outputs:
            row.amount = flt(shares.get(row.pk, 0) + packaging.get(row.item_id, flt(0)))
            row.rate = flt(row.amount / row.qty, 4) if row.qty else flt(0, 4)
            result[row.pk] = row
            if save_rows:
                row.save(update_fields=["rate", "amount"])
        self.input_cost = input_cost
        self.packaging_cost = sum(packaging.values(), flt(0))
        self.total_cost = flt(self.input_cost + self.packaging_cost + flt(self.additional_cost))
        return result

    def validate_for_submit(self):
        rows = list(self.items.select_related("item"))
        inputs = [r for r in rows if r.row_type == RepackRowType.INPUT]
        outputs = [r for r in rows if r.row_type == RepackRowType.OUTPUT]
        if not inputs:
            raise ValidationError("أضف الصنف الذي يُفكك (سطر من نوع «مادة تُفكك»).")
        if not outputs:
            raise ValidationError("أضف منتجاً ناتجاً واحداً على الأقل.")
        if self.additional_cost < 0:
            raise ValidationError("التكاليف الإضافية لا تكون سالبة.")
        for row in rows:
            if row.qty <= 0:
                raise ValidationError(f"الكمية لـ «{row.item.name}» يجب أن تكون أكبر من صفر.")
            if not row.item.is_stock_item:
                raise ValidationError(f"«{row.item.name}» ليس صنفاً مخزنياً.")
        output_items = [r.item_id for r in outputs]
        if len(set(output_items)) != len(output_items):
            raise ValidationError("المنتج الواحد يُكتب في سطر واحد؛ اجمع كمياته.")
        if set(output_items) & {r.item_id for r in inputs}:
            raise ValidationError("المنتج لا يكون هو نفسه الصنف المفكك.")
        for row in rows:
            if row.row_type == RepackRowType.PACKAGING and row.for_item_id not in output_items:
                raise ValidationError(f"تغليف «{row.item.name}» مرتبط بمنتج غير موجود في المنتجات الناتجة.")
        if self.cost_basis == CostBasis.MANUAL and sum(flt(r.cost_percent) for r in outputs) != 100:
            raise ValidationError("مجموع نسب التكلفة للمنتجات يجب أن يساوي 100%.")

    def on_submit(self):
        costs = {}
        for row in self.items.select_related("item").order_by("id"):
            if row.row_type == RepackRowType.OUTPUT:
                continue
            cost = -make_stock_ledger_entry(self, row.item, self.source_warehouse, -row.qty, batch_no=row.batch_no)
            row.rate = flt(cost / row.qty, 4)
            row.amount = cost
            row.save(update_fields=["rate", "amount"])
            costs[row.pk] = cost
        input_cost, packaging = self._consumed(costs)
        consumed = flt(input_cost + sum(packaging.values(), flt(0)))
        value = flt(0)
        for row in self._allocate(input_cost, packaging, save_rows=True).values():
            value += make_stock_ledger_entry(self, row.item, self.target_warehouse, row.qty, row.amount / row.qty,
                                             batch_no=row.batch_no, manufacturing_date=self.posting_date)
        self.save(update_fields=["input_cost", "packaging_cost", "total_cost"])
        overhead = flt(self.additional_cost)
        stock = self.company.require_account("default_stock_account")
        # المخزون الخارج (الصنف والتغليف) دخل في قيمة المنتجات مع التكاليف الإضافية
        entries = [{"account": stock, "debit": value}, {"account": stock, "credit": consumed}]
        rounding = flt(value - consumed - overhead)
        if overhead:
            entries.append({"account": overhead_account(self.company), "credit": overhead})
        if rounding:
            entries.append({"account": self.company.require_account("default_stock_adjustment_account"),
                            "credit": rounding})
        if overhead or rounding:
            make_gl_entries(self, entries, remarks=self.remarks or "إعادة تعبئة / تفكيك")

    def on_cancel(self):
        cancel_stock_ledger_entries(self)
        cancel_gl_entries(self)

    @property
    def output_summary(self):
        """لكل منتج: الكمية والتكلفة وتكلفة الوحدة وسعر البيع وهامش الربح."""
        summary = []
        for row in self.rows(RepackRowType.OUTPUT):
            price = flt(row.item.selling_rate)
            margin = flt((price - row.rate) * 100 / price) if price else None
            summary.append({"row": row, "price": price, "margin": margin})
        return summary


class RepackItem(RepackLine):
    parent = models.ForeignKey(Repack, models.CASCADE, related_name="items")
    row_type = models.CharField("النوع", max_length=10, choices=RepackRowType.choices, default=RepackRowType.OUTPUT)
    batch_no = models.CharField("التشغيلة", max_length=40, blank=True,
                                help_text="للأصناف المتتبعة؛ فارغ = تلقائي")
    rate = models.DecimalField("تكلفة الوحدة", max_digits=18, decimal_places=4, default=0, editable=False)
    amount = models.DecimalField("التكلفة", max_digits=18, decimal_places=2, default=0, editable=False)

    class Meta:
        verbose_name = "صنف إعادة تعبئة"
        verbose_name_plural = "الأصناف"
