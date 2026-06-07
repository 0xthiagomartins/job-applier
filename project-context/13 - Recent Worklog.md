# Recent Worklog

This file summarizes the most relevant recent changes so a new harness does not have to infer intent only from the raw git log.

## Important Recent Commits

- `working tree` `persist canonical resume source snapshots and reuse them in dynamic resume generation`
- `412d90d` `fix: harden accessibility field resolution`
- `495734f` `fix: harden linkedin prod apply guardrails`
- `98ade4b` `feat: replay adaptive field memory across apply flows`
- `0776462` `test: harden adaptive apply memory replay`
- `d3ebff5` `fix: warm apply memory from deterministic footer actions`
- `aecb52d` `refactor: move apply memory storage to diskcache`
- `c1559ed` `feat: add adaptive easy apply memory`
- `7dc5c4b` `fix: align playwright mcp click and type payloads`
- `e32b16a` `refactor: reorganize backend workspace structure`
- `5ab3ed4` `chore: checkpoint backend cleanup and apply flow changes`
- `598b89e` `fix: upload tailored resumes in linkedin easy apply`
- `a1e3ea6` `fix: prefer env ai key over persisted panel state`
- `53ada37` `docs: add portable project-context vault for handoff`
- `5310e15` `fix: localize standalone "Applied AI" in PT resume output`
- `adaf322` `fix: tighten portuguese dynamic resume localization`
- `7725b9b` `fix: harden dynamic resume localization quality`

## Issues Recently Addressed

- backend workspace reorganization under `apps/backend/...`
- removal of the old frontend surface
- `.env` precedence for OpenAI key
- dynamic resume PT localization
- tailored resume upload in real `Easy Apply`
- adaptive apply memory
- diskcache-backed apply memory storage
- replay of field-level memory across PT/EN flows
- production fail-fast behavior on OpenAI `429`
- accessibility/disability field handling
- canonical resume source snapshot persistence
- backend routes to inspect, refresh, and override the canonical snapshot
- 1-hour diskcache-backed search+score reuse for repeated full-stage validations
- stronger resume reassertion in the apply flow before falling back to picker reselection
- resume-review repair now treats a re-opened resume step with the correct checked PDF as verified instead of forcing a redundant reselection loop
- post-submit `about:blank` Jobgether/Lever flows now complete via deterministic job-page recheck
- sensitive-question guardrails no longer false-positive on generic `drug test` yes/no questions because of substring token collisions like `cor` inside `accordance`

## Recent Real Successes

- real successful PT submissions:
  - `4418597669` `Jobgether` `Engenheiro de Software Pl. (Java)`
  - `4419642311` `Itaú Unibanco` `Engenharia de software Sênior- JAVA`
- real successful EN submissions:
  - `4419915012` `Oowlish` `Senior Software Engineer (AI & Cloud Solutions)`
  - `4422836187` `Crossing Hurdles` `Software Engineer`
  - `4424232275` `Jobgether` `AI Developer Backend Java Sr`

## Current Hotspot

The main remaining area to keep validating is:

- credit efficiency during real production validation
- long employer-specific forms
- how much adaptive apply memory reduces OpenAI calls in repeated flows
- how much the persisted resume snapshot reduces repeated dynamic-resume setup cost
- how much the new 1-hour search+score cache reduces repeated full-stage validation cost

There is no single known blocker in dynamic resume quality right now; the higher-value work is cost control and apply robustness.

## Suggested Next Validation

Use the fixed low-cost 3-job suite:

1. `4418597669` `Jobgether` `PT`
2. `4422383527` `CI&T` `PT`
3. `4420980277` `CI&T` `EN`

Use the `Jobgether` slot as the cheapest smoke test.
Use the two `CI&T` slots only when validating apply memory, field resolution, or production guardrails.

## Handoff Readiness

The repository now also includes a portable Obsidian-style handoff vault in `project-context/`.

That means a new harness can be onboarded with:

- the repository
- the `project-context/` folder

without having to reconstruct product intent from raw code and git history alone.
