# Validation and Operations

## Primary Local Checks

Python:

```bash
uv run ruff check .
uv run mypy src
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
uv run python scripts/generate_mock_dynamic_resumes.py --offline
```

Audit a resume:

```bash
uv run python scripts/audit_dynamic_resume.py \
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
