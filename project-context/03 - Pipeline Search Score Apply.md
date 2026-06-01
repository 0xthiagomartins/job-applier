# Pipeline Search Score Apply

## Pipeline Stages

The main operational pipeline is:

1. search
2. detail extraction and normalization
3. score
4. select
5. prepare CV
6. open `Easy Apply`
7. answer form steps
8. review and submit
9. capture artifacts and persist final status

## Search

The system performs one LinkedIn search campaign per configured role target.

Important behavior:

- targets are searched independently, not merged into one giant query
- debug target mode can bypass search and open one direct job URL
- current local setup prioritizes PT-BR job families first

## Detail Extraction

The search layer tries to produce a normalized `JobPosting` with:

- title
- company name
- location
- description
- `easy_apply` signal
- detail quality metadata

Recent hardening added:

- `detail_quality_score`
- `detail_description_score`
- detail quality signals and source labels

This matters because poor job detail quality should reduce trust in stack inference and resume tailoring.

## Score

The scorer is rule-based and tries to infer:

- whether the job belongs to one configured role family
- what specializations are explicit in the title/context
- whether the posting should be selected

Outputs include:

- `matched_role_target`
- `matched_specializations`
- detailed score components
- rejection reasons when not selected

## Select

Only jobs that are both:

- `Easy Apply`
- and aligned enough with the configured role families

should move forward.

The pipeline should skip jobs early when:

- `Easy Apply` is not actually present
- the listing is closed
- it is external apply only
- the job was already successfully applied
- the daily LinkedIn `Easy Apply` limit is reached

## Apply

The executor:

- assesses entrypoint availability
- opens the modal
- extracts each step
- preserves already-good fields where possible
- answers missing fields
- advances until review/submit

The current system supports recovery from some validation feedback, but the LinkedIn surface remains a beta-risk area.

## Observability

Key outputs:

- `progress.json`
- `summary.json`
- `timeline.jsonl`
- `run.log`
- per-submission HTML/screenshots/PDFs/markdown

