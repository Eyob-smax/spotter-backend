"""Settings for the test suite: deterministic, offline, in-memory cache."""

import os

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("TOMTOM_API_KEY", "test-tomtom-key")
os.environ["REDIS_URL"] = ""

from .settings import *  # noqa: E402,F403

TOMTOM_API_KEY = "test-tomtom-key"
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "tests"}}
LOGGING["root"]["level"] = "WARNING"  # noqa: F405
