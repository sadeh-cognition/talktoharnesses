.PHONY: lint

lint:
	uv run pyright
	uv run ruff check .
	uv run isort --check-only src
