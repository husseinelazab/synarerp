# نشر سِينار على PythonAnywhere (للتجربة مجاناً)

الرابط يكون بالشكل: `https://USERNAME.pythonanywhere.com`. استبدل **USERNAME** في كل الأوامر باسم حسابك.

## قبل البدء: حدود الخطة المجانية

- **الإنترنت الخارجي مقيّد**: ميزات الذكاء الاصطناعي، والربط مع منصة «فاتورة»، وإرسال البريد قد لا تعمل.
- **الأداء محدود**: مناسب لتجربة عدد قليل من المستخدمين.
- **مساحة القرص 512MB.**
- **مدة التشغيل**: يجب الضغط على زر **Run until 3 months from today** في تبويب Web مرة كل 3 أشهر، وإلا يتوقف الموقع.
- **قاعدة البيانات** هي SQLite (`db.sqlite3`)، وبياناتها تبقى ولا تُمسح.

## 1. إنشاء الحساب
سجّل في https://www.pythonanywhere.com واختر الخطة المجانية **Beginner**.

## 2. تنزيل الكود
افتح **Consoles ← Bash** واكتب:

```bash
git clone https://github.com/husseinelazab/HAMTerp.git
cd HAMTerp
```

> إذا كان المستودع **خاصاً (Private)** فسيطلب اسم المستخدم وكلمة المرور.
> - اكتب اسم مستخدم GitHub.
> - في مكان كلمة المرور الصق **Personal access token**، تنشئه من GitHub ← Settings ← Developer settings ← Personal access tokens، وتعطيه صلاحية قراءة المستودع.
>
> بديل بدون token: ارفع ملف ZIP من تبويب **Files** (حدّه 100MB)، ثم نفّذ:
> ```bash
> unzip HAMTerp-*.zip && mv HAMTerp-* HAMTerp
> ```

## 3. تجهيز البيئة

```bash
mkvirtualenv --python=/usr/bin/python3.12 synar
pip install -r ~/HAMTerp/requirements.txt
```

الأمر الأول ينشئ البيئة ويفعّلها. إذا فتحت Console جديدة لاحقاً، فعّلها بالأمر `workon synar`.

## 4. ملف الإعدادات `.env`

**أ. ولّد مفتاحاً سرياً وانسخ الناتج:**

```bash
python -c "import secrets; print(secrets.token_urlsafe(50))"
```

**ب. أنشئ الملف بالمحرر:**

```bash
nano ~/HAMTerp/.env
```

**ج. الصق فيه السطور التالية، وعدّل USERNAME والمفتاح:**

```
SYNAR_DEBUG=0
SYNAR_SECRET_KEY=الصق-المفتاح-هنا
SYNAR_ALLOWED_HOSTS=USERNAME.pythonanywhere.com
SYNAR_CSRF_TRUSTED_ORIGINS=https://USERNAME.pythonanywhere.com
SYNAR_SITE_URL=https://USERNAME.pythonanywhere.com
SYNAR_SSL_REDIRECT=0
```

احفظ بـ `Ctrl+O` ثم `Enter`، واخرج بـ `Ctrl+X`.

> لا تغيّر `SYNAR_SECRET_KEY` بعد التشغيل.
> `SYNAR_SSL_REDIRECT=0` لأن التحويل إلى HTTPS يتم من إعداد **Force HTTPS** في الخطوة 6.

## 5. قاعدة البيانات والملفات الثابتة

حمّل الإعدادات في الـ Console الحالية:

```bash
cd ~/HAMTerp
set -a; source .env; set +a
```

### الخيار (أ): بياناتك الحالية من كمبيوترك
1. من تبويب **Files** ادخل مجلد `/home/USERNAME/HAMTerp/`.
2. ارفع ملف `db.sqlite3`.
3. نفّذ:

```bash
python manage.py migrate
```

### الخيار (ب): بداية جديدة
اختر واحداً من الأمرين:

```bash
# بشركة تجريبية ببيانات جاهزة:
python manage.py migrate && python manage.py setup_erp --demo --admin admin --password "كلمة-مرور-قوية"

# أو منصة فارغة بمدير فقط:
python manage.py migrate && python manage.py setup_erp --no-company --admin admin --password "كلمة-مرور-قوية"
```

### بعدها في الحالتين

```bash
python manage.py createcachetable
python manage.py collectstatic --noinput
```

## 6. إعداد تطبيق الويب

### أ. إنشاء التطبيق
1. افتح تبويب **Web** واضغط **Add a new web app**.
2. اضغط **Next**، ثم اختر **Manual configuration**. **لا تختر Django.**
3. اختر **Python 3.12**.

### ب. ضبط الإعدادات في صفحة التطبيق

| القسم | القيمة |
|---|---|
| **Source code** | `/home/USERNAME/HAMTerp` |
| **Working directory** | `/home/USERNAME/HAMTerp` |
| **Virtualenv** | `/home/USERNAME/.virtualenvs/synar` |
| **Static files** | URL: `/static/` ← Directory: `/home/USERNAME/HAMTerp/staticfiles` |
| **Security** | فعّل **Force HTTPS** |

### ج. ملف WSGI
1. اضغط على رابط **WSGI configuration file**، واحذف كل محتواه.
2. الصق محتوى الملف [`deploy/pythonanywhere_wsgi.py`](deploy/pythonanywhere_wsgi.py) كما هو، ثم احفظ.

### د. التشغيل
اضغط زر **Reload** الأخضر أعلى الصفحة، ثم افتح `https://USERNAME.pythonanywhere.com`.

## 7. المهام اليومية (اختياري)
الخطة المجانية تسمح بمهمة واحدة يومياً، وهي تشغّل التنبيهات اليومية مثل تنبيهات الموارد البشرية وانتهاء الصلاحية.

من تبويب **Tasks** أضف مهمة يومية بهذا الأمر:

```bash
cd ~/HAMTerp && set -a && . ./.env && set +a && ~/.virtualenvs/synar/bin/python manage.py run_scheduler --once
```

## تحديث النظام لاحقاً

```bash
workon synar
cd ~/HAMTerp
cp db.sqlite3 db.sqlite3.bak     # نسخة احتياطية قبل التحديث
git pull
set -a; source .env; set +a
pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --noinput
```

بعد التحديث اضغط **Reload** من تبويب Web. البيانات في `db.sqlite3` لا تتأثر.

**نسخة احتياطية:** نزّل `db.sqlite3` من تبويب Files من وقت لآخر.

## حل المشاكل

| المشكلة | الحل |
|---|---|
| **Something went wrong** أو خطأ 500 | افتح **Error log** من تبويب Web، وغالباً يكون السطر الأخير هو السبب. |
| **DisallowedHost** | راجع `SYNAR_ALLOWED_HOSTS` في `.env`، ثم اضغط Reload. |
| **CSRF verification failed** عند الدخول | راجع `SYNAR_CSRF_TRUSTED_ORIGINS`: يجب أن تبدأ بـ `https://`. |
| **الصفحة بدون تنسيق** | نفّذ `collectstatic`، وتأكد من مسار Static files في تبويب Web. |
| **اضبط SYNAR_SECRET_KEY** | ملف `.env` غير موجود في `/home/USERNAME/HAMTerp/` أو المفتاح فارغ. |
