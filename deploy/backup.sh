#!/bin/sh
# نسخ احتياطي لقاعدة البيانات: pg_dump مضغوط يومياً مع حذف النسخ الأقدم من BACKUP_KEEP_DAYS
#   sh backup.sh once   ← نسخة فورية
#   sh backup.sh loop   ← يومياً في الساعة BACKUP_HOUR (يستخدمه docker compose)
set -e
DIR=/backups
mkdir -p "$DIR"

backup_once() {
  file="$DIR/synar-$(date +%Y%m%d-%H%M%S).sql.gz"
  pg_dump --no-owner --no-privileges | gzip > "$file.tmp" && mv "$file.tmp" "$file"
  echo "تمت النسخة: $file ($(du -h "$file" | cut -f1))"
  find "$DIR" -name 'synar-*.sql.gz' -mtime +"${BACKUP_KEEP_DAYS:-14}" -delete
}

if [ "$1" = "loop" ]; then
  backup_once
  while true; do
    now=$(date +%s)
    next=$(date -d "$(date +%Y-%m-%d) ${BACKUP_HOUR:-2}:00" +%s 2>/dev/null || echo $((now + 86400)))
    [ "$next" -le "$now" ] && next=$((next + 86400))
    sleep $((next - now))
    backup_once || echo "فشلت النسخة الاحتياطية"
  done
else
  backup_once
fi
