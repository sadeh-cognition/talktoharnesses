import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "host.settings")

from django.core.asgi import get_asgi_application

django_app = get_asgi_application()

from talktoharnesses.django.asgi import talktoharnesses_lifespan  # noqa: E402

application = talktoharnesses_lifespan(django_app)
