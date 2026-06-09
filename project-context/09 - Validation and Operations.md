# Validation and Operations

## Primary Local Checks

Python:

```bash
uv run ruff check .
uv run mypy apps/backend
```

Targeted unit tests when touching apply memory or answer generation:

```bash
uv run --no-sync python -m unittest discover -s apps/backend/execution-worker/tests -p 'test_*.py'
```

## Artifact Review

Latest run bundle:

- `artifacts/last-run/progress.json`
- `artifacts/last-run/summary.json`
- `artifacts/last-run/timeline.jsonl`
- `artifacts/last-run/run.log`

Cost observability is now embedded directly into run artifacts:

- `summary.json`
  - `cost.openai.calls_total`
  - `cost.openai.by_category`
  - `cost.openai.latency_ms_total`
  - `cost.openai.tokens`
  - `cost.openai.rate_limit_count`
  - `cost.openai.failure_count`
  - `cost.efficiency.apply_memory.*`
  - `cost.efficiency.search_cache.*`
  - `cost.efficiency.resume_snapshot.*`
- `timeline.jsonl`
  - `openai_cost_recorded`
  - `cost_efficiency_recorded`

Durable evidence:

- `artifacts/runtime/artifacts/linkedin/submissions/...`

## Dynamic Resume Review

Generate mock scenarios:

```bash
uv run --all-packages python -m job_applier.tools.generate_mock_dynamic_resumes --offline
```

Audit a resume:

```bash
uv run --all-packages python -m job_applier.tools.audit_dynamic_resume \
  --submission-dir artifacts/runtime/artifacts/linkedin/submissions/<submission-dir> \
  --job-title "Backend Engineer (Python / JavaScript)"
```

## Useful Operational Modes

Staged debugging:

- `search`
- `score`
- `apply`
- `full`

Direct target mode:

- `JOB_APPLIER_LINKEDIN_DEBUG_TARGET_JOB_URL=...`

Low-cost debugging mode:

- `JOB_APPLIER_AGENT_TEST_MODE=true`

Low-cost validation strategy:

- prefer direct target mode over broad search
- prefer a fixed 3-job suite over large production rounds
- when broad `full` validation is needed, reuse the built-in 1-hour search+score cache instead of rerunning the same campaigns cold
- stop on the first OpenAI `429` in production
- avoid forcing raw CV reprocessing when validating dynamic resume changes that can be inspected through the persisted snapshot

Search+score cache behavior:

- scope: repeated `full`-stage runs with the same target inputs
- storage: `artifacts/runtime/cache/search-score/`
- ttl: `1 hour`
- reuse point: after campaign search/pagination and after rule-based scoring, before apply
- expected effect: repeated validation runs should skip browser search work and skip scorer recomputation for cached postings

Current recommended low-cost suite:

1. `4418597669` `Jobgether` `Engenheiro de Software Pl. (Java)` `PT`
2. `4422383527` `CI&T` `Senior Java/Kotlin Backend Developer, Brazil` `PT`
3. `4420980277` `CI&T` `Senior Java Developer, Brazil` `EN`

## Sensitive Metadata Validation

The backend now supports a separate private-metadata flow for factual or sensitive fields that the agent cannot infer safely.

Validation checklist:

1. with no private metadata configured, unresolved factual fields should skip safely
2. `GET /api/panel/state` should report aggregated `missing_private_metadata` feedback without exposing raw values
3. `GET /api/panel/private-metadata` is the only route that may expose the raw user-managed block
4. when consent is enabled and matching metadata exists, the Easy Apply resolver may use OpenAI with only the relevant metadata subset for the current field
5. snapshots and panel state summaries must not contain the raw metadata values
6. `GET /api/panel/state` should distinguish:
   - fields still missing
   - fields already configured in private metadata
   - cases where consent is still blocking AI usage
7. the suggested raw-text template should only mention unconfigured fields

## What “Good” Looks Like

A healthy beta run should:

- search the intended role family
- score with understandable reasons
- skip unsupported jobs early
- generate the correct resume mode
- open and traverse `Easy Apply`
- either submit successfully or skip with a clear diagnosis

## Common Things To Inspect First

When something looks wrong:

1. `timeline.jsonl`
2. the markdown dynamic resume artifact
3. the rendered PDF
4. the entrypoint assessment events
5. scorer output and matched role target
6. any `apply_memory_*` events
7. any `linkedin_search_campaign_cache_*` or `job_score_cache_*` events
8. whether the run halted on an OpenAI `429` instead of retrying indefinitely
9. whether the canonical resume snapshot is stale or user-edited
10. whether `summary.json.cost` shows the expected cache/memory hits and OpenAI call categories
