# Known Risks

## 1. LinkedIn Surface Variability

This is the biggest ongoing operational risk.

Examples:

- modal structure changes
- button wording changes
- `Easy Apply` visibility changes
- already-applied states
- daily apply limits
- closed or stale job URLs

## 2. Sparse Job Detail Quality

Some postings come with poor or noisy detail content.

Effects:

- weaker specialization extraction
- generic `matched_specializations`
- more conservative dynamic resumes

Recent hardening improved this, but it remains a real risk surface.

## 3. Multilingual Dynamic Resume Quality

Portuguese support is much better now and has passed real submissions.

Current specific risk:

- localization/prompt changes can still regress PT output quality
- PT artifacts should still be spot-audited after resume-worker changes

## 4. OpenAI Cost And Rate Limits

The current biggest operational risk is cost pressure during real validation.

Risk factors:

- long `Easy Apply` forms
- semantic step planning
- `autofill_ai` on repeated screening questions
- PT dynamic resume localization

Mitigations already in place:

- adaptive local apply memory
- production halt on OpenAI `429`
- 3-attempt production retry ceilings

## 5. Scoring Recall vs Precision

The scorer is intentionally conservative now.

Good:

- fewer false positives

Tradeoff:

- some relevant jobs may still be missed if titles are unusually phrased

## 6. Telemetry Cleanliness

Functional outcomes are stronger than telemetry polish.

Some prior runs showed:

- inherited `skip_reason` or `submission_id` noise across jobs
- timeline clarity issues

This is not always a blocker, but it can slow debugging.

## 7. External Service Dependence

The dynamic resume system depends on AI availability when doing full localization or advanced adaptation.

Risks:

- quota
- API failures
- environment policy restrictions on sending private data externally

## 8. Expensive Employer-Specific Forms

Some employers, especially CI&T-style flows, have long screening forms that are useful for validation but expensive for credits.

Use them intentionally:

- good for memory warmup and replay validation
- bad as default smoke tests
