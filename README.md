# Job Applier

[![CI](https://github.com/0xthiagomartins/job-applier/actions/workflows/ci.yml/badge.svg)](https://github.com/0xthiagomartins/job-applier/actions/workflows/ci.yml)

Job Applier is an open source system for assisted job application automation with strong submission auditability.

The current repository bootstrap already includes:

- Python 3.14 project management with `uv`
- FastAPI backend API with panel configuration endpoints
- Next.js + TypeScript panel in `apps/panel` with shadcn-style components
- Ruff, mypy, pytest and pre-commit configuration
- GitHub Actions CI for lint, type-check and tests
- single-container on-premise runtime path with local SQLite fallback
- automatic Alembic upgrade to the latest schema on startup

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

4. Start the backend API locally:

   ```bash
   uv run uvicorn job_applier.main:app --reload
   ```

5. Install panel dependencies:

   ```bash
   cd apps/panel
   npm install
   ```

6. Start the panel locally:

   ```bash
   npm run dev
   ```

7. Check the backend health endpoint:

   ```bash
   curl http://127.0.0.1:8000/health
   ```

8. Open the panel at `http://127.0.0.1:3000`.

## LinkedIn search setup

The LinkedIn Jobs search automation reads credentials from local runtime config, never from versioned code.

Add these keys to your local `.env`:

```bash
JOB_APPLIER_LINKEDIN_EMAIL="you@example.com"
JOB_APPLIER_LINKEDIN_PASSWORD="your-linkedin-password"
JOB_APPLIER_PLAYWRIGHT_HEADLESS=false
```

Runtime behavior:

- the first successful login saves a reusable session in `artifacts/runtime/linkedin/storage-state.json`;
- later runs reuse that storage state automatically;
- if LinkedIn expires the session, the app clears the saved state and logs in again;
- in headful mode, the browser stays visible so the user can solve captcha or checkpoint screens.
- when the panel state is still empty, the app bootstraps a local profile automatically from `.env` and tries to import a CV from `~/Documents`.

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

Frontend checks:

```bash
cd apps/panel
npm run typecheck
npm run build
```

The lean test philosophy for this repo lives in [docs/testing-strategy.md](docs/testing-strategy.md).

## On-premise container

The repository already includes a single `Dockerfile` for the on-premise flow:

- backend API and panel run inside the same container;
- if `JOB_APPLIER_DATABASE_URL` is not provided, the app creates and uses a local SQLite file in `/data`;
- for Linux hosts, Playwright can open a visible browser on the host display so the user can watch the automation and step in for captchas.

Build and run details live in [docs/on-premise.md](docs/on-premise.md).

## Contributing

Contribution guidelines live in [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

Security reporting details live in [SECURITY.md](SECURITY.md).

## Code of conduct

Community expectations live in [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
