"""مشغّل الاختبارات: مع SYNAR_TEST_RLS=1 (على PostgreSQL) تعمل الاختبارات كلها بالدور المقيد كما يعمل الخادم،
فيُكتشف أي مسار يعتمد على تجاوز عزل الشركات في قاعدة البيانات."""
import os

from django.db import connections
from django.test.runner import DiscoverRunner


class SynarTestRunner(DiscoverRunner):
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
                    # Forked workers may have invalidated the parent's libpq session.
                    # Reconnect as the owner rather than issuing SQL on that inherited socket.
                    connection.close()
        super().teardown_databases(old_config, **kwargs)
