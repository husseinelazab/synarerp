#!/bin/sh
# استعادة نسخة احتياطية (تحذف البيانات الحالية!):
#   docker compose stop web scheduler
#   docker compose exec -T db sh -c 'dropdb -U "$POSTGRES_USER" "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
#   gunzip -c backups/synar-XXXX.sql.gz | docker compose exec -T db psql -U synar synar
#   docker compose start web scheduler
set -e
[ -f "$1" ] || { echo "الاستخدام: sh deploy/restore.sh backups/synar-XXXX.sql.gz"; exit 1; }
echo "سيتم استبدال قاعدة البيانات الحالية بالنسخة $1"
printf "اكتب نعم للمتابعة: "; read answer; [ "$answer" = "نعم" ] || exit 1
docker compose stop web scheduler
docker compose exec -T db sh -c 'dropdb -U "$POSTGRES_USER" "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
gunzip -c "$1" | docker compose exec -T db sh -c 'psql -q -U "$POSTGRES_USER" "$POSTGRES_DB"'
docker compose start web scheduler
echo "تمت الاستعادة."
