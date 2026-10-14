#!/usr/bin/env bash
# تثبيت سِينار على خادم جديد بأمر واحد (Ubuntu): يسأل عن النطاق وحساب مدير المنصة فقط،
# ويولّد كلمات السر والمفاتيح تلقائياً، ثم يشغّل النظام.
#   sudo bash deploy/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."

say() { printf '\n== %s\n' "$*"; }
die() { printf '\n✗ %s\n' "$*" >&2; exit 1; }

# ── 1) Docker ──
if ! command -v docker >/dev/null 2>&1; then
  say "تثبيت Docker (مرة واحدة، دقائق قليلة)..."
  curl -fsSL https://get.docker.com | sh
fi
docker compose version >/dev/null 2>&1 || die "Docker Compose غير مثبت. ثبّته ثم أعد تشغيل هذا الأمر."

# ── 2) ملف الإعدادات .env ──
gen() { openssl rand -base64 96 | tr -dc 'A-Za-z0-9' | head -c "$1"; }

# يكتب القيمة بين علامتي تنصيص مفردتين حتى لا يفسّر Docker Compose أي رمز فيها
set_var() {
  KEY="$1" VAL="$2" awk 'BEGIN { k = ENVIRON["KEY"]; v = ENVIRON["VAL"]; done = 0 }
    $0 ~ "^#? ?" k "=" && !done { print k "='"'"'" v "'"'"'"; done = 1; next } { print }
    END { if (!done) print k "='"'"'" v "'"'"'" }' .env > .env.tmp && mv .env.tmp .env
}

if [ -f .env ]; then
  say "ملف الإعدادات .env موجود من قبل؛ سيُستخدم كما هو."
else
  say "إعداد النظام لأول مرة"
  while true; do
    read -rp "اسم النطاق الذي سيعمل عليه النظام (مثل erp.example.com): " DOMAIN
    DOMAIN=$(printf '%s' "$DOMAIN" | tr 'A-Z' 'a-z' | sed -e 's|^https\?://||' -e 's|/.*$||')
    [[ "$DOMAIN" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]] && break
    echo "اكتب النطاق فقط، دون https:// ودون مسافات."
  done
  read -rp "اسم مستخدم مدير المنصة [admin]: " ADMIN
  ADMIN=${ADMIN:-admin}
  [[ "$ADMIN" =~ ^[A-Za-z0-9_.@+-]+$ ]] || die "اسم المستخدم بالأحرف الإنجليزية والأرقام فقط."
  while true; do
    read -rsp "كلمة مرور المدير (12 حرفاً على الأقل): " PASS; echo
    read -rsp "أعد كتابتها: " PASS2; echo
    if [ "$PASS" != "$PASS2" ]; then echo "غير متطابقتين، حاول مرة أخرى."; continue; fi
    if [ "${#PASS}" -lt 12 ]; then echo "قصيرة: 12 حرفاً على الأقل."; continue; fi
    if [[ "$PASS" == *"'"* ]]; then echo "لا تستخدم علامة التنصيص ' في كلمة المرور."; continue; fi
    break
  done

  BACKUP_PASSPHRASE=$(gen 40)
  cp .env.example .env
  set_var SYNAR_DOMAIN "$DOMAIN"
  set_var SYNAR_ALLOWED_HOSTS "$DOMAIN"
  set_var SYNAR_CSRF_TRUSTED_ORIGINS "https://$DOMAIN"
  set_var SYNAR_SITE_URL "https://$DOMAIN"
  set_var SYNAR_SECRET_KEY "$(gen 60)"
  set_var SYNAR_DB_PASSWORD "$(gen 32)"
  set_var SYNAR_ADMIN_USER "$ADMIN"
  set_var SYNAR_ADMIN_PASSWORD "$PASS"
  set_var SYNAR_BACKUP_PASSPHRASE "$BACKUP_PASSPHRASE"
  chmod 600 .env  # فيه أسرار: لصاحب الخادم فقط

  cat <<EOF

╔══════════════════════════════════════════════════════════════════╗
  مهم جداً — احفظ كلمة سر النسخ الاحتياطي التالية خارج الخادم
  (في مدير كلمات المرور أو ورقة في مكان آمن). بدونها لا يمكن
  استعادة أي نسخة احتياطية أبداً:

      $BACKUP_PASSPHRASE

╚══════════════════════════════════════════════════════════════════╝
EOF
  read -rp "اضغط Enter بعد أن تحفظها... " _
fi

# ── 3) التحقق من توجيه النطاق ──
DOMAIN=$(sed -n "s/^SYNAR_DOMAIN='\{0,1\}\([^']*\)'\{0,1\}$/\1/p" .env | head -1)
SERVER_IP=$(curl -fsS --max-time 8 https://api.ipify.org 2>/dev/null || true)
DOMAIN_IP=$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk 'NR==1 {print $1}' || true)
if [ -n "$SERVER_IP" ] && [ "$DOMAIN_IP" != "$SERVER_IP" ]; then
  cat <<EOF

⚠ النطاق $DOMAIN يشير الآن إلى «${DOMAIN_IP:-لا شيء}» وعنوان هذا الخادم $SERVER_IP.
  أضف عند مزوّد النطاق سجلاً من نوع A يشير إلى $SERVER_IP ثم انتظر دقائق.
  سيعمل النظام الآن، وتصدر شهادة HTTPS تلقائياً حين يصل التوجيه.
EOF
fi

# ── 4) التشغيل ──
say "بناء النظام وتشغيله (أول مرة تأخذ 5–10 دقائق)..."
docker compose up -d --build

say "انتظار جاهزية النظام..."
for _ in $(seq 1 60); do
  if docker compose exec -T web python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz/', timeout=4).status == 200 else 1)" >/dev/null 2>&1; then
    cat <<EOF

✓ سِينار يعمل الآن:  https://$DOMAIN
  ادخل باسم المستخدم الذي اخترته، ثم غيّر كلمة المرور من «حسابي».
  الحالة:        docker compose ps
  السجل:         docker compose logs -f web
  نسخة فورية:    docker compose exec backup sh /backup.sh once
EOF
    exit 0
  fi
  sleep 5
done
die "لم يجهز النظام خلال 5 دقائق. اعرض السجل بالأمر: docker compose logs web"
