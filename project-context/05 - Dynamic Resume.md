# Dynamic Resume

## Purpose

Generate one resume per matched job without drifting away from the base CV.

## Core Inputs

- base CV PDF
- persisted canonical resume snapshot
- matched role target
- matched specializations
- job title and usable detail context
- reviewed capability profile

## Builder Strategy

The builder uses a structured approach:

1. reuse a persisted canonical resume snapshot when the base CV hash matches
2. otherwise extract and persist a new snapshot from the base CV
3. create a heuristic adaptation plan
4. optionally refine with AI
5. sanitize the AI output against allowed evidence
6. optionally localize to the target job language
7. render markdown
8. render PDF
9. validate output and fall back when necessary

## Why The Snapshot Matters

The snapshot is now a first-class persisted asset.

Benefits:

- the builder does not need to rebuild the factual CV structure on every job
- the snapshot can be reviewed and edited independently of the raw CV file
- future UI/editor work can operate on the snapshot directly
- token and latency pressure should drop over time because less raw-CV work is repeated

Important boundary:

- the snapshot is still deterministic-first today
- AI is not the sole source of truth for the snapshot
- the snapshot remains grounded in the uploaded base CV
- sensitive private metadata is not stored inside the resume snapshot
- sensitive metadata belongs to a separate private-metadata flow used only by Easy Apply

## Important Rules

- preserve base role identity
- allow editorial emphasis
- do not mention stacks not anchored in evidence
- keep the output readable and recruiter-usable

## Backend Positioning Rule

A recurring product requirement is:

- keep the candidate identity as `Full Stack` when true
- but still position strongly enough for backend roles

That led to two recent fixes:

- stronger backend editorial positioning
- better backend target resolution when job detail is sparse

## Audit Layer

The local auditor checks:

- headline quality
- summary quality
- unanchored terms
- keyword stuffing
- mixed-language resume bodies
- some obvious label leaks
- page underuse heuristics

Use:

```bash
uv run --all-packages python -m job_applier.tools.audit_dynamic_resume \
  --submission-dir artifacts/runtime/artifacts/linkedin/submissions/<submission-dir> \
  --job-title "Backend Engineer (Python / JavaScript)"
```

## Current Known Dynamic-Resume Status

English dynamic resumes are strong enough for beta.

Portuguese dynamic resumes:

- have already passed real successful apply flows
- are materially better than the earlier mixed-language artifacts
- should still be reaudited whenever localization prompts or rendering logic changes

## Current Cost Optimization Notes

The latest cold-start optimization for PT localization is intentionally conservative:

- factual/proper-name fields such as company names, institutions, issuers, certification names, and city are no longer sent through the translation step
- only items that still look like natural-language localization work are sent to OpenAI
- translation batches are larger now, which reduces the number of `resume_translation` calls

This keeps the optimization low-risk because it avoids changing the overall factual rendering contract.
