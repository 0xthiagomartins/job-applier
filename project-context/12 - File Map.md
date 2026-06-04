# File Map

## Top-Level Areas

- `apps/backend/`: backend application code split by responsibility
- `apps/frontend/`: frontend placeholder
- `artifacts/`: runtime and debug outputs
- `project-context/`: this Obsidian handoff vault

## Backend Files To Know First

### Orchestration

- `apps/backend/execution-worker/src/job_applier/application/agent_execution.py`
- `apps/backend/execution-worker/src/job_applier/application/agent_scheduler.py`

### Scoring

- `apps/backend/execution-worker/src/job_applier/application/job_scoring.py`

### Settings and config

- `apps/backend/shared/src/job_applier/settings.py`
- `apps/backend/shared/src/job_applier/application/config.py`

### LinkedIn search and apply

- `apps/backend/execution-worker/src/job_applier/infrastructure/linkedin/search.py`
- `apps/backend/execution-worker/src/job_applier/infrastructure/linkedin/easy_apply.py`
- `apps/backend/execution-worker/src/job_applier/infrastructure/linkedin/question_resolution.py`

### Dynamic resume and language support

- `apps/backend/resume-worker/src/job_applier/infrastructure/resume_dynamic.py`
- `apps/backend/shared/src/job_applier/infrastructure/language_support.py`
- `apps/backend/shared/src/job_applier/infrastructure/candidate_capabilities.py`

### Persistence

- `apps/backend/shared/src/job_applier/infrastructure/sqlite/repositories.py`
- `apps/backend/shared/src/job_applier/infrastructure/sqlite/models.py`
- `alembic/versions/`

New persistence to know:

- `resume_source_snapshots` table in SQLite
- owner-scoped by `owner_key`
- matched to the uploaded base CV by `cv_sha256`

### HTTP composition

- `apps/backend/control-api/src/job_applier/interface/http/dependencies.py`
- `apps/backend/control-api/src/job_applier/interface/http/routes/panel.py`

## Scripts

- `apps/backend/resume-worker/src/job_applier/tools/generate_mock_dynamic_resumes.py`
- `apps/backend/resume-worker/src/job_applier/tools/audit_dynamic_resume.py`

## Runtime State

- `artifacts/runtime/job-applier.db`
- `artifacts/runtime/panel/settings.json`
- `artifacts/runtime/artifacts/linkedin/submissions/...`
- `artifacts/last-run/`

Snapshot-related runtime state:

- canonical base-resume snapshot rows now live in `artifacts/runtime/job-applier.db`
