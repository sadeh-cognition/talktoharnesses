.PHONY: lint

lint:
	uv run pyrefly check
	uv run ruff check .
	uv run isort --check-only src
