import datetime

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounts.models import Account
from core.models import Company, Currency, FiscalYear
from core.setup_data import CHART_OF_ACCOUNTS, DEFAULT_ACCOUNTS, DEPARTMENTS, DESIGNATIONS, ITEM_GROUPS, UOMS
from hr.models import Department, Designation
from stock.models import UOM, ItemGroup, Warehouse


class Command(BaseCommand):
    help = "إعداد النظام: الشركة، دليل الحسابات، المستودعات، والبيانات الأساسية (مع بيانات تجريبية اختيارياً)."

    def add_arguments(self, parser):
        parser.add_argument("--company", default="شركتي التجارية", help="اسم الشركة")
        parser.add_argument("--abbr", default="ش ت", help="اختصار الشركة")
        parser.add_argument("--tax-id", default="", help="الرقم الضريبي")
        parser.add_argument("--demo", action="store_true", help="إضافة بيانات تجريبية (عملاء، موردون، أصناف، فواتير)")
        parser.add_argument("--admin", default="", help="إنشاء مستخدم مدير بهذا الاسم (كلمة المرور تساوي --password)")
        parser.add_argument("--password", default="admin12345", help="كلمة مرور المستخدم المدير")

    @transaction.atomic
    def handle(self, *args, **opts):
        currency, _ = Currency.objects.get_or_create(code="SAR", defaults={"name": "ريال سعودي", "symbol": "ر.س"})
        company, created = Company.objects.get_or_create(
            name=opts["company"], defaults={"abbr": opts["abbr"], "currency": currency, "tax_id": opts["tax_id"]}
        )
        if created:
            self._create_accounts(company, CHART_OF_ACCOUNTS, None, None)
            accounts = {a.number: a for a in company.accounts.all()}
            for field, number in DEFAULT_ACCOUNTS.items():
                setattr(company, field, accounts[number])
            company.save()
            self.stdout.write(self.style.SUCCESS(f"تم إنشاء الشركة «{company}» ودليل الحسابات."))
        else:
            self.stdout.write(f"الشركة «{company}» موجودة مسبقاً.")

        year = timezone.localdate().year
        FiscalYear.objects.get_or_create(
            name=str(year), defaults={"start_date": datetime.date(year, 1, 1), "end_date": datetime.date(year, 12, 31)}
        )
        for name in UOMS:
            UOM.objects.get_or_create(name=name)
        for name in ITEM_GROUPS:
            ItemGroup.objects.get_or_create(name=name)
        for name in ("المستودع الرئيسي", "مستودع الفرع"):
            Warehouse.objects.get_or_create(name=name, defaults={"company": company})
        for name in DEPARTMENTS:
            Department.objects.get_or_create(name=name)
        for name in DESIGNATIONS:
            Designation.objects.get_or_create(name=name)

        if opts["admin"]:
            User = get_user_model()
            if not User.objects.filter(username=opts["admin"]).exists():
                User.objects.create_superuser(opts["admin"], "", opts["password"])
                self.stdout.write(self.style.SUCCESS(f"تم إنشاء المستخدم المدير «{opts['admin']}»."))

        if opts["demo"]:
            from core.demo import create_demo_data

            create_demo_data(company)
            self.stdout.write(self.style.SUCCESS("تمت إضافة البيانات التجريبية."))
        self.stdout.write(self.style.SUCCESS("اكتمل الإعداد."))

    def _create_accounts(self, company, nodes, parent, root_type):
        for number, name, kind, children in nodes:
            is_root = parent is None
            node_root = kind if is_root else root_type
            account = Account.objects.create(
                company=company, number=number, name=name, parent=parent,
                is_group=children is not None, root_type=node_root,
                account_type="" if (is_root or children is not None) else (kind or ""),
            )
            if children:
                self._create_accounts(company, children, account, node_root)
