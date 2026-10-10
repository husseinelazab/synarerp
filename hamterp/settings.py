"""إعدادات نظام همت ERP."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("HAMTERP_SECRET_KEY", "dev-insecure-change-me")
DEBUG = os.environ.get("HAMTERP_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("HAMTERP_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

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
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "hamterp.urls"

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

WSGI_APPLICATION = "hamterp.wsgi.application"

if os.environ.get("HAMTERP_DB_NAME"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ["HAMTERP_DB_NAME"],
            "USER": os.environ.get("HAMTERP_DB_USER", ""),
            "PASSWORD": os.environ.get("HAMTERP_DB_PASSWORD", ""),
            "HOST": os.environ.get("HAMTERP_DB_HOST", "localhost"),
            "PORT": os.environ.get("HAMTERP_DB_PORT", "5432"),
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
TIME_ZONE = os.environ.get("HAMTERP_TIME_ZONE", "Asia/Riyadh")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

# اسم النظام المعروض في الواجهة، ويمكن تغييره دون تعديل الكود
ERP_NAME = os.environ.get("HAMTERP_NAME", "همت ERP")
