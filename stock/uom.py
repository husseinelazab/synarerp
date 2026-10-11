"""وحدات التحويل على أسطر المستندات: الكمية بوحدة السطر، والمخزون دائماً بالوحدة الأساسية للصنف."""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models

from core.utils import flt


class UOMLine(models.Model):
    """حقول الوحدة لأسطر الفواتير والأوامر والأذون."""

    uom = models.ForeignKey("stock.UOM", models.PROTECT, null=True, blank=True, related_name="+",
                            verbose_name="الوحدة", help_text="فارغ = الوحدة الأساسية للصنف")
    conversion_factor = models.DecimalField("معامل التحويل", max_digits=18, decimal_places=6, default=1,
                                            editable=False)
    stock_qty = models.DecimalField("الكمية بالوحدة الأساسية", max_digits=18, decimal_places=3, default=0,
                                    editable=False)

    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        if self.item_id and self.uom_id:
            try:
                self.item.uom_factor(self.uom_id)
            except ValidationError as exc:
                raise ValidationError({"uom": exc.messages})

    def apply_uom(self):
        if self.item_id:
            if self.uom_id == self.item.uom_id:
                self.uom_id = None
            self.conversion_factor = self.item.uom_factor(self.uom_id)
        self.stock_qty = flt(flt(self.qty or 0, 3) * Decimal(self.conversion_factor or 1), 3)

    def save(self, *args, **kwargs):
        self.apply_uom()
        fields = kwargs.get("update_fields")
        if fields is not None:
            kwargs["update_fields"] = set(fields) | {"uom", "conversion_factor", "stock_qty"}
        super().save(*args, **kwargs)

    @property
    def uom_name(self):
        return str(self.uom) if self.uom_id else str(self.item.uom)

    @property
    def stock_rate(self):
        """السعر لكل وحدة أساسية."""
        return flt(Decimal(self.rate) / Decimal(self.conversion_factor or 1), 6)


def linked_qty(row, linked):
    """كمية السطر بوحدة السطر المرتبط (أمر/إذن) حتى لو اختلفت الوحدتان."""
    return flt(Decimal(row.stock_qty) / Decimal(linked.conversion_factor or 1), 3)
