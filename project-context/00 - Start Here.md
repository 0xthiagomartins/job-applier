# Job Applier Project Context

This vault is the portable context layer for the `job-applier` repository.

Use this vault together with the repository:

- the repository is the source of truth for code
- this vault is the source of truth for product context, architecture intent, validation history, risks, and handoff notes

## Read This First

Start here when onboarding any new agent, harness, or collaborator:

1. [[01 - Product Overview]]
2. [[10 - Known Risks]]
3. [[12 - File Map]]
4. [[13 - Recent Worklog]]

Then inspect the repo before making assumptions.

## What This Project Is

`job-applier` is an internal-beta LinkedIn `Easy Apply` automation product with:

- multi-target search
- rule-based scoring with role-family inference
- `static` and `dynamic` resume modes
- dynamic resume generation grounded in one truthful base CV
- a reviewed capability profile used for screening questions
- strong artifact capture for operational debugging

## Current Status

The project is functional for internal beta usage, including real successful submissions, but it is still a beta:

- the core search -> score -> apply pipeline works
- English and Portuguese dynamic resume flows both work in real runs
- adaptive local apply memory now exists and replays successfully in production-like validation
- the base CV now has a persisted canonical source snapshot reused across dynamic resume generation
- multilingual dynamic resume generation exists
- the main operational concern now is cost and latency, not a known resume-quality blocker
- LinkedIn surface changes remain a normal operational risk

## How To Use This Vault

- product framing: [[01 - Product Overview]]
- architecture: [[02 - Architecture]]
- search/score/apply pipeline: [[03 - Pipeline Search Score Apply]]
- resume system: [[04 - Resume Modes]] and [[05 - Dynamic Resume]]
- screening/capabilities: [[06 - Capability Profile]]
- i18n: [[07 - Internationalization]]
- operations and validation: [[09 - Validation and Operations]]
- risks: [[10 - Known Risks]]
- decisions: [[11 - Decisions Log]]
- important files: [[12 - File Map]]
- latest project history: [[13 - Recent Worklog]]
- reusable handoff prompt: [[14 - Handoff Prompt Template]]

## Safety Notes

Do not put these in shared vault exports unless you explicitly want to:

- `.env`
- LinkedIn credentials
- browser session files
- cookies or storage state
- the real base CV PDF
- any personal data not required for debugging

The repo contains local runtime state under `artifacts/runtime/`; that data should be curated before sharing outside your trusted environment.
