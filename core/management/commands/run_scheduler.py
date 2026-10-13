"""مُجدول المهام الدورية (يعمل في حاوية مستقلة في الإنتاج).

- يومياً في الساعة SYNAR_REMINDERS_HOUR (افتراضي 7 صباحاً): تنبيهات الموارد البشرية (hr_reminders) وصلاحية التشغيلات (expiry_alerts)
  والشيكات المستحقة (accounts_alerts).
- كل ساعة: إعادة إرسال الفواتير الإلكترونية المعلقة — للشركات التي فعّلت الربط مع منصة فاتورة بنفسها فقط.
"""
import os
import time

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone


class Command(BaseCommand):
    help = "تشغيل المهام الدورية بشكل مستمر (التنبيهات اليومية وإعادة إرسال الفواتير الإلكترونية)"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="تشغيل كل المهام مرة واحدة ثم الخروج")

    def run(self, name, *args):
        close_old_connections()
        try:
            call_command(name, *args, stdout=self.stdout)
        except Exception as exc:  # فشل مهمة لا يوقف المجدول
            self.stderr.write(f"فشلت المهمة {name}: {exc}")

    def handle(self, *args, **options):
        reminders_hour = int(os.environ.get("SYNAR_REMINDERS_HOUR", "7"))
        if options["once"]:
            self.run("hr_reminders")
            self.run("expiry_alerts")
            self.run("send_reminders")
            self.run("reorder_alerts")
            self.run("accounts_alerts")
            self.run("run_recurring")
            self.run("crm_reminders")
            self.run("support_alerts")
            self.run("sponsorship_dues")
            self.run("waqf_alerts")
            self.run("office_alerts")
            self.run("zatca_submit")
            from core.jobs import run_pending

            run_pending()
            return
        self.stdout.write("المجدول يعمل.")
        last_daily = last_hourly = None
        from core.jobs import run_pending

        while True:
            close_old_connections()
            try:
                run_pending()
            except Exception as exc:  # خطأ في الطابور لا يوقف المهام الدورية
                self.stderr.write(f"فشل تنفيذ المهام الخلفية: {exc}")
            now = timezone.localtime()
            hour_key = now.strftime("%Y%m%d%H")
            if hour_key != last_hourly:
                last_hourly = hour_key
                self.run("zatca_submit")
            if now.hour >= reminders_hour and now.date() != last_daily:
                last_daily = now.date()
                self.run("hr_reminders")
                self.run("expiry_alerts")
                self.run("send_reminders")
                self.run("reorder_alerts")
                self.run("accounts_alerts")
                self.run("run_recurring")
                self.run("crm_reminders")
                self.run("support_alerts")
                self.run("sponsorship_dues")
                self.run("waqf_alerts")
                self.run("office_alerts")
            time.sleep(int(os.environ.get("SYNAR_SCHEDULER_TICK", "5")))
