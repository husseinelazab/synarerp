#!/bin/sh
# نقطة تشغيل الحاوية: web (الخادم) أو scheduler (المهام الدورية) أو أي أمر manage.py
set -e

wait_for_db() {
  if [ -n "$SYNAR_DB_HOST" ]; then
    echo "انتظار قاعدة البيانات $SYNAR_DB_HOST..."
    python - <<'PY'
import os, time
import psycopg
for attempt in range(90):
    try:
        psycopg.connect(host=os.environ["SYNAR_DB_HOST"], port=os.environ.get("SYNAR_DB_PORT", "5432"),
                        dbname=os.environ.get("SYNAR_DB_NAME"), user=os.environ.get("SYNAR_DB_USER"),
                        password=os.environ.get("SYNAR_DB_PASSWORD"), connect_timeout=3).close()
        break
    except psycopg.OperationalError:
        time.sleep(2)
else:
    raise SystemExit("تعذر الاتصال بقاعدة البيانات")
PY
  fi
}

case "$1" in
  web)
    wait_for_db
    python manage.py migrate --noinput
    # أول تشغيل: الباقات ومدير المنصة (بدون شركة؛ الشركات تسجّل نفسها أو يضيفها المدير)
    if [ -n "$SYNAR_ADMIN_USER" ] && [ -n "$SYNAR_ADMIN_PASSWORD" ]; then
      python manage.py setup_erp --no-company --admin "$SYNAR_ADMIN_USER" --password "$SYNAR_ADMIN_PASSWORD"
    fi
    exec gunicorn synar.wsgi:application --bind 0.0.0.0:8000 \
      --workers "${SYNAR_WORKERS:-3}" --threads "${SYNAR_THREADS:-2}" --timeout 120 \
      --access-logfile - --error-logfile - --forwarded-allow-ips "*"
    ;;
  scheduler)
    wait_for_db
    exec python manage.py run_scheduler
    ;;
  *)
    exec python manage.py "$@"
    ;;
esac
