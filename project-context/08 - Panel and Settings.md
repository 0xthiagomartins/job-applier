# Settings and State

## Local Settings Responsibilities

The local settings document is the operational control plane for the local user.

It controls:

- profile information
- uploaded CV
- canonical resume source snapshot lifecycle
- resume mode
- default content language
- search targets
- capability profile overrides
- runtime triggers and history views

There is no active frontend application today; this state is persisted locally and accessed through the backend/runtime.

## Important User Settings

### Profile

- name
- phone
- email
- city
- LinkedIn URL
- GitHub URL
- portfolio URL
- work authorization
- sponsorship
- availability
- salary expectation

### Resume

- CV path / uploaded CV
- persisted resume source snapshot derived from that CV
- `resume_mode`
- `preferred_language`
- `resume_css`

### Search

- role targets / keywords
- location
- positive filters

## Current Local Role Target Order

The current local file prioritizes Brazilian targets first:

1. `Desenvolvedor RPA`
2. `Desenvolvedor Full Stack`
3. `Desenvolvedor de Automação`
4. `Desenvolvedor Backend`

## Persistence Locations

- local settings document and local CV copies:
  - `artifacts/runtime/panel/`
- local runtime database:
  - `artifacts/runtime/job-applier.db`

## Snapshot Endpoints

The backend now exposes snapshot-oriented endpoints:

- `GET /api/panel/resume-source-snapshot`
- `POST /api/panel/resume-source-snapshot/refresh`
- `PUT /api/panel/resume-source-snapshot`

These are the current integration points for inspecting or overriding the canonical snapshot without changing the raw CV file.

## Important Sharing Warning

Before sharing the project with another harness or person, review:

- local settings state
- copied CVs
- any saved session or runtime file

Do not blindly ship `artifacts/runtime/`.
