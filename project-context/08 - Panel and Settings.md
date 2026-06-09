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
- private metadata stays separate from this snapshot

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

## Private Metadata

The backend now supports a separate private-metadata section for user-supplied factual or sensitive fields that are not safe to infer from the CV.

Examples:

- `CPF`
- `RG`
- `Nome do pai`
- `Nome da mãe`
- `Data de nascimento`
- `Empresa atual`
- `Salário atual/último`
- `Benefícios atuais/últimos`

Current endpoints:

- `GET /api/panel/private-metadata`
- `PUT /api/panel/private-metadata`

Important behavior:

- this metadata is stored separately from the canonical resume snapshot
- the user must explicitly consent before this metadata can be sent to OpenAI
- panel state exposes only a safe summary, not the raw metadata block

Current safe state summary now includes:

- `entry_count`
- `stored_keys`
- `stored_labels`
- `consent_to_ai_usage`
- `ai_usage_warning`
- `parse_error`

Current aggregated feedback now also includes:

- `missing_fields`
- `configured_missing_fields`
- `missing_unconfigured_fields`
- `consent_required_for_ai_usage`
- `suggested_raw_text_template`
- `next_action`

Important nuance:

- the feedback is cumulative from recent skipped submissions
- but it is also re-evaluated against the current private metadata state on every `GET /api/panel/state`
- so a field that failed in the past can later appear as already configured, even before a new apply run proves the fix end to end

## Important Sharing Warning

Before sharing the project with another harness or person, review:

- local settings state
- copied CVs
- any saved session or runtime file

Do not blindly ship `artifacts/runtime/`.
