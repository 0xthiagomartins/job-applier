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
- stop on the first OpenAI `429` in production
- avoid forcing raw CV reprocessing when validating dynamic resume changes that can be inspected through the persisted snapshot

Current recommended low-cost suite:

1. `4418597669` `Jobgether` `Engenheiro de Software Pl. (Java)` `PT`
2. `4422383527` `CI&T` `Senior Java/Kotlin Backend Developer, Brazil` `PT`
3. `4420980277` `CI&T` `Senior Java Developer, Brazil` `EN`

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
7. whether the run halted on an OpenAI `429` instead of retrying indefinitely
8. whether the canonical resume snapshot is stale or user-edited
