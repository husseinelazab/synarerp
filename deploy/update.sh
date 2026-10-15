#!/bin/sh
# تحديث آمن لنسخة جديدة مع إمكانية الرجوع — يُشغَّل من مجلد المشروع على الخادم:
#   sh deploy/update.sh
# 1) نسخة احتياطية فورية لقاعدة البيانات (يتوقف التحديث إن فشلت)
# 2) حفظ رقم النسخة الحالية من الكود وصورتها (synar-erp:previous) للرجوع إليها
# 3) جلب التحديث وبناؤه وتشغيله، ثم فحص صحة النظام؛ إن لم يعمل خلال دقيقتين يُرجَع للنسخة السابقة تلقائياً
set -e
mkdir -p .deploy
echo "١) نسخة احتياطية قبل التحديث..."
docker compose exec -T backup sh /backup.sh once || { echo "فشلت النسخة الاحتياطية؛ أُلغي التحديث."; exit 1; }
ls -t backups/synar-*.sql.gz* 2>/dev/null | head -1 > .deploy/previous_backup || true

echo "٢) حفظ النسخة الحالية للرجوع إليها..."
git rev-parse HEAD > .deploy/previous_commit
git symbolic-ref --short -q HEAD > .deploy/branch || true
docker image inspect synar-erp:latest >/dev/null 2>&1 && docker tag synar-erp:latest synar-erp:previous

echo "٣) جلب التحديث وتشغيله..."
# بعد رجوع سابق يكون الكود على نسخة محددة لا على الفرع: نعود للفرع أولاً
branch=$(git symbolic-ref --short -q HEAD || cat .deploy/branch 2>/dev/null || echo "")
[ -n "$branch" ] || { echo "تعذر تحديد الفرع؛ نفّذ git checkout <اسم الفرع> ثم أعد المحاولة."; exit 1; }
echo "$branch" > .deploy/branch
git checkout --quiet "$branch"
git pull --ff-only
docker compose up -d --build web scheduler

if sh deploy/healthcheck.sh 120; then
  echo "تم التحديث بنجاح إلى $(git rev-parse --short HEAD)."
  echo "إن ظهرت مشكلة لاحقاً: sh deploy/rollback.sh"
else
  echo "النظام لا يستجيب بعد التحديث؛ جارٍ الرجوع للنسخة السابقة..."
  sh deploy/rollback.sh --yes
  exit 1
fi
