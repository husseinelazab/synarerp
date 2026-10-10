# تشغيل سِينار في الإنتاج

هذا الدليل لتشغيل النظام على خادم حقيقي لعملاء فعليين. للتجربة على جهازك استخدم `run.sh` أو `run.bat` بدلاً منه.

## ما الذي تحصل عليه

| الخدمة | الدور |
|---|---|
| `db` | قاعدة بيانات PostgreSQL 16 (بياناتها في volume دائم) |
| `web` | التطبيق عبر Gunicorn؛ يطبّق التحديثات (migrations) تلقائياً عند كل تشغيل |
| `scheduler` | المهام الدورية: تنبيهات الموارد البشرية يومياً، وإعادة إرسال الفواتير الإلكترونية المعلقة كل ساعة (للشركات التي فعّلت الربط بنفسها فقط) |
| `caddy` | شهادة HTTPS مجانية تلقائية (Let's Encrypt) وتجديدها، وتحويل HTTP إلى HTTPS |
| `backup` | نسخة احتياطية مضغوطة لقاعدة البيانات يومياً في مجلد `backups/` مع حذف الأقدم من 14 يوماً |

## المتطلبات

- خادم Linux (Ubuntu 22.04 أو أحدث) بذاكرة 2GB على الأقل — يكفي لعشرات الشركات الصغيرة. يُفضّل خادم داخل المملكة.
- نطاق (domain) مثل `erp.yourcompany.com` وسجل DNS من نوع **A** يشير إلى عنوان الخادم.
- المنفذان 80 و443 مفتوحان.
- Docker مع Docker Compose: `curl -fsSL https://get.docker.com | sh`

## التثبيت (مرة واحدة)

```bash
git clone <رابط المستودع> synar && cd synar
cp .env.example .env
nano .env          # عدّل القيم (انظر أدناه)
docker compose up -d --build
```

القيم الواجب تعديلها في `.env`:

| المتغير | القيمة |
|---|---|
| `SYNAR_DOMAIN` و`SYNAR_ALLOWED_HOSTS` | نطاقك، مثل `erp.yourcompany.com` |
| `SYNAR_CSRF_TRUSTED_ORIGINS` و`SYNAR_SITE_URL` | `https://erp.yourcompany.com` |
| `SYNAR_SECRET_KEY` | قيمة عشوائية: `python3 -c "import secrets; print(secrets.token_urlsafe(50))"` — **لا تغيّرها بعد التشغيل** |
| `SYNAR_DB_PASSWORD` | كلمة مرور قوية لقاعدة البيانات |
| `SYNAR_ADMIN_USER` و`SYNAR_ADMIN_PASSWORD` | حساب مدير المنصة الذي يُنشأ عند أول تشغيل |

بعد دقيقة افتح `https://erp.yourcompany.com` وادخل بحساب مدير المنصة، ثم:
1. غيّر كلمة المرور من «حسابي».
2. اضبط «إدارة المنصة ← خادم البريد» متى أردت (اختياري).
3. أنشئ الشركات من لوحة المنصة، أو اترك العملاء يسجلون من صفحة التسجيل.

## العمليات اليومية

```bash
docker compose ps                         # حالة الخدمات
docker compose logs -f web                # سجل التطبيق
docker compose exec web python manage.py createsuperuser   # أي أمر manage.py
docker compose exec backup sh /backup.sh once   # نسخة احتياطية فورية
```

**التحديث لنسخة جديدة:**
```bash
git pull && docker compose up -d --build   # التحديثات على قاعدة البيانات تُطبق تلقائياً
```
خذ نسخة احتياطية فورية قبل كل تحديث، وزد قيمة `SYNAR_POS_CACHE_VERSION` في `.env` لتحدّث أجهزة نقاط البيع ملفاتها المخزنة.

## النسخ الاحتياطي والاستعادة

- النسخ اليومية في مجلد `backups/` على الخادم. **انسخها خارج الخادم بانتظام** (مثلاً إلى تخزين سحابي) — نسخة على نفس الخادم لا تحمي من تلف القرص.
- الاستعادة (تحذف البيانات الحالية): `sh deploy/restore.sh backups/synar-YYYYMMDD-HHMMSS.sql.gz`
- جرّب الاستعادة على خادم آخر مرة كل فترة لتتأكد أن نسخك سليمة.

## الأمان المطبق

- HTTPS إلزامي مع HSTS، وملفات تعريف الارتباط آمنة (Secure / HttpOnly)، ومنع التضمين في إطارات (clickjacking).
- كلمات المرور: 8 أحرف على الأقل، ولا تكون شائعة أو أرقاماً فقط أو قريبة من اسم المستخدم.
- الجلسة تنتهي بعد 12 ساعة (`SYNAR_SESSION_HOURS`).
- مفاتيح الفوترة الإلكترونية وكلمة مرور البريد مخزنة مشفرة بمفتاح مشتق من `SYNAR_SECRET_KEY`.
- التطبيق يعمل داخل الحاوية بمستخدم غير root، وقاعدة البيانات غير مكشوفة للإنترنت.
- مسار فحص الصحة: `/healthz/` (يستخدمه Docker لإعادة تشغيل التطبيق إن توقف).

## ملاحظات

- **التوسع:** زد `SYNAR_WORKERS` مع زيادة الأنوية، وانقل قاعدة البيانات إلى خدمة مُدارة عند كبر الحجم.
- **أكثر من نطاق** (لكل عميل نطاق): أضف النطاقات في `SYNAR_ALLOWED_HOSTS` و`SYNAR_CSRF_TRUSTED_ORIGINS` ككتل إضافية في `deploy/Caddyfile`.
- **بدون Docker:** يعمل أيضاً بـ `gunicorn synar.wsgi` خلف Nginx مع PostgreSQL؛ اضبط متغيرات `.env` نفسها، ونفّذ
  `python manage.py migrate` و`collectstatic`، وشغّل `python manage.py run_scheduler` كخدمة دائمة.
