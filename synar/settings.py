"""إعدادات نظام سِينار (Synar ERP)."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("SYNAR_SECRET_KEY", "dev-insecure-change-me")
DEBUG = os.environ.get("SYNAR_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("SYNAR_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
CSRF_TRUSTED_ORIGINS = [o for o in os.environ.get("SYNAR_CSRF_TRUSTED_ORIGINS", "").split(",") if o]

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
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
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
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
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

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

# اسم النظام المعروض في الواجهة
ERP_NAME = "سِينار"
ERP_NAME_EN = "Synar ERP"

# تعليمات الدفع التي تظهر للعملاء في صفحة الاشتراك (حساب بنكي، رقم تواصل...)
SYNAR_BILLING_INSTRUCTIONS = os.environ.get(
    "SYNAR_BILLING_INSTRUCTIONS",
    "للاشتراك أو التجديد: حوّل قيمة الباقة إلى حسابنا البنكي ثم أرسل الإيصال، وسيتم تفعيل اشتراكك خلال يوم عمل.",
)
SYNAR_SUPPORT_CONTACT = os.environ.get("SYNAR_SUPPORT_CONTACT", "")
