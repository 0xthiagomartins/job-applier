# File Map

## Top-Level Areas

- `src/`: backend application code
- `scripts/`: operational scripts
- `docs/`: repo-facing docs
- `artifacts/`: runtime and debug outputs
- `project-context/`: this Obsidian handoff vault

## Backend Files To Know First

### Orchestration

- `src/job_applier/application/agent_execution.py`
- `src/job_applier/application/agent_scheduler.py`

### Scoring

- `src/job_applier/application/job_scoring.py`

### Settings and config

- `src/job_applier/settings.py`
- `src/job_applier/application/config.py`

### LinkedIn search and apply

- `src/job_applier/infrastructure/linkedin/search.py`
- `src/job_applier/infrastructure/linkedin/easy_apply.py`
- `src/job_applier/infrastructure/linkedin/question_resolution.py`

### Dynamic resume and language support

- `src/job_applier/infrastructure/resume_dynamic.py`
- `src/job_applier/infrastructure/language_support.py`
- `src/job_applier/infrastructure/candidate_capabilities.py`

### Persistence

- `src/job_applier/infrastructure/sqlite/repositories.py`
- `src/job_applier/infrastructure/sqlite/models.py`
- `alembic/versions/`

### HTTP composition

- `src/job_applier/interface/http/dependencies.py`

## Scripts

- `scripts/generate_mock_dynamic_resumes.py`
- `scripts/audit_dynamic_resume.py`

## Runtime State

- `artifacts/runtime/job-applier.db`
- `artifacts/runtime/panel/settings.json`
- `artifacts/runtime/artifacts/linkedin/submissions/...`
- `artifacts/last-run/`
