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

Portuguese support is much better now, but still not fully closed.

Current specific risk:

- some PT dynamic resume artifacts may still leak English-heavy skill or interests content
- the latest fixes are in code, but that exact live validation is still an area to keep checking

## 4. Scoring Recall vs Precision

The scorer is intentionally conservative now.

Good:

- fewer false positives

Tradeoff:

- some relevant jobs may still be missed if titles are unusually phrased

## 5. Telemetry Cleanliness

Functional outcomes are stronger than telemetry polish.

Some prior runs showed:

- inherited `skip_reason` or `submission_id` noise across jobs
- timeline clarity issues

This is not always a blocker, but it can slow debugging.

## 6. External Service Dependence

The dynamic resume system depends on AI availability when doing full localization or advanced adaptation.

Risks:

- quota
- API failures
- environment policy restrictions on sending private data externally

