#!/usr/bin/env bash
# تشغيل سِينار للتجربة بأمر واحد (Linux / macOS / GitHub Codespaces)
set -e
cd "$(dirname "$0")"

PYTHON=python3
command -v $PYTHON >/dev/null 2>&1 || PYTHON=python
command -v $PYTHON >/dev/null 2>&1 || { echo "Python غير مثبت. حمّله من https://www.python.org/downloads/"; exit 1; }

if [ ! -d .venv ]; then
  echo "== تجهيز البيئة لأول مرة..."
  $PYTHON -m venv .venv
fi
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

FIRST_RUN=0
[ -f db.sqlite3 ] || FIRST_RUN=1
python manage.py migrate -v 0
python manage.py createcachetable
if [ "$FIRST_RUN" = "1" ]; then
  echo "== إنشاء شركة تجريبية ببيانات جاهزة..."
  python manage.py setup_erp --demo --admin admin --password admin12345 --tax-id 310122393500003
fi

echo ""
echo "======================================================"
echo "  سِينار يعمل الآن:  http://127.0.0.1:8000"
echo "  اسم المستخدم: admin    كلمة المرور: admin12345"
echo "  للإيقاف اضغط Ctrl+C"
echo "======================================================"
python manage.py runserver 0.0.0.0:8000
