.PHONY: lint serve

SERVE_HOST := 127.0.0.1
SERVE_PORT := 8115

lint:
	uv run pyright
	uv run ruff check .
	uv run isort --check-only src

serve:
	DJANGO_SETTINGS_MODULE=tth_opencode.settings uv run uvicorn tth_opencode.asgi:application --host $(SERVE_HOST) --port $(SERVE_PORT)
