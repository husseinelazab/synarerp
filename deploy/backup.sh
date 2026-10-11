#!/bin/sh
# نسخ احتياطي لقاعدة البيانات: pg_dump مضغوط يومياً، مشفّر (AES-256) إذا ضُبطت SYNAR_BACKUP_PASSPHRASE،
# ثم فحص سلامة النسخة، وحذف النسخ الأقدم من BACKUP_KEEP_DAYS.
#   sh backup.sh once   ← نسخة فورية
#   sh backup.sh loop   ← يومياً في الساعة BACKUP_HOUR (يستخدمه docker compose)
# حالة آخر نسخة تُكتب في /backups/status.json ليعرضها النظام في لوحة إدارة المنصة.
set -e
DIR=${BACKUP_DIR:-/backups}
mkdir -p "$DIR"
umask 077

encrypt() {
  gpg --batch --yes --quiet --pinentry-mode loopback --symmetric --cipher-algo AES256 \
      --passphrase-fd 3 --output - 3<<PASS
$BACKUP_PASSPHRASE
PASS
}

decrypt() {
  gpg --batch --quiet --pinentry-mode loopback --decrypt --passphrase-fd 3 "$1" 3<<PASS
$BACKUP_PASSPHRASE
PASS
}

write_status() {
  printf '{"time": "%s", "ok": %s, "file": "%s", "size": %s, "encrypted": %s, "message": "%s"}\n' \
    "$(date -Iseconds)" "$1" "$2" "${3:-0}" "$4" "$5" > "$DIR/status.json.tmp" && chmod 644 "$DIR/status.json.tmp" && mv "$DIR/status.json.tmp" "$DIR/status.json"
}

# فشل pg_dump في منتصف الأنبوب يُعلَّم بملف (sh لا يدعم pipefail في كل الإصدارات)
dump() {
  rm -f "$DIR/.dump-failed"
  { pg_dump --no-owner --no-privileges || touch "$DIR/.dump-failed"; } | gzip
  [ ! -e "$DIR/.dump-failed" ]
}

fail() {
  rm -f "$1"
  write_status false "" 0 "$2" "$3"
  echo "$3"; return 1
}

backup_once() {
  stamp=$(date +%Y%m%d-%H%M%S)
  if [ -n "$BACKUP_PASSPHRASE" ]; then
    file="$DIR/synar-$stamp.sql.gz.gpg"; encrypted=true
    dump | encrypt > "$file.tmp" && [ ! -e "$DIR/.dump-failed" ] || { fail "$file.tmp" true "فشل أخذ النسخة (pg_dump)"; return 1; }
    check() { decrypt "$1" | gzip -t; }
  else
    file="$DIR/synar-$stamp.sql.gz"; encrypted=false
    echo "تنبيه: النسخة غير مشفّرة. اضبط SYNAR_BACKUP_PASSPHRASE في ملف .env لتشفير النسخ."
    dump > "$file.tmp" || { fail "$file.tmp" false "فشل أخذ النسخة (pg_dump)"; return 1; }
    check() { gzip -t "$1"; }
  fi
  check "$file.tmp" || { fail "$file.tmp" "$encrypted" "فشل فحص سلامة النسخة"; return 1; }
  mv "$file.tmp" "$file"
  size=$(wc -c < "$file" | tr -d ' ')
  write_status true "$(basename "$file")" "$size" "$encrypted" "تمت النسخة وفُحصت سلامتها"
  echo "تمت النسخة وفُحصت سلامتها: $file ($(du -h "$file" | cut -f1))"
  find "$DIR" -name 'synar-*.sql.gz*' -mtime +"${BACKUP_KEEP_DAYS:-14}" -delete
}

run() {
  backup_once || true  # الحالة كُتبت في status.json؛ تستمر الحلقة لليوم التالي
}

if [ "$1" = "decrypt" ]; then
  # فك تشفير نسخة إلى المخرج القياسي: sh backup.sh decrypt /backups/synar-XXXX.sql.gz.gpg
  decrypt "$2"
elif [ "$1" = "loop" ]; then
  run
  while true; do
    now=$(date +%s)
    next=$(date -d "$(date +%Y-%m-%d) ${BACKUP_HOUR:-2}:00" +%s 2>/dev/null || echo $((now + 86400)))
    [ "$next" -le "$now" ] && next=$((next + 86400))
    sleep $((next - now))
    run
  done
else
  backup_once
fi
