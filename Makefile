.PHONY: backend lint

BACKEND_HOST := 127.0.0.1
BACKEND_PORT := 8010
BACKEND_URL := http://$(BACKEND_HOST):$(BACKEND_PORT)

lint:
	uv run pyright
	uv run ruff check .
	uv run isort --check-only src

backend:
	@if curl --fail --silent --max-time 1 $(BACKEND_URL)/api/v1/health >/dev/null; then \
		echo "Backend is already running at $(BACKEND_URL)"; \
	else \
		DJANGO_SETTINGS_MODULE=host.settings uv run uvicorn host.asgi:application --host $(BACKEND_HOST) --port $(BACKEND_PORT); \
	fi
