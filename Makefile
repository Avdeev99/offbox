.PHONY: fmt check test

fmt:
	uv run --frozen ruff format
	uv run --frozen ruff check --fix

check:
	uv run --frozen ruff format --check
	uv run --frozen ruff check
	uv run --frozen mypy
	uv run --frozen pyright

test:
	uv run --frozen pytest -m "not subprocess"
