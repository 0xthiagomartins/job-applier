# Testing Strategy

The test suite for Job Applier is intentionally small and risk-oriented.

What we prioritize:

- real business decisions instead of implementation trivia
- real SQLite integration instead of mock-heavy repository tests
- 1 high-value controlled end-to-end flow that proves the product works
- stability and reproducibility in CI

What we avoid:

- asserting internal calls and framework plumbing
- broad mock pyramids around code that is already deterministic
- duplicate tests for the same rule at multiple levels
- coverage chasing without product value

Suite shape:

- `unit`: core rules only
  - vacancy scoring
  - question classification
  - answer resolution priority chain
  - scheduling behavior
  - recruiter message generation
  - submission gating rules
- `integration`: SQLite real
  - CRUD and history queries
  - successful-submission persistence only
  - artifacts, recruiter interactions and execution events
  - referential integrity and repository contracts
- `e2e`: controlled environment
  - panel configuration
  - manual execution trigger
  - job fetch, qualify, submit and persist
  - later history lookup with artifacts and audit data

Run locally:

```bash
uv run ruff check .
uv run mypy src tests
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest
cd apps/panel && npm run typecheck && npm run build
```

Rule of thumb:

- if a test does not protect a real product risk, it probably should not exist
- if a test needs too many mocks, prefer moving it up one level
