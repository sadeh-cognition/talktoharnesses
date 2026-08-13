.PHONY: backend

backend:
	DJANGO_SETTINGS_MODULE=host.settings uv run uvicorn host.asgi:application --host 127.0.0.1 --port 8010
