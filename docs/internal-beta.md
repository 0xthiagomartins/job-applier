# Internal Beta Guide

This guide describes the current internal-beta contract for `job-applier`.

## What the MVP supports

- LinkedIn job search with broad multi-target role families
- Role targets such as:
  - `Automation Engineer`
  - `Automation Developer`
  - `RPA Developer`
  - `Backend Developer`
  - `Full Stack Developer`
- Two resume modes:
  - `static`: upload one base CV and use it as-is
  - `dynamic`: generate one tailored CV per matched job while preserving the base CV identity
- English-first product defaults with job-language-aware resume generation for supported languages
- Competitive-but-plausible screening answers derived from the base CV
- Artifact capture for scoring, dynamic resume generation, and Easy Apply troubleshooting

## What is not fully supported yet

- Arbitrary LinkedIn form layouts with perfect success rates
- Guaranteed success on every `Easy Apply` modal variation
- Multi-board support outside LinkedIn
- Visual CV theme editing from the panel
- High-confidence tailoring for stacks that are not grounded in the base CV

## Recommended panel setup

1. Upload a truthful base CV in the `Profile` page.
2. Choose `Resume mode`:
   - `static` if you want strict reuse of the uploaded CV
   - `dynamic` if you want per-job tailoring
3. Choose `Default content language`:
   - keep `English` as the default unless your base profile is primarily maintained in Portuguese
   - the dynamic resume builder will still target the vacancy language when it has a strong signal
4. Keep `Keywords / role targets` broad in the `Preferences` page.

Recommended target families:

- `Automation Engineer`
- `Automation Developer`
- `RPA Developer`
- `Backend Developer`
- `Full Stack Developer`

Do not use overly narrow search targets such as `Python Automation Engineer` as the main family. Let the scorer and dynamic resume builder infer stack emphasis per job.

## Capability profile review

The `Profile` page now shows an inferred capability profile derived from the base CV.

Each capability row exposes:

- capability name
- min years
- max years
- recommended years
- confidence
- source / inference origin

How to use it:

- leave it untouched if the inferred range already looks plausible
- override it when the inferred range is too weak or too strong
- disable a capability if it would create misleading screening answers

Priority rules:

- exact values in `Experience by stack` win first
- reviewed capability overrides come next
- inferred ranges are used when there is no exact value

## Dynamic resume behavior

In `dynamic` mode the system should:

- preserve the base CV identity
- keep dates, employers, education, and certifications factual
- emphasize stack cues that are both:
  - present in the target job
  - supported by the base CV or reviewed capability profile

If dynamic generation fails, the flow falls back to the uploaded base CV.

If the vacancy language differs from the base CV language and the system cannot complete a safe localization pass, the flow also falls back to the uploaded base CV instead of generating a mixed-language document.

## Artifact review workflow

The latest execution bundle lives in `artifacts/last-run/`.

Use these files first:

- `progress.json`
- `summary.json`
- `timeline.jsonl`
- `run.log`

Per-submission artifacts live under:

- `artifacts/runtime/artifacts/linkedin/submissions/...`

Useful files inside one submission:

- `input/*.pdf`: the original CV used as source input
- `dynamic-resume/*-oh-my-cv.md`: generated markdown source
- `dynamic-resume/*-tailored.pdf`: rendered dynamic CV
- failure screenshots / HTML dumps when Easy Apply breaks

## Quality review commands

Python quality:

```bash
uv run ruff check .
uv run mypy src
```

Panel build:

```bash
cd apps/panel
npm run build
```

Generate mock dynamic resumes for review:

```bash
uv run python scripts/generate_mock_dynamic_resumes.py --offline
```

Audit one generated dynamic resume:

```bash
uv run python scripts/audit_dynamic_resume.py \
  --submission-dir artifacts/runtime/artifacts/linkedin/submissions/<submission-dir> \
  --job-title "Backend Engineer (Python / JavaScript)"
```

## Suggested debug flow

1. Start with `stage=score` when validating role targeting.
2. Use `stage=apply` or a direct target job URL when refining Easy Apply behavior.
3. Inspect `timeline.jsonl` before changing code.
4. Use the dynamic resume auditor before polishing prompts or CSS.

## MVP interpretation

This beta is ready for controlled internal usage when:

- search and scoring are selecting the right role families
- capability profile ranges look plausible
- dynamic resumes stay truthful
- the auditor reports no major findings on representative samples
- unsupported Easy Apply cases skip with a clear diagnosis instead of burning the run
