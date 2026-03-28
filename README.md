# Job Applier

[![CI](https://github.com/0xthiagomartins/job-applier/actions/workflows/ci.yml/badge.svg)](https://github.com/0xthiagomartins/job-applier/actions/workflows/ci.yml)

Job Applier is an open source system for assisted job application automation with strong submission auditability.

The current repository bootstrap already includes:

- Python 3.14 project management with `uv`
- FastAPI application skeleton with a health check endpoint
- Ruff, mypy, pytest and pre-commit configuration
- GitHub Actions CI for lint, type-check and tests

## Getting started

1. Install Python 3.14 and `uv`.
2. Sync the environment:

   ```bash
   uv sync --all-groups
   ```

3. Install the git hooks:

   ```bash
   uv run pre-commit install
   ```

4. Start the bootstrap app locally:

   ```bash
   uv run uvicorn job_applier.main:app --reload
   ```

5. Check the health endpoint:

   ```bash
   curl http://127.0.0.1:8000/health
   ```

## Quality commands

Run lint:

```bash
uv run ruff check .
uv run ruff format --check .
```

Run type-check:

```bash
uv run mypy src tests
```

Run tests:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest
```

## Contributing

Contribution guidelines live in [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

Security reporting details live in [SECURITY.md](SECURITY.md).

## Code of conduct

Community expectations live in [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
