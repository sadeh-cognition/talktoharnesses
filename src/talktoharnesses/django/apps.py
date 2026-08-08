from django.apps import AppConfig


class TalkToHarnessesConfig(AppConfig):
    """Django AppConfig for the talktoharnesses application.

    ``ready()`` must not create an event loop, database connection, worker,
    adapter, or subprocess. Service lifecycle belongs to the ASGI lifespan
    wrapper (``talktoharnesses.django.asgi.talktoharnesses_lifespan``).
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "talktoharnesses.django"
    label = "talktoharnesses"
    verbose_name = "Talk To Harnesses"

    def ready(self) -> None:
        # Intentionally empty of workers / adapters / connections.
        return
