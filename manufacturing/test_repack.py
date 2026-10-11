from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.test import TestCase
from django.urls import reverse

from accounts.models import GLEntry
from buying.models import PurchaseInvoice, Supplier
from core.models import DocStatus
from core.tenancy import tenant_context
from manufacturing.models import OVERHEAD_ACCOUNT, CostBasis, Repack, RepackRowType, RepackTemplate
from saas.tests import make_company
from stock.models import UOM, Bin, Item, ItemGroup, Warehouse

OUT, PACK, INP = RepackRowType.OUTPUT, RepackRowType.PACKAGING, RepackRowType.INPUT


class RepackTests(TestCase):
    """فلينة طماطم كبيرة تُفكك إلى فلين وسط وفلين صغير وصحون، ولكل منها تغليفه."""

    @classmethod
    def setUpTestData(cls):
        cls.company, cls.owner = make_company("خضار الجملة", "veggies")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.client.force_login(self.owner)
        group, uom = ItemGroup.objects.first(), UOM.objects.first()
        make = lambda code, name, price=0: Item.objects.create(  # noqa: E731
            code=code, name=name, item_group=group, uom=uom, selling_rate=price)
        self.crate = make("TOM-L", "طماطم فلين كبير")
        self.medium = make("TOM-M", "طماطم فلين وسط", 20)
        self.small = make("TOM-S", "طماطم فلين صغير", 8)
        self.plate = make("TOM-P", "طماطم صحن", 5)
        self.foam_m, self.foam_s = make("PK-FM", "فلين وسط فاضي"), make("PK-FS", "فلين صغير فاضي")
        self.tray, self.wrap = make("PK-TR", "صحن فاضي"), make("PK-WR", "نايلون تغليف")
        self.wh = Warehouse.objects.filter(disabled=False).first()
        self.buy({self.crate: (10, 50), self.foam_m: (100, 1), self.foam_s: (100, "0.5"),
                  self.tray: (100, "0.3"), self.wrap: (100, "0.1")})
        self.template = RepackTemplate.objects.create(name="تفكيك طماطم", source_item=self.crate, source_qty=1)
        for row_type, item, qty, for_item in [
            (OUT, self.medium, 2, None), (OUT, self.small, 3, None), (OUT, self.plate, 4, None),
            (PACK, self.foam_m, 2, self.medium), (PACK, self.wrap, 2, self.medium),
            (PACK, self.foam_s, 3, self.small),
            (PACK, self.tray, 4, self.plate), (PACK, self.wrap, 4, self.plate),
        ]:
            self.template.items.create(row_type=row_type, item=item, qty=qty, for_item=for_item)

    def buy(self, lines):
        inv = PurchaseInvoice.objects.create(supplier=Supplier.objects.create(name="مورد الخضار"), warehouse=self.wh)
        for item, (qty, rate) in lines.items():
            inv.items.create(item=item, qty=qty, rate=Decimal(str(rate)))
        inv.calculate_totals()
        inv.save()
        inv.submit()

    def repack(self, source_qty=2, **kw):
        doc = Repack.objects.create(template=self.template, source_qty=source_qty, source_warehouse=self.wh,
                                    target_warehouse=self.wh, **kw)
        doc.calculate_totals()
        doc.save()
        return doc

    def qty(self, item):
        b = Bin.objects.filter(item=item, warehouse=self.wh).first()
        return b.actual_qty if b else 0

    def output(self, doc, item):
        return doc.items.get(row_type=OUT, item=item)

    def test_fills_from_template_scaled(self):
        doc = self.repack(source_qty=2)
        rows = {(r.row_type, r.item_id, r.for_item_id): r.qty for r in doc.items.all()}
        self.assertEqual(rows[(INP, self.crate.pk, None)], 2)
        self.assertEqual(rows[(OUT, self.medium.pk, None)], 4)
        self.assertEqual(rows[(PACK, self.wrap.pk, self.plate.pk)], 8)
        self.assertEqual(doc.items.count(), 9)

    def test_cost_split_by_selling_value_and_packaging_on_its_product(self):
        doc = self.repack(source_qty=2, additional_cost=10)
        doc.submit()
        # الصنف 2×50=100 + إضافية 10 = 110 تُوزع بقيمة البيع: وسط 4×20=80، صغير 6×8=48، صحن 8×5=40 (المجموع 168)
        # التغليف على منتجه فقط: وسط 4×1+4×0.1=4.4، صغير 6×0.5=3، صحن 8×0.3+8×0.1=3.2
        self.assertEqual(self.output(doc, self.medium).amount, Decimal("56.78"))   # 52.38 + 4.40
        self.assertEqual(self.output(doc, self.small).amount, Decimal("34.43"))    # 31.43 + 3.00
        self.assertEqual(self.output(doc, self.plate).amount, Decimal("29.39"))    # 26.19 + 3.20
        doc.refresh_from_db()
        self.assertEqual((doc.input_cost, doc.packaging_cost, doc.total_cost),
                         (Decimal("100.00"), Decimal("10.60"), Decimal("120.60")))
        self.assertEqual(self.output(doc, self.medium).rate, Decimal("14.1950"))
        # المخزون: خرج الصنف وتغليفه، ودخلت المنتجات
        self.assertEqual((self.qty(self.crate), self.qty(self.medium), self.qty(self.small), self.qty(self.plate)),
                         (8, 4, 6, 8))
        self.assertEqual((self.qty(self.foam_m), self.qty(self.wrap), self.qty(self.tray)), (96, 88, 92))
        medium_bin = Bin.objects.get(item=self.medium, warehouse=self.wh)
        self.assertEqual(medium_bin.stock_value, Decimal("56.78"))
        # القيود: التكاليف الإضافية فقط تُحمّل على المخزون (من حساب التكاليف المحملة)، والقيد متوازن
        entries = GLEntry.objects.filter(voucher_type="repack", voucher_id=doc.pk)
        totals = entries.aggregate(d=Sum("debit"), c=Sum("credit"))
        self.assertEqual(totals["d"], totals["c"])
        self.assertEqual(entries.get(account__name=OVERHEAD_ACCOUNT).credit, Decimal("10.00"))

    def test_without_extra_cost_value_is_conserved_and_cancel_reverses(self):
        doc = self.repack()
        doc.submit()
        doc.refresh_from_db()
        self.assertEqual(doc.total_cost, Decimal("110.60"))
        self.assertFalse(GLEntry.objects.filter(voucher_type="repack", voucher_id=doc.pk, is_cancelled=False)
                         .exclude(debit=0, credit=0).exclude(account=self.company.default_stock_account).exists())
        doc.cancel()
        self.assertEqual((self.qty(self.crate), self.qty(self.medium), self.qty(self.wrap)), (10, 0, 100))

    def test_actual_counts_can_be_edited_before_submit(self):
        doc = self.repack(source_qty=1)
        self.output(doc, self.small).delete()                     # لم يخرج فلين صغير هذه المرة
        doc.items.filter(row_type=PACK, for_item=self.small).delete()
        doc.items.filter(row_type=OUT, item=self.plate).update(qty=6)            # وخرجت صحون أكثر
        doc.calculate_totals()
        doc.save()
        doc.submit()
        self.assertEqual((self.qty(self.small), self.qty(self.plate)), (0, 6))
        doc.refresh_from_db()
        self.assertEqual(doc.input_cost, Decimal("50.00"))

    def test_manual_percentages_and_quantity_basis(self):
        doc = self.repack(source_qty=1, cost_basis=CostBasis.MANUAL)
        doc.cost_basis = CostBasis.MANUAL  # التعبئة من القالب تأخذ طريقة القالب؛ المستخدم يغيرها بعدها
        doc.save()
        for item, percent in ((self.medium, 50), (self.small, 30), (self.plate, 10)):
            doc.items.filter(row_type=OUT, item=item).update(cost_percent=percent)
        with self.assertRaisesMessage(ValidationError, "100%"):
            doc.submit()
        doc.items.filter(row_type=OUT, item=self.plate).update(cost_percent=20)
        doc.calculate_totals()
        doc.save()
        doc.submit()
        self.assertEqual(self.output(doc, self.medium).amount, Decimal("27.20"))  # 50% من 50 + تغليف 2.2

        doc = self.repack(source_qty=1)
        doc.cost_basis = CostBasis.QTY
        doc.save()
        doc.calculate_totals()
        doc.save()
        doc.submit()
        # بالتساوي على 9 وحدات: الوحدة 50/9، والوسط وحدتان
        self.assertEqual(self.output(doc, self.medium).amount, Decimal("13.31"))  # 11.11 + 2.20

    def test_validation(self):
        doc = self.repack(source_qty=1)
        doc.items.filter(row_type=OUT, item=self.medium).delete()
        with self.assertRaisesMessage(ValidationError, "مرتبط بمنتج غير موجود"):
            doc.submit()
        doc.items.filter(row_type=INP).delete()
        with self.assertRaisesMessage(ValidationError, "الصنف الذي يُفكك"):
            doc.submit()
        doc.items.create(row_type=INP, item=self.small, qty=1)
        doc.items.filter(row_type=PACK).delete()
        with self.assertRaisesMessage(ValidationError, "نفسه الصنف المفكك"):
            doc.submit()
        doc.refresh_from_db()
        self.assertEqual(doc.docstatus, DocStatus.DRAFT)
        self.assertEqual(self.qty(self.crate), 10)

    def test_shortage_blocks_submit(self):
        doc = self.repack(source_qty=11)
        with self.assertRaises(ValidationError):
            doc.submit()
        self.assertEqual(self.qty(self.crate), 10)

    def test_through_the_web_form(self):
        response = self.client.post(reverse("doc_new", args=["repack"]), {
            "template": self.template.pk, "source_qty": "3", "posting_date": "2026-10-11",
            "source_warehouse": self.wh.pk, "target_warehouse": self.wh.pk, "cost_basis": "selling",
            "additional_cost": "0", "remarks": "",
            "items-TOTAL_FORMS": "0", "items-INITIAL_FORMS": "0", "items-MIN_NUM_FORMS": "0",
            "items-MAX_NUM_FORMS": "1000"})
        doc = Repack.objects.get()
        self.assertRedirects(response, reverse("doc_detail", args=["repack", doc.pk]), fetch_redirect_response=False)
        self.assertEqual(self.output(doc, self.medium).qty, 6)
        page = self.client.get(reverse("doc_detail", args=["repack", doc.pk]))
        self.assertContains(page, "تكلفة المنتجات")
        self.assertContains(page, "طماطم فلين وسط")
        self.client.post(reverse("doc_action", args=["repack", doc.pk, "submit"]))
        doc.refresh_from_db()
        self.assertEqual(doc.docstatus, DocStatus.SUBMITTED)
        self.assertEqual(self.qty(self.medium), 6)
        self.assertContains(self.client.get(reverse("doc_new", args=["repack-template"])), "المنتجات والتغليف")
