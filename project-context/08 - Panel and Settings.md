# Settings and State

## Local Settings Responsibilities

The local settings document is the operational control plane for the local user.

It controls:

- profile information
- uploaded CV
- resume mode
- default content language
- search targets
- capability profile overrides
- runtime triggers and history views

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
- `resume_mode`
- `preferred_language`
- `resume_css`

### Search

- role targets / keywords
- location
- positive filters

## Current Local Role Target Order

The current local file prioritizes Brazilian targets first:

1. `Engenheiro de Software`
2. `Desenvolvedor Full Stack`
3. `Desenvolvedor Backend`
4. `Desenvolvedor de Software`
5. `Engenheiro de Automação`
6. `Desenvolvedor de Automação`
7. `Desenvolvedor RPA`
8. `Software Engineer`
9. `Full Stack Developer`
10. `Backend Developer`
11. `Software Developer`
12. `Automation Engineer`
13. `Automation Developer`
14. `RPA Developer`

## Persistence Locations

- local settings document and local CV copies:
  - `artifacts/runtime/panel/`
- local runtime database:
  - `artifacts/runtime/job-applier.db`

## Important Sharing Warning

Before sharing the project with another harness or person, review:

- local settings state
- copied CVs
- any saved session or runtime file

Do not blindly ship `artifacts/runtime/`.
