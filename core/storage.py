"""تخزين الملفات الثابتة في وضع التطوير: يُضاف للرابط رقم نسخة من وقت تعديل الملف (?v=)
حتى لا يعرض المتصفح نسخة قديمة من CSS/JS بعد التحديث (في الإنتاج تتولى WhiteNoise تجزئة الأسماء)."""
import os

from django.contrib.staticfiles import finders
from django.contrib.staticfiles.storage import StaticFilesStorage


class VersionedStaticFilesStorage(StaticFilesStorage):
    def url(self, name):
        url = super().url(name)
        path = finders.find(name) if name else None
        if isinstance(path, (list, tuple)):
            path = path[0] if path else None
        if not path:
            return url
        try:
            version = int(os.path.getmtime(path))
        except OSError:
            return url
        return f"{url}{'&' if '?' in url else '?'}v={version}"
