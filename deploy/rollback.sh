#!/bin/sh
# الرجوع للنسخة السابقة من النظام بعد تحديث سبّب مشكلة (يحفظها deploy/update.sh):
#   sh deploy/rollback.sh
# يُرجع الكود وصورة التشغيل خلال دقيقة دون المساس بالبيانات. إن كان التحديث غيّر بنية قاعدة البيانات
# وظل النظام لا يعمل، استعد النسخة الاحتياطية المأخوذة قبل التحديث (يُطبع مسارها بالأسفل) بـ deploy/restore.sh
# — وانتبه أن الاستعادة تحذف ما أُدخل بعد التحديث.
set -e
[ -f .deploy/previous_commit ] || { echo "لا توجد نسخة سابقة محفوظة (تُحفظ عند التحديث بـ deploy/update.sh)."; exit 1; }
commit=$(cat .deploy/previous_commit)
if [ "$1" != "--yes" ]; then
  echo "سيُرجَع النظام إلى النسخة $(git rev-parse --short "$commit")."
  printf "اكتب نعم للمتابعة: "; read answer; [ "$answer" = "نعم" ] || exit 1
fi
git checkout --quiet "$commit"
if docker image inspect synar-erp:previous >/dev/null 2>&1; then
  docker tag synar-erp:previous synar-erp:latest
  docker compose up -d --no-build web scheduler
else
  docker compose up -d --build web scheduler
fi
if sh deploy/healthcheck.sh 120; then
  echo "تم الرجوع للنسخة $(git rev-parse --short HEAD) والنظام يعمل."
  echo "للتحديث مجدداً بعد إصلاح المشكلة: sh deploy/update.sh"
else
  echo "النظام ما زال لا يستجيب. غالباً غيّر التحديث بنية قاعدة البيانات؛ استعد النسخة المأخوذة قبله:"
  echo "  sh deploy/restore.sh $(cat .deploy/previous_backup 2>/dev/null)"
  exit 1
fi
