#!/bin/sh
# استعادة نسخة احتياطية (تحذف البيانات الحالية!) — تُشغَّل من مجلد المشروع على الخادم:
#   sh deploy/restore.sh backups/synar-XXXX.sql.gz.gpg   (نسخة مشفّرة: تُفك بكلمة SYNAR_BACKUP_PASSPHRASE من .env)
#   sh deploy/restore.sh backups/synar-XXXX.sql.gz       (نسخة غير مشفّرة)
#
# لاستعادة بيانات شركة واحدة دون المساس بالباقين: استعد النسخة في قاعدة مؤقتة ثم صدّر الشركة منها
# بالأمر export_company (انظر README قسم «النسخ الاحتياطي»).
set -e
[ -f "$1" ] || { echo "الاستخدام: sh deploy/restore.sh backups/synar-XXXX.sql.gz[.gpg]"; exit 1; }
case "$1" in
  *.gpg) name=$(basename "$1")
         [ -f "backups/$name" ] || { echo "ضع الملف في مجلد backups داخل مجلد المشروع ثم أعد المحاولة"; exit 1; }
         # فحص كلمة السر وسلامة الملف قبل حذف أي بيانات
         docker compose run --rm -T --entrypoint sh backup /backup.sh decrypt "/backups/$name" | gzip -t \
           || { echo "تعذر فك التشفير: تحقق من SYNAR_BACKUP_PASSPHRASE في .env"; exit 1; }
         read_backup() { docker compose run --rm -T --entrypoint sh backup /backup.sh decrypt "/backups/$name" | gunzip -c; } ;;
  *)     gzip -t "$1" || { echo "الملف تالف"; exit 1; }
         read_backup() { gunzip -c "$1"; } ;;
esac
echo "سيتم استبدال قاعدة البيانات الحالية بالنسخة $1"
printf "اكتب نعم للمتابعة: "; read answer; [ "$answer" = "نعم" ] || exit 1
docker compose stop web scheduler
docker compose exec -T db sh -c 'dropdb -U "$POSTGRES_USER" "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
read_backup "$1" | docker compose exec -T db sh -c 'psql -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" "$POSTGRES_DB"'
docker compose start web scheduler
echo "تمت الاستعادة."
