# Agentic Hardening Plan

## Goal

Finish the project without growing fragile LinkedIn-specific heuristics.

The target architecture is:

1. deterministic macro orchestration
2. semantic UI observation
3. agentic decision making on top of an explicit UI state
4. local recovery when a step fails
5. verification before continuing

This means the system should recover from recoverable mistakes in-place instead of restarting the whole run whenever possible.

## What can remain deterministic

These parts are acceptable and should stay deterministic:

- orchestration of the high-level flow
- persistence and audit trail
- deduplication of already applied jobs
- timeouts, budgets, and retry ceilings
- success and failure accounting
- safety and compliance guardrails
- execution scheduling and overlap prevention

## What should not remain heuristic

These parts should not rely on text-specific or layout-specific remendos:

- reading LinkedIn search result cards
- deciding whether the search surface is ready, blocked, loading, or empty
- parsing title, company, location, workplace type, and seniority from noisy page text
- deciding which form control is the blocking one
- deciding the next action inside Easy Apply
- adapting answers to ambiguous questions using keyword rules
- choosing visible options by hardcoded language-dependent labels
- repairing form mistakes with one-off rules such as "if city then ..."

## Current heuristic inventory

### 1. Search parsing and filter handling

Main file:
- [search.py](/home/thiago/projects/public/job-applier/src/job_applier/infrastructure/linkedin/search.py)

Current heuristic hotspots:

- `_DETAIL_PLACEHOLDER_TOKENS`, `_NON_COMPANY_EXACT_TOKENS`, `_NON_COMPANY_SUBSTRING_TOKENS`
  - used to filter out bad company/title candidates from the detail page
  - strongly coupled to current LinkedIn wording

- `infer_workplace_type()`
  - infers `remote`, `hybrid`, `on-site` from substrings

- `infer_seniority()`
  - infers `senior`, `junior`, `staff`, `principal`, etc. from substrings

- `_looks_like_location_line()`
  - uses a hardcoded list of city/country/location tokens

- `_looks_like_non_company_line()`
  - classifies bad company candidates with hand-maintained tokens

- `_fill_input()`
  - relies on role/label regex patterns to find search inputs

- `_apply_filters()`, `_toggle_filter()`, `_select_filter_options()`
  - still use label-based button names like `Easy Apply`, `Date posted`, `Past 24 hours`, `On-site/remote`, `Experience level`, `Show results`, `Apply`

Risk:
- high

Why it matters:
- this is where language dependence still propagates most strongly
- this is also where search correctness starts, so noise here contaminates score and apply

### 2. Question classification and fallback answering

Main file:
- [question_resolution.py](/home/thiago/projects/public/job-applier/src/job_applier/infrastructure/linkedin/question_resolution.py)

Current heuristic hotspots:

- `LinkedInQuestionClassifier.classify()`
  - maps questions to types using keyword lists such as `first name`, `last name`, `email`, `visa`, `salary`, `city`, `experience`

- `_resolve_profile_value_by_normalized_key()`
  - direct mapping from normalized keys to profile values

- `_resolve_phone_country_code()`
  - prefers `Brazil`, `Brasil`, `+55`, `55`

- `_resolve_exact_years_experience()`
  - matches stack names by keyword containment

- `_resolve_guardrail_fallback()`
  - contains several deterministic fallbacks:
    - current employer -> `Freelancer`
    - language proficiency -> conservative option picker
    - generic option picking
    - generic yes/no answer choice
    - textarea fallback -> `Open to discuss.`
    - numeric fallback based on salary or inferred years

- `_adapt_field_for_validation_feedback()`
  - reclassifies fields as numeric from validation tokens

- `_infer_conservative_related_years()`
  - converts stack experience to a conservative generic experience number

- `_prefers_negative_answer()`
  - chooses `No` for newsletter, talent community, alerts, marketing-style prompts

- `_looks_like_current_employer_question()`
  - keyword-based detection of employer prompts

- `_looks_like_language_proficiency_question()`
  - keyword-based detection of language proficiency ladders

- `_pick_conservative_language_proficiency_option()`
  - heuristic selection of an intermediate answer

Risk:
- very high

Why it matters:
- this is where non-agentic behavior is still strongest
- it directly controls what the candidate answers in production
- it is especially fragile across languages and new question phrasings

### 3. Easy Apply control interaction

Main file:
- [easy_apply.py](/home/thiago/projects/public/job-applier/src/job_applier/infrastructure/linkedin/easy_apply.py)

Current heuristic hotspots:

- control extraction by DOM shape
  - inputs, radios, checkboxes, option lists, visibility, proximity

- radio/checkbox activation strategies
  - direct input activation first, then browser-agent fallback

- control finalization and validation inspection
  - several branches depend on DOM conventions and validation timing

- invalid-field retry logic
  - still depends in part on field classification and validation text interpretation

Risk:
- medium to high

Why it matters:
- this is less language-coupled than question classification
- but it is still fragile to DOM/layout variation

### 4. Browser agent structural heuristics

Main file:
- [browser_agent.py](/home/thiago/projects/public/job-applier/src/job_applier/infrastructure/linkedin/browser_agent.py)

Current heuristic hotspots:

- active surface detection
- click blocker detection through DOM hit testing
- candidate control discovery with CSS selectors and geometry

Risk:
- medium

Why it matters:
- these are layout heuristics, not content heuristics
- they are more acceptable as transitional infrastructure, but they still are not a semantic UI model

### 5. Stagehand integration status

Main file:
- [stagehand.py](/home/thiago/projects/public/job-applier/src/job_applier/infrastructure/linkedin/stagehand.py)

Current status:

- already used to semantically repair:
  - job detail extraction
  - search card extraction
  - search surface assessment

Current gap:

- Stagehand is still a repair layer in a hybrid flow, not yet the primary UI-state source for the whole volatile LinkedIn surface

Risk:
- low by itself, but high-value as the replacement path for the remaining heuristics

## Safe removal order

### Phase 1. Search semantics first

Why first:

- safest area to change
- easiest to validate with `stage=search`
- failures here do not submit applications

Work:

- make Stagehand the primary source for search surface readiness
- make Stagehand the primary source for visible job cards when possible
- reduce label-driven search/filter fallbacks to backup-only behavior
- stop using token lists for company/location cleanup as the main path
- preserve deterministic URL-based search construction, but verify results through semantic observation

Exit criteria:

- 3 consecutive `stage=search` runs
- no polluted company names from card/detail merge
- results surface correctly classified as ready/loading/empty/blocked
- no dependency on current UI language for the happy path

### Phase 2. Score semantics second

Why second:

- depends on clean search payloads
- still does not touch submission

Work:

- verify that scored jobs are based on semantically clean postings
- ensure `jobs_seen` and `jobs_selected` remain accurate in incremental mode

Exit criteria:

- 3 consecutive `stage=score` runs
- selected vs rejected jobs look reasonable from the actual job content

### Phase 3. Replace question classifier with answer planner

Why third:

- this is the biggest remaining content heuristic layer
- it should be replaced carefully after search is stable

Work:

- reduce `LinkedInQuestionClassifier` from main decision-maker to optional metadata helper
- introduce an answer-planning step that reasons over:
  - question text
  - visible options
  - control kind
  - validation feedback
  - profile facts
  - job context
  - safety/compliance rules
- keep only safety guardrails deterministic
- ban one-off answer patches tied to individual fields

Exit criteria:

- the agent can answer:
  - text
  - textarea
  - select
  - radio
  - checkbox
  - numeric
  - autocomplete
- without relying on a fixed keyword list as the main path

### Phase 4. Easy Apply step-state semantics

Why fourth:

- once answers become more agentic, the next bottleneck is step interpretation

Work:

- build an explicit semantic step state for Easy Apply:
  - modal visible
  - current blocking control
  - validation errors
  - CTA intent
  - submit/review/continue state
  - blocker overlay
- make advance/repair/verify decisions from that state instead of mixed DOM heuristics

Exit criteria:

- the agent can make a mistake, detect the mistake, repair locally, and continue
- the flow does not restart from scratch for recoverable form issues

### Phase 5. Recovery policy

Why fifth:

- this is what turns the agent from “fragile but smart” into “autonomous”

Work:

- standardize the recovery loop:
  - observe
  - diagnose
  - repair
  - verify
  - resume
- classify failures into:
  - recoverable
  - requires user intervention
  - terminal
- ensure browser cleanup and execution finalization always happen

Exit criteria:

- no hanging runs
- no silent infinite loops
- every failure ends with either recovery or a precise terminal state

### Phase 6. Full-run production validation

Why last:

- full runs are expensive and noisy
- they should only happen after stage validation is stable

Work:

- validate full runs only after `search`, `score`, and `apply` are individually approved

Exit criteria:

- 3 consecutive `full` runs without hang
- successful apply flow in real LinkedIn runs
- stable cleanup and trustworthy `artifacts/last-run`

## Validation protocol

Always validate in this order:

1. `POST /api/agent/run?stage=search`
2. inspect `artifacts/last-run`
3. fix search-only issues
4. `POST /api/agent/run?stage=score`
5. inspect selection quality
6. `POST /api/agent/run?stage=apply`
7. inspect form-state, answer planning, and recovery
8. only then run `full`

## Definition of done for the current project phase

The project can be considered ready for supervised on-prem production when:

- search uses semantic observation as the main path
- answer selection is agentic, with deterministic safety guardrails only
- Easy Apply can recover locally from recoverable mistakes
- no recurring language-bound heuristics remain on the happy path
- staged validation passes consistently
- full runs complete without overlapping browsers, hanging states, or silent failures

## Explicit anti-regression rule

Do not add new hardcoded fixes of the form:

- if question contains X, answer Y
- if field contains city, do special-case Z
- if button label is exact English string, depend on that as the primary path
- if one LinkedIn widget behaves oddly, patch only that widget wording

If a bug appears, the preferred fix is:

1. improve semantic observation
2. improve agent prompt/context
3. improve local recovery and verification
4. only use deterministic logic for safety, orchestration, or explicit compliance constraints
