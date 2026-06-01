# Dynamic Resume

## Purpose

Generate one resume per matched job without drifting away from the base CV.

## Core Inputs

- base CV PDF
- extracted resume snapshot
- matched role target
- matched specializations
- job title and usable detail context
- reviewed capability profile

## Builder Strategy

The builder uses a structured approach:

1. extract a resume snapshot from the base CV
2. create a heuristic adaptation plan
3. optionally refine with AI
4. sanitize the AI output against allowed evidence
5. optionally localize to the target job language
6. render markdown
7. render PDF
8. validate output and fall back when necessary

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
uv run python scripts/audit_dynamic_resume.py \
  --submission-dir artifacts/runtime/artifacts/linkedin/submissions/<submission-dir> \
  --job-title "Backend Engineer (Python / JavaScript)"
```

## Current Known Dynamic-Resume Status

English dynamic resumes are strong enough for beta.

Portuguese dynamic resumes:

- are materially better than earlier mixed-language artifacts
- now preserve localized metadates better
- still need final live validation to eliminate remaining English-heavy skill/interests suffix leaks in some cases

