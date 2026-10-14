#!/bin/sh
# نسخ احتياطي لقاعدة البيانات: pg_dump مضغوط يومياً، مشفّر (AES-256) إذا ضُبطت SYNAR_BACKUP_PASSPHRASE،
# ثم فحص سلامة النسخة، وحذف النسخ الأقدم من BACKUP_KEEP_DAYS.
#   sh backup.sh once   ← نسخة فورية
#   sh backup.sh loop   ← يومياً في الساعة BACKUP_HOUR (يستخدمه docker compose)
# حالة آخر نسخة تُكتب في /backups/status.json ليعرضها النظام في لوحة إدارة المنصة.
#
# النسخ خارج الخادم (اختياري ومُوصى به): اضبط BACKUP_REMOTE على وجهة rclone (مثل offsite:synar-backups)
# وإعدادات الوجهة في deploy/offsite.env (تخزين متوافق مع S3 أو خادم SFTP، ويُفضّل داخل المملكة).
# بعد كل نسخة ناجحة تُرفع النسخة المشفّرة وأرشيف مشفّر لملفات المرفقات، وتُحذف النسخ الخارجية الأقدم
# من BACKUP_REMOTE_KEEP_DAYS. لا يُرفع شيء غير مشفّر. حالة الرفع في /backups/remote_status.json.
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

write_remote_status() {
  printf '{"time": "%s", "ok": %s, "target": "%s", "files": "%s", "message": "%s"}\n' \
    "$(date -Iseconds)" "$1" "$BACKUP_REMOTE" "$2" "$3" > "$DIR/remote_status.json.tmp" \
    && chmod 644 "$DIR/remote_status.json.tmp" && mv "$DIR/remote_status.json.tmp" "$DIR/remote_status.json"
}

# رفع النسخة المشفّرة (وأرشيف المرفقات المشفّر) إلى الوجهة الخارجية
upload_remote() {
  [ -n "$BACKUP_REMOTE" ] || return 0
  if [ "$2" != "true" ]; then
    write_remote_status false "" "لم تُرفع: النسخ خارج الخادم يتطلب تشفير النسخ (SYNAR_BACKUP_PASSPHRASE)"
    echo "لم تُرفع النسخة: اضبط SYNAR_BACKUP_PASSPHRASE لتشفير النسخ قبل رفعها خارج الخادم."; return 1
  fi
  uploaded=$(basename "$1")
  rclone copyto "$1" "$BACKUP_REMOTE/$uploaded" --retries 3 || {
    write_remote_status false "" "فشل رفع النسخة إلى $BACKUP_REMOTE"; echo "فشل رفع النسخة"; return 1; }
  media=${MEDIA_DIR:-/media}
  if [ -d "$media" ] && [ -n "$(ls -A "$media" 2>/dev/null)" ]; then
    archive="$DIR/media-$3.tar.gz.gpg"
    { tar -C "$media" -czf - . | encrypt > "$archive.tmp"; } && mv "$archive.tmp" "$archive" \
      && rclone copyto "$archive" "$BACKUP_REMOTE/$(basename "$archive")" --retries 3 || {
        rm -f "$archive" "$archive.tmp"
        write_remote_status false "$uploaded" "رُفعت قاعدة البيانات وفشل رفع أرشيف المرفقات"; echo "فشل رفع المرفقات"; return 1; }
    rm -f "$archive"
    uploaded="$uploaded $(basename "$archive")"
  fi
  rclone delete "$BACKUP_REMOTE" --min-age "${BACKUP_REMOTE_KEEP_DAYS:-30}d" --include 'synar-*.sql.gz.gpg' --include 'media-*.tar.gz.gpg' || true
  write_remote_status true "$uploaded" "رُفعت إلى $BACKUP_REMOTE"
  echo "رُفعت النسخة خارج الخادم: $uploaded"
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
  upload_remote "$file" "$encrypted" "$stamp" || true
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
