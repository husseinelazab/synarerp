"""إعدادات نظام سِينار (Synar ERP)."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

DEBUG = os.environ.get("SYNAR_DEBUG", "1") == "1"
SECRET_KEY = os.environ.get("SYNAR_SECRET_KEY", "dev-insecure-change-me" if DEBUG else "")
if not SECRET_KEY or (not DEBUG and SECRET_KEY.startswith("dev-insecure")):
    # في الإنتاج يجب مفتاح سري قوي وثابت: تتغير بتغييره كل الجلسات وتُفقد مفاتيح الفوترة وكلمة مرور البريد المشفرة
    raise RuntimeError("اضبط SYNAR_SECRET_KEY بقيمة عشوائية طويلة قبل التشغيل في الإنتاج (SYNAR_DEBUG=0).")
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("SYNAR_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = [o for o in os.environ.get("SYNAR_CSRF_TRUSTED_ORIGINS", "").split(",") if o]

# وضع التجربة: الوصول عبر localhost (ومنه تحويل منفذ Codespaces المحلي الذي يفتح https://localhost:8000)
if DEBUG:
    CSRF_TRUSTED_ORIGINS += [f"{scheme}://{host}:8000" for scheme in ("http", "https") for host in ("localhost", "127.0.0.1")]

# التشغيل داخل GitHub Codespaces: السماح برابط المعاينة الذي يوفره GitHub
if os.environ.get("CODESPACE_NAME"):
    _domain = os.environ.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "app.github.dev")
    ALLOWED_HOSTS.append(f".{_domain}")
    CSRF_TRUSTED_ORIGINS.append(f"https://{os.environ['CODESPACE_NAME']}-8000.{_domain}")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",
    "accounts",
    "stock",
    "selling",
    "buying",
    "hr",
    "pos",
    "saas",
    "einvoice",
    "assets",
    "manufacturing",
    "messaging",
    "pricing",
    "crm",
    "projects",
    "support",
    "quality",
    "beneficiaries",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "core.middleware.TenantMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "synar.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.erp",
            ],
        },
    },
]

WSGI_APPLICATION = "synar.wsgi.application"

if os.environ.get("SYNAR_DB_NAME"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ["SYNAR_DB_NAME"],
            "USER": os.environ.get("SYNAR_DB_USER", ""),
            "PASSWORD": os.environ.get("SYNAR_DB_PASSWORD", ""),
            "HOST": os.environ.get("SYNAR_DB_HOST", "localhost"),
            "PORT": os.environ.get("SYNAR_DB_PORT", "5432"),
            "CONN_MAX_AGE": int(os.environ.get("SYNAR_DB_CONN_MAX_AGE", "60")),
            "CONN_HEALTH_CHECKS": True,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# اللغة العربية افتراضياً وبالكامل
LANGUAGE_CODE = "ar"
LANGUAGES = [("ar", "العربية")]
TIME_ZONE = os.environ.get("SYNAR_TIME_ZONE", "Asia/Riyadh")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    # الملفات الثابتة تُخدم مضغوطة وبأسماء ثابتة التجزئة (WhiteNoise) في الإنتاج
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage" if DEBUG
                    else "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
MEDIA_ROOT = Path(os.environ.get("SYNAR_MEDIA_ROOT", BASE_DIR / "media"))

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ذاكرة مؤقتة مشتركة بين كل عمليات الخادم (لحدّ محاولات الدخول الخاطئة) في جدول بقاعدة البيانات
CACHES = {"default": {"BACKEND": "django.core.cache.backends.db.DatabaseCache", "LOCATION": "synar_cache"}}
if os.environ.get("SYNAR_REDIS_URL"):
    # Redis أسرع بكثير تحت الضغط ويشترك فيه كل عمّال الخادم (اختياري؛ يُفعّل في docker-compose)
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache",
                          "LOCATION": os.environ["SYNAR_REDIS_URL"], "KEY_PREFIX": "synar"}}

AUTHENTICATION_BACKENDS = ["core.auth_backends.CaseInsensitiveModelBackend"]

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

# اسم النظام المعروض في الواجهة
ERP_NAME = "سِينار"
ERP_NAME_EN = "Synar ERP"
# يتغير مع كل إصدار ليحدّث عامل خدمة نقطة البيع الملفات المخزنة على الأجهزة
SYNAR_POS_CACHE_VERSION = os.environ.get("SYNAR_POS_CACHE_VERSION", "2026-10-4")

# تعليمات الدفع التي تظهر للعملاء في صفحة الاشتراك (حساب بنكي، رقم تواصل...)
SYNAR_BILLING_INSTRUCTIONS = os.environ.get(
    "SYNAR_BILLING_INSTRUCTIONS",
    "للاشتراك أو التجديد: حوّل قيمة الباقة إلى حسابنا البنكي ثم أرسل الإيصال، وسيتم تفعيل اشتراكك خلال يوم عمل.",
)
SYNAR_SUPPORT_CONTACT = os.environ.get("SYNAR_SUPPORT_CONTACT", "")

# البريد الإلكتروني للتنبيهات: معطّل حتى يُضبط خادم بريد (لا اتصال خارجي افتراضياً)
SYNAR_EMAIL_ENABLED = bool(os.environ.get("SYNAR_EMAIL_HOST"))
if SYNAR_EMAIL_ENABLED:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = os.environ["SYNAR_EMAIL_HOST"]
    EMAIL_PORT = int(os.environ.get("SYNAR_EMAIL_PORT", "587"))
    EMAIL_HOST_USER = os.environ.get("SYNAR_EMAIL_USER", "")
    EMAIL_HOST_PASSWORD = os.environ.get("SYNAR_EMAIL_PASSWORD", "")
    EMAIL_USE_TLS = os.environ.get("SYNAR_EMAIL_TLS", "1") == "1"
    DEFAULT_FROM_EMAIL = os.environ.get("SYNAR_EMAIL_FROM", EMAIL_HOST_USER)
else:
    EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"
SYNAR_SITE_URL = os.environ.get("SYNAR_SITE_URL", "")
# خلف وكيل عكسي (Caddy في الإنتاج) يُؤخذ عنوان العميل من X-Forwarded-For؛ بدونه يُتجاهل لأنه قابل للتزوير
SYNAR_TRUST_PROXY = os.environ.get("SYNAR_TRUST_PROXY", "0" if DEBUG else "1") == "1"

# ───────── الأمان في الإنتاج ─────────
if not DEBUG:
    # يفترض أن النظام خلف وكيل عكسي (Caddy/Nginx) يتولى شهادة HTTPS
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = os.environ.get("SYNAR_SSL_REDIRECT", "1") == "1"
    SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = SECURE_SSL_REDIRECT
    SECURE_HSTS_SECONDS = int(os.environ.get("SYNAR_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = os.environ.get("SYNAR_HSTS_SUBDOMAINS", "0") == "1"
    SECURE_HSTS_PRELOAD = False
    # قرار يخص مالك النطاق: تشمل HSTS النطاقات الفرعية فقط إن كانت كلها على HTTPS، والإدراج في قائمة المتصفحات اختياري
    SILENCED_SYSTEM_CHECKS = ["security.W021"] + ([] if SECURE_HSTS_INCLUDE_SUBDOMAINS else ["security.W005"])
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"
    X_FRAME_OPTIONS = "DENY"
    # مسار فحص الصحة يُطلب داخلياً عبر HTTP من Docker
    # أجهزة البصمة كثير منها يرسل عبر HTTP فقط ولا يتبع التحويل إلى HTTPS
    SECURE_REDIRECT_EXEMPT = [r"^healthz/$", r"^iclock/"]
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_AGE = int(os.environ.get("SYNAR_SESSION_HOURS", "12")) * 3600

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": os.environ.get("SYNAR_LOG_LEVEL", "INFO")},
    "loggers": {"django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False}},
}

# مجلد النسخ الاحتياطية (للقراءة فقط) لعرض حالتها في لوحة إدارة المنصة
SYNAR_BACKUP_DIR = os.environ.get("SYNAR_BACKUP_DIR", str(BASE_DIR / "backups"))
