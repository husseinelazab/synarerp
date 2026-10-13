from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import Company
from core.provisioning import provision_company
from saas.services import ensure_default_plans


class Command(BaseCommand):
    help = "إعداد المنصة: الباقات، مستخدم مدير المنصة، وشركة (مع بيانات تجريبية اختيارياً)."

    def add_arguments(self, parser):
        parser.add_argument("--company", default="شركتي التجارية", help="اسم الشركة")
        parser.add_argument("--abbr", default="ش ت", help="اختصار الشركة")
        parser.add_argument("--tax-id", default="", help="الرقم الضريبي")
        parser.add_argument("--nonprofit", action="store_true", help="تأسيس الشركة كجهة غير ربحية (جمعية)")
        parser.add_argument("--no-company", action="store_true", help="إعداد المنصة فقط دون إنشاء شركة")
        parser.add_argument("--demo", action="store_true", help="إضافة بيانات تجريبية (عملاء، موردون، أصناف، فواتير)")
        parser.add_argument("--admin", default="", help="إنشاء مستخدم مدير المنصة بهذا الاسم (ويكون مالك الشركة)")
        parser.add_argument("--password", default="admin12345", help="كلمة مرور المستخدم المدير")

    @transaction.atomic
    def handle(self, *args, **opts):
        ensure_default_plans()
        owner = None
        if opts["admin"]:
            User = get_user_model()
            owner = User.objects.filter(username=opts["admin"]).first()
            if owner is None:
                owner = User.objects.create_superuser(opts["admin"], "", opts["password"])
                self.stdout.write(self.style.SUCCESS(f"تم إنشاء مدير المنصة «{owner}»."))

        if opts["nonprofit"]:
            # أسماء افتراضية للجمعية التجريبية (فلا يحتاج سطر الأوامر نصاً عربياً)
            if opts["company"] == "شركتي التجارية":
                opts["company"] = "جمعية البر التجريبية"
            if opts["abbr"] == "ش ت":
                opts["abbr"] = "جمعية"
        if not opts["no_company"]:
            if Company.objects.filter(name=opts["company"]).exists():
                self.stdout.write(f"الشركة «{opts['company']}» موجودة مسبقاً.")
            else:
                company = provision_company(opts["company"], owner=owner, abbr=opts["abbr"], tax_id=opts["tax_id"],
                                            demo=opts["demo"],
                                            organization_type="nonprofit" if opts["nonprofit"] else "business")
                self.stdout.write(self.style.SUCCESS(f"تم تأسيس الشركة «{company}»."))
        self.stdout.write(self.style.SUCCESS("اكتمل الإعداد."))
