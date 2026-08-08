SECRET_KEY = "test-not-for-production"
USE_TZ = True
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "talktoharnesses.django",
]
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
