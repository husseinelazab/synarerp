"""مشغّل الاختبارات: مع SYNAR_TEST_RLS=1 (على PostgreSQL) تعمل الاختبارات كلها بالدور المقيد كما يعمل الخادم،
فيُكتشف أي مسار يعتمد على تجاوز عزل الشركات في قاعدة البيانات."""
import os
import time

from django.db import connections
from django.test.runner import DiscoverRunner
from django.utils import timezone

# كثير من الاختبارات تحسب «اليوم» عند تحميلها (TODAY = timezone.localdate())؛ لو عبر التشغيل منتصف الليل
# بتوقيت النظام اختلف «اليوم» بين التحميل والتنفيذ ففشلت دون خطأ حقيقي. فإن بدأ التشغيل قبل منتصف الليل بأقل
# من هذه المدة ينتظر حتى يبدأ اليوم الجديد (التشغيل الكامل يستغرق دقائق).
MIDNIGHT_MARGIN_MINUTES = int(os.environ.get("SYNAR_TEST_MIDNIGHT_MARGIN", "30"))


def wait_past_midnight(margin_minutes=MIDNIGHT_MARGIN_MINUTES, sleep=time.sleep):
    now = timezone.localtime()
    seconds_left = ((23 - now.hour) * 60 + (59 - now.minute)) * 60 + (60 - now.second)
    if 0 < seconds_left <= margin_minutes * 60:
        print(f"اقترب منتصف الليل ({now:%H:%M} بتوقيت النظام): الانتظار {seconds_left // 60 + 1} دقيقة حتى يثبت تاريخ اليوم.",
              flush=True)
        sleep(seconds_left + 5)
        return True
    return False


class SynarTestRunner(DiscoverRunner):
    def run_tests(self, test_labels, **kwargs):
        wait_past_midnight()
        return super().run_tests(test_labels, **kwargs)

    def setup_databases(self, **kwargs):
        config = super().setup_databases(**kwargs)
        if os.environ.get("SYNAR_TEST_RLS") == "1":
            from core import rls

            rls.RUNTIME = True  # الاتصالات الجديدة (ومنها اتصالات العمليات المتوازية) تبدأ بالدور المقيد
            for connection in connections.all():
                if rls.enabled(connection):
                    rls.activate_role(connection)
        return config

    def teardown_databases(self, old_config, **kwargs):
        from core import rls

        if rls.RUNTIME:
            rls.RUNTIME = False
            for connection in connections.all():
                if rls.enabled(connection):
                    with connection.cursor() as cursor:
                        cursor.execute("RESET ROLE")  # حذف قاعدة الاختبار يحتاج المالك
        super().teardown_databases(old_config, **kwargs)
