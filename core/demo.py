"""بيانات تجريبية لتجربة دورة العمل كاملة."""
import datetime
from decimal import Decimal

from django.utils import timezone

from accounts.models import Account, JournalEntry, PaymentEntry
from buying.models import PurchaseInvoice, Supplier
from hr.models import Department, Designation, Employee
from selling.models import Customer, SalesInvoice
from stock.models import UOM, Item, ItemGroup, Warehouse


def create_demo_data(company):
    """تُستدعى داخل tenant_context للشركة."""
    if Item.objects.exists():
        return
    today = timezone.localdate()
    piece, hour = UOM.objects.get(name="حبة"), UOM.objects.get(name="ساعة")
    products, services = ItemGroup.objects.get(name="المنتجات"), ItemGroup.objects.get(name="الخدمات")
    warehouse = Warehouse.objects.get(name="المستودع الرئيسي")

    items = [
        Item.objects.create(code="LAP-001", name="حاسب محمول", item_group=products, uom=piece, buying_rate=2500, selling_rate=3200),
        Item.objects.create(code="PRN-001", name="طابعة ليزر", item_group=products, uom=piece, buying_rate=800, selling_rate=1100),
        Item.objects.create(code="MOU-001", name="فأرة لاسلكية", item_group=products, uom=piece, buying_rate=40, selling_rate=75),
    ]
    service = Item.objects.create(
        code="SRV-001", name="خدمة صيانة", item_group=services, uom=hour, is_stock_item=False, selling_rate=150
    )

    customers = [
        Customer.objects.create(name="مؤسسة النور للتجارة", tax_id="300000000000003", city="الرياض", phone="0500000001"),
        Customer.objects.create(name="شركة الأفق للتقنية", tax_id="300000000000013", city="جدة", phone="0500000002"),
        Customer.objects.create(name="عبدالله محمد", customer_type="individual", city="الدمام"),
    ]
    supplier = Supplier.objects.create(name="شركة التوريدات الحديثة", tax_id="300000000000023", city="الرياض")

    # رأس المال
    je = JournalEntry.objects.create(posting_date=today.replace(day=1), entry_type="opening", remarks="إيداع رأس المال")
    je.accounts.create(account=company.default_cash_account, debit=Decimal("100000"))
    je.accounts.create(account=Account.objects.get(number="3101"), credit=Decimal("100000"))
    je.calculate_totals()
    je.save()
    je.submit()

    pinv = PurchaseInvoice.objects.create(supplier=supplier, bill_no="INV-7781", warehouse=warehouse,
                                          posting_date=today.replace(day=1))
    for item, qty in zip(items, (10, 5, 50)):
        pinv.items.create(item=item, qty=qty, rate=item.buying_rate)
    pinv.calculate_totals()
    pinv.save()
    pinv.submit()

    pay = PaymentEntry.objects.create(payment_type="pay", supplier=supplier, account=company.default_cash_account,
                                      amount=Decimal("20000"), purchase_invoice=pinv, posting_date=today)
    pay.submit()

    for idx, customer in enumerate(customers):
        sinv = SalesInvoice.objects.create(customer=customer, warehouse=warehouse, posting_date=today,
                                           due_date=today + datetime.timedelta(days=30))
        sinv.items.create(item=items[idx % len(items)], qty=2, rate=items[idx % len(items)].selling_rate)
        sinv.items.create(item=service, qty=1, rate=service.selling_rate)
        sinv.calculate_totals()
        sinv.save()
        sinv.submit()
        if idx == 0:
            PaymentEntry.objects.create(payment_type="receive", customer=customer, account=company.default_cash_account,
                                        amount=sinv.grand_total, sales_invoice=sinv, posting_date=today).submit()

    Employee.objects.create(
        employee_number="EMP-001", full_name="أحمد علي السالم", gender="male", nationality="سعودي",
        date_of_joining=today.replace(month=1, day=1), department=Department.objects.get(name="المالية والمحاسبة"),
        designation=Designation.objects.get(name="محاسب"), basic_salary=8000, housing_allowance=2000, transport_allowance=800,
    )
