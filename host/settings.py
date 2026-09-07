import os
from pathlib import Path

from dotenv import load_dotenv

from talktoharnesses.django.http_logging import configure_logging

BASE_DIR = Path(__file__).resolve().parent


def _resolve_env_file() -> Path:
    """Prefer an env file outside the repo tree, shared with agentbahn, so a
    sandboxed harness that mounts this checkout never sees host secrets."""
    override = os.environ.get("TTH_ENV_FILE")
    if override:
        return Path(override).expanduser()
    user_env = Path("~/.config/agentbahn/env").expanduser()
    if user_env.is_file():
        return user_env
    return BASE_DIR.parent / ".env"


# Optional: containerized instances (see deploy/) pass everything via the
# process environment and have no file to load. Existing variables win.
ENV_FILE_PATH = _resolve_env_file()
load_dotenv(ENV_FILE_PATH)

configure_logging(log_file=os.environ.get("TTH_LOG_FILE"))

# Containerized instances (see deploy/) override these per instance via env;
# the bare defaults keep the local single-host dev flow working.
SECRET_KEY = os.environ.get(
    "TTH_SECRET_KEY", "local-talktoharnesses-host-secret-key-not-for-production"
)
TALKTOHARNESSES_JWT_SIGNING_KEY = os.environ.get(
    "TALKTOHARNESSES_JWT_SIGNING_KEY", "local-talktoharnesses-jwt-signing-key-32b"
)

DEBUG = os.environ.get("TTH_DEBUG", "1") == "1"
USE_TZ = True
# Django's default TIME_ZONE (America/Chicago) is applied to the process clock
# at setup, which stamped log lines five hours behind the UTC timestamps in
# agentbahn's records and the harness journals. Keep every log in UTC.
TIME_ZONE = "UTC"
ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("TTH_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")
    if host.strip()
]
ROOT_URLCONF = "host.urls"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "auth.User"
MIDDLEWARE: list[str] = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "talktoharnesses.django.http_logging.RequestResponseLoggingMiddleware",
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "talktoharnesses.django",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

STATIC_URL = "static/"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("TTH_DB_PATH", str(BASE_DIR / "db.sqlite3")),
        "OPTIONS": {"transaction_mode": "IMMEDIATE"},
    }
}
