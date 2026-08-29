import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "host.settings")

import django  # noqa: E402

# Importing settings runs configure_logging(), whose logger.remove() would
# strip the OTel loguru sink if telemetry were configured first.
django.setup()

from host.telemetry import configure_opentelemetry, instrument_django  # noqa: E402

configure_opentelemetry()
instrument_django()

from django.core.asgi import get_asgi_application  # noqa: E402

django_app = get_asgi_application()

from talktoharnesses.django.asgi import talktoharnesses_lifespan  # noqa: E402

application = talktoharnesses_lifespan(django_app)
