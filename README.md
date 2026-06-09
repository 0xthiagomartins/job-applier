# Job Applier

Job Applier is an internal-beta LinkedIn `Easy Apply` automation system with:

- multi-target job search
- explicit `static` and `dynamic` resume modes
- English-first defaults with job-language-aware resume targeting for supported languages
- competitive-but-plausible screening answers grounded in a real base CV
- strong artifact capture for debugging, auditing, and resume review

## What the product is today

The current product flow is:

1. upload one truthful base CV
2. configure broad role targets such as `Desenvolvedor RPA`, `Desenvolvedor Full Stack`, `Desenvolvedor de Automação`, or `Desenvolvedor Backend`
3. let the system search LinkedIn `Easy Apply` jobs across those target families
4. score each job against the configured targets
5. either:
   - apply with the base CV unchanged in `static` mode
   - generate a job-specific CV variant in `dynamic` mode without changing the base identity or inventing experience
6. answer screening questions with exact profile facts when available, or with plausible competitive ranges inferred from the base CV
7. persist logs, timelines, screenshots, HTML dumps, and generated resume artifacts for auditability

## What the MVP supports

- LinkedIn job search with broad multi-target role families
- direct `Easy Apply` execution with staged debugging modes
- reviewed capability inference from the base CV
- dynamic resume generation with CSS-backed PDF rendering
- supported language-aware resume localization metadata in submission history
- local artifact auditing for dynamic resumes
- local runtime with FastAPI backend and SQLite persistence

The current repository already includes:

- Python 3.14 project management with `uv`
- FastAPI backend API
- Ruff, mypy and pre-commit configuration
- local SQLite runtime path
- resume capability inference and review UI
- dynamic resume audit tooling

## What the MVP does not promise yet

- perfect success on every LinkedIn `Easy Apply` variation
- support for boards outside LinkedIn
- fully visual resume theme editing in the panel
- high-confidence stack tailoring when the target stack is not grounded in the base CV
- arbitrary resume translation without an available AI localization path

## Core concepts

### Resume modes

- `static`: always apply with the uploaded base CV
- `dynamic`: generate a per-job resume variant while preserving the base CV identity

### Role targets

Targets should be broad role families, not narrow stack-specific job titles.

Recommended targets:

- `Desenvolvedor RPA`
- `Desenvolvedor Full Stack`
- `Desenvolvedor de Automação`
- `Desenvolvedor Backend`

Use stack cues such as `Python`, `AWS`, `TypeScript`, `JavaScript`, `UiPath`, or `LangChain` as scoring/tailoring signals, not as the primary job family.

### Capability profile

The `Profile` page exposes an inferred capability profile derived from the base CV.

Each capability can include:

- capability name
- min years
- max years
- recommended years
- confidence
- inference source

This profile is used for screening questions such as years of experience. The user can review, tighten, disable, or override individual ranges without rewriting the CV itself.

### Language targeting

- the product default language is `English`
- the `Profile` page lets the user choose a default content language
- for supported multilingual flows, the dynamic resume builder attempts to target the vacancy language even when the base CV is in a different language
- if that language alignment cannot be completed safely, the system falls back to the uploaded base CV instead of producing a mixed-language resume

## Getting started

1. Install Python 3.14 and `uv`.
2. Sync the environment:

   ```bash
   uv sync --all-packages --all-groups
   ```

3. Install the git hooks:

   ```bash
   uv run pre-commit install
   ```

4. Start the backend API locally:

   ```bash
   uv run --package job-applier-control-api uvicorn job_applier.main:app --reload
   ```

5. Check the backend health endpoint:

   ```bash
   curl http://127.0.0.1:8000/health
   ```

## LinkedIn runtime setup

The LinkedIn Jobs search automation reads credentials from local runtime config, never from versioned code.

Add these keys to your local `.env`:

```bash
JOB_APPLIER_LINKEDIN_EMAIL="you@example.com"
JOB_APPLIER_LINKEDIN_PASSWORD="your-linkedin-password"
JOB_APPLIER_PLAYWRIGHT_HEADLESS=false
```

Runtime behavior:

- the first successful login saves a reusable session in `artifacts/runtime/linkedin/storage-state.json`;
- persistent runtime state lives under `artifacts/runtime/`;
- the troubleshooting bundle for only the latest execution lives under `artifacts/last-run/`;
- later runs reuse that storage state automatically;
- if LinkedIn expires the session, the app clears the saved state and logs in again;
- when `JOB_APPLIER_PLAYWRIGHT_MCP_URL` is configured, the login bootstrap runs through Playwright MCP and exports the storage state back to the Python app;
- when `JOB_APPLIER_STAGEHAND_ENABLED=true`, the search flow can use Stagehand to semantically repair noisy LinkedIn job-detail extraction, especially in direct-target debugging and suspicious detail pages;
- in headful mode, the browser stays visible so the user can solve captcha or checkpoint screens;
- when the local state is still empty, the app bootstraps a local profile automatically from `.env` and tries to import a CV from `~/Documents`.

Two optional auxiliary automations are also feature-flagged in `.env`:

- `JOB_APPLIER_FEATURE_RECRUITER_CONNECT_ENABLED=true`
  - enabled by default for the post-submit recruiter connection flow, but it still only runs when the user preference `auto_connect_with_recruiter` is enabled too.
- `JOB_APPLIER_FEATURE_JOB_EMAIL_ENABLED=true`
  - enables a post-submit email step for vacancies whose description explicitly asks the candidate to email a resume.
  - it still only runs when the user preference `auto_send_job_email` is enabled too.
  - SMTP delivery requires the `JOB_APPLIER_EMAIL_SMTP_*` settings in `.env`.

## Resume and search configuration

Dynamic resume generation is still behind a feature flag.

- enable with `JOB_APPLIER_RESUME_DYNAMIC_ENABLED=true`;
- when enabled, each target job can receive a tailored resume variant generated as Oh-My-CV markdown;
- the renderer then attempts to export that markdown to PDF and uses the resulting file in Easy Apply;
- if generation or rendering fails, the flow falls back to the original uploaded CV (safe default);
- the profile API accepts optional `resume_css`, so users can persist custom stylesheet rules from the panel/UI for PDF rendering;
- the generated resume should preserve the base CV identity and only emphasize stack cues that are grounded in the uploaded CV or reviewed capability profile.
- the profile also accepts a default content language; this is used as a fallback signal when the vacancy language is weak or ambiguous.

The local settings state exposes:

- `static`: always apply with the uploaded base CV
- `dynamic`: generate a per-job CV variant before application
- `capability profile`: reviewed capability ranges used in screening answers
- `keywords / role targets`: broad job families for search and scoring
- `auto_connect_with_recruiter`: enabled by default for the recruiter-connect helper
- `auto_send_job_email`: disabled by default for the post-submit email helper

The search pipeline works best when `Preferences > Keywords` are broad role families rather than narrow stack-specific titles.

Recommended targets:

- `Desenvolvedor RPA`
- `Desenvolvedor Full Stack`
- `Desenvolvedor de Automação`
- `Desenvolvedor Backend`

Use stack-specific detail such as `Python`, `AWS`, `TypeScript`, `JavaScript`, `UiPath`, or `LangChain` as positive signals and tailoring cues, not as the main role family.

Optional custom renderer command:

```bash
JOB_APPLIER_RESUME_DYNAMIC_RENDER_COMMAND='oh-my-cv render "{markdown}" --output "{pdf}" --css "{css}"'
```

## Last-run troubleshooting

When you click `Run now`, the app resets `artifacts/last-run/` and keeps only the latest execution bundle:

- `summary.json`: final outcome and counters
- `progress.json`: current stage, current job and current step
- `timeline.jsonl`: ordered execution timeline across orchestration, search and Easy Apply
- `artifacts.jsonl`: index of screenshots, HTML dumps and traces created during the run
- `run.log`: structured logs for deeper debugging

The durable evidence for the product itself still lives under `artifacts/runtime/`. The `last-run` bundle exists only to make troubleshooting the latest execution fast and readable.

For deeper agent debugging, the last-run bundle now also keeps machine-oriented traces:

- `llm/browser-agent.jsonl`: structured prompt/response records for planning and assessment calls
- `browser-agent/task-trace.jsonl`: snapshot -> action -> result trace for multi-step browser tasks
- `browser-agent/single-action-trace.jsonl`: focused micro-action traces used inside Easy Apply

For low-cost manual debugging, you can enable `JOB_APPLIER_AGENT_TEST_MODE=true`. In this mode the app processes only 1 selected job per run, disables OpenAI HTTP retries, and keeps the richer browser-agent traces so we can refine prompts without falling back to brittle heuristics. If you need to force one near-match through the pipeline while debugging the apply flow, set `JOB_APPLIER_AGENT_TEST_MINIMUM_SCORE_THRESHOLD` too.

For immediate iteration on a single problematic job, set `JOB_APPLIER_LINKEDIN_DEBUG_TARGET_JOB_URL=https://www.linkedin.com/jobs/view/...`. In that mode the agent bypasses the search pages and opens the target job directly, which is much faster when we are polishing the Easy Apply agent. When `JOB_APPLIER_AGENT_TEST_MODE=true`, this direct-target mode also relaxes the score threshold to `0.0` automatically so the debug run reaches `Easy Apply` instead of being blocked by ranking.

Keep in mind that direct target URLs can go stale quickly on LinkedIn. A URL that was `Easy Apply` a few minutes ago may later turn into an external apply or closed listing, and the current pipeline is designed to skip those cases cleanly instead of forcing a broken submission.

If you also enable `JOB_APPLIER_STAGEHAND_ENABLED=true`, the debug-target path will use Stagehand observe/extract to get a cleaner semantic view of the LinkedIn job page before the parser merges the detail payload.

For stage-by-stage debugging, set `JOB_APPLIER_AGENT_DEBUG_STAGE` to one of:
- `search`: stop after fetch/hydration so we can validate filters, title parsing, company parsing and page-entry behavior
- `score`: fetch and score a few jobs without entering `Easy Apply`
- `apply`: keep the run focused on one selected job
- `full`: normal end-to-end execution

You can also override the per-stage inspection budget with `JOB_APPLIER_AGENT_DEBUG_MAX_JOBS`, and the manual API accepts `POST /api/agent/run?stage=search` (or `score` / `apply` / `full`) so you can switch stages without editing `.env` between runs.

## Dynamic resume audit

Generate mock dynamic resumes for review:

```bash
uv run --all-packages python -m job_applier.tools.generate_mock_dynamic_resumes --offline
```

Audit one generated resume artifact:

```bash
uv run --all-packages python -m job_applier.tools.audit_dynamic_resume \
  --submission-dir artifacts/runtime/artifacts/linkedin/submissions/<submission-dir> \
  --job-title "Backend Engineer (Python / JavaScript)"
```

## Quality commands

This repository is currently validated through lint, type-check, scripts, artifacts, and real/staged runs rather than a maintained unit-test suite.

Run lint:

```bash
uv run ruff check .
uv run ruff format --check .
```

Run type-check:

```bash
uv run mypy apps/backend
```

## Contributing

Contribution guidelines live in [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

Security reporting details live in [SECURITY.md](SECURITY.md).

## Code of conduct

Community expectations live in [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
