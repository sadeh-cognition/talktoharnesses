.PHONY: lint serve

SERVE_HOST := 127.0.0.1
SERVE_PORT := 8114

lint:
	uv run pyright
	uv run ruff check .
	uv run isort --check-only src

serve:
	DJANGO_SETTINGS_MODULE=tth_claude.settings uv run uvicorn tth_claude.asgi:application --host $(SERVE_HOST) --port $(SERVE_PORT)
