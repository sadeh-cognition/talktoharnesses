from pathlib import Path

from talktoharnesses.django.http_logging import configure_logging

configure_logging()

BASE_DIR = Path(__file__).resolve().parent

SECRET_KEY = "local-talktoharnesses-host-secret-key-not-for-production"
TALKTOHARNESSES_JWT_SIGNING_KEY = "local-talktoharnesses-jwt-signing-key-32b"

DEBUG = True
USE_TZ = True
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
ROOT_URLCONF = "host.urls"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "auth.User"
MIDDLEWARE: list[str] = [
    "talktoharnesses.django.http_logging.RequestResponseLoggingMiddleware",
]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "talktoharnesses.django",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(BASE_DIR / "db.sqlite3"),
        "OPTIONS": {"transaction_mode": "IMMEDIATE"},
    }
}
