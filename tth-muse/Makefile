.PHONY: lint serve

SERVE_HOST := 127.0.0.1
SERVE_PORT := 8117

lint:
	uv run pyright
	uv run ruff check .
	uv run isort --check-only src

serve:
	DJANGO_SETTINGS_MODULE=tth_muse.settings uv run uvicorn tth_muse.asgi:application --host $(SERVE_HOST) --port $(SERVE_PORT)
