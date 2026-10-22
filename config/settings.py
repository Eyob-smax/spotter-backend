"""
Django settings. Everything environment-specific is read from env vars
(see .env.example); safe defaults are for local development only.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


DEBUG = env_bool("DEBUG", False)
SECRET_KEY = os.getenv("SECRET_KEY") or ("dev-insecure-key" if DEBUG else "")
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY must be set when DEBUG is off.")

# localhost stays allowed so the container healthcheck can reach /api/health.
ALLOWED_HOSTS = [*env_list("ALLOWED_HOSTS"), "localhost", "127.0.0.1"]
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "drf_spectacular",
    "planner",
]

MIDDLEWARE = [
    "django.middleware.gzip.GZipMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "planner.api.middleware.RequestIdMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": ["django.template.context_processors.request"]},
    },
]

# The app has no persistent models: all state lives in the cache.
DATABASES: dict = {}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Cache (Redis in docker/prod, in-process memory otherwise) -------------
REDIS_URL = os.getenv("REDIS_URL")
if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
            "KEY_PREFIX": "spotter",
            "TIMEOUT": 3600,
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "spotter",
            "TIMEOUT": 3600,
            "OPTIONS": {"MAX_ENTRIES": 5000},
        }
    }

# --- CORS -------------------------------------------------------------------
# Browsers may call the API only from these origins. Set CORS_ALLOWED_ORIGINS to override.
DEFAULT_CORS_ORIGINS = [
    "https://spotter-ai-navy.vercel.app",  # production frontend (Vercel)
    "http://localhost:5173",  # local Vite dev server
    "http://127.0.0.1:5173",
]
CORS_ALLOWED_ORIGINS = env_list("CORS_ALLOWED_ORIGINS") or DEFAULT_CORS_ORIGINS
CORS_ALLOW_CREDENTIALS = False

# --- Django REST framework -----------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": [],
    "UNAUTHENTICATED_USER": None,
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "planner.api.errors.exception_handler",
    "DEFAULT_THROTTLE_CLASSES": [],
    "DEFAULT_THROTTLE_RATES": {
        "routes": os.getenv("THROTTLE_ROUTES", "12/min"),
        "analysis": os.getenv("THROTTLE_ANALYSIS", "30/min"),
        "heatmap": os.getenv("THROTTLE_HEATMAP", "30/min"),
        "geocode": os.getenv("THROTTLE_GEOCODE", "120/min"),
    },
    # Number of trusted proxies in front of the app (the server's nginx = 1), so throttling
    # keys on the real client IP from X-Forwarded-For.
    "NUM_PROXIES": int(os.getenv("NUM_PROXIES")) if os.getenv("NUM_PROXIES") else None,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Spotter Route API",
    "DESCRIPTION": "Weather-aware truck routing: compare 3 truck routes against the forecast at the truck's ETA.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

# --- Third-party APIs ---------------------------------------------------------------
TOMTOM_API_KEY = os.getenv("TOMTOM_API_KEY", "")
TOMTOM_BASE_URL = os.getenv("TOMTOM_BASE_URL", "https://api.tomtom.com")
OPEN_METEO_URL = os.getenv("OPEN_METEO_URL", "https://api.open-meteo.com/v1/forecast")
# Optional: a paid Open-Meteo key (use with OPEN_METEO_URL=https://customer-api.open-meteo.com/v1/forecast).
OPEN_METEO_API_KEY = os.getenv("OPEN_METEO_API_KEY", "")

# --- Security (effective when DEBUG is off, behind an HTTPS proxy) -------------
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
X_FRAME_OPTIONS = "DENY"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
# TLS is terminated by the server's nginx (certbot), which also redirects HTTP to HTTPS.
# CSRF middleware is unnecessary: the API is stateless (no cookies or sessions).
SECURE_SSL_REDIRECT = False
SILENCED_SYSTEM_CHECKS = ["security.W003", "security.W004", "security.W008"]

# --- Logging ------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"request_id": {"()": "planner.api.middleware.RequestIdFilter"}},
    "formatters": {
        "plain": {"format": "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain", "filters": ["request_id"]},
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "urllib3": {"level": "WARNING"},
    },
}
