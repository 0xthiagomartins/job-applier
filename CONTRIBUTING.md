# Contributing

Thanks for contributing to Job Applier.

## Development setup

1. Install Python 3.14 and `uv`.
2. Run `uv sync --all-groups`.
3. Run `uv run pre-commit install`.

## Local quality checks

- `uv run ruff check .`
- `uv run ruff format --check .`
- `uv run mypy src tests`
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest`

## Pull request expectations

- Keep changes small and intentional.
- Link the issue being addressed.
- Explain what changed and how it was verified.
- Prefer incremental commits over large unrelated bundles.

## Testing policy

This project prefers a lean test suite:

- write few tests, but make them high-value;
- prefer real flow coverage over excessive mocking;
- mock only when dealing with external boundaries or unavoidable nondeterminism;
- avoid tests that only validate implementation details.
