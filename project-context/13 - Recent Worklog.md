# Recent Worklog

This file summarizes the most relevant recent changes so a new harness does not have to infer intent only from the raw git log.

## Important Recent Commits

- `pending` `docs: add portable project-context vault for handoff`
- `pending` `fix: localize standalone "Applied AI" in PT resume output`
- `adaf322` `fix: tighten portuguese dynamic resume localization`
- `7725b9b` `fix: harden dynamic resume localization quality`
- `5ab9e99` `fix: prefer specific backend targets for sparse job details`
- `2f0728c` `fix: strengthen backend positioning in dynamic resumes`
- `31a0618` `fix: stop runs when linkedin easy apply limit is reached`
- `f3fa03d` `fix: broaden multilingual target matching and search queries`
- `9bc07a3` `fix: batch multilingual resume translation payloads`
- `295d5d6` `fix: harden sparse linkedin job detail extraction`
- `e8ed4af` `feat: add language-aware dynamic resume targeting`
- `16a5298` `feat: add a local auditor for dynamic resumes`
- `001eeb1` `feat: expose reviewed capability profiles in the panel`
- `dfe038f` `feat: infer competitive capability ranges from base resumes`

## Issues Recently Addressed

- dynamic/static resume mode alignment
- capability profile exposure and usage
- multilingual dynamic resumes
- backend-specific resume positioning
- sparse job detail extraction
- visible `Easy Apply` control detection
- generic engineering title scoring
- daily Easy Apply limit termination

## Recent Real Successes

Recent real successful applies existed before the PT localization loop, including:

- English dynamic resume flows
- Portuguese `Easy Apply` path success in earlier real runs

## Current Hotspot

The main remaining area to keep validating is:

- Portuguese dynamic resume output quality in production-like artifacts

The code has been tightened repeatedly, but this should still be treated as an active refinement area rather than a fully closed topic.

## Suggested Next Validation

Use a fresh PT vacancy with `Easy Apply` available and inspect:

- generated markdown
- rendered PDF
- auditor findings
- whether the resume body and skill lines are consistently Portuguese except for intentional tech tokens

## Handoff Readiness

The repository now also includes a portable Obsidian-style handoff vault in `project-context/`.

That means a new harness can be onboarded with:

- the repository
- the `project-context/` folder

without having to reconstruct product intent from raw code and git history alone.
