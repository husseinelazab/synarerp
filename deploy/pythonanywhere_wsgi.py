"""ملف WSGI لتشغيل سِينار على PythonAnywhere (انظر DEPLOY_PYTHONANYWHERE.md).

انسخ محتواه إلى ملف WSGI الذي يفتحه لك تبويب Web، وغيّر PROJECT_DIR إن اختلف مسار المشروع.
يقرأ الإعدادات من ملف .env داخل مجلد المشروع (السطر بصيغة KEY=value)، لأن PythonAnywhere لا يمرّر متغيرات البيئة.
"""
import os
import sys
from pathlib import Path

PROJECT_DIR = Path.home() / "HAMTerp"

env_file = PROJECT_DIR / ".env"
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))

if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "synar.settings")

from django.core.wsgi import get_wsgi_application  # noqa: E402

application = get_wsgi_application()
