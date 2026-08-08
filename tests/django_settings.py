SECRET_KEY = "test-not-for-production"
USE_TZ = True
ROOT_URLCONF = "tests.django_urls"
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "talktoharnesses.django",
]
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "auth.User"
# Distinct from SECRET_KEY; >= 32 bytes.
TALKTOHARNESSES_JWT_SIGNING_KEY = "test-jwt-signing-key-32-bytes-min!!"
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
MIDDLEWARE: list[str] = []
