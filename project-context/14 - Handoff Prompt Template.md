# Handoff Prompt Template

Use this prompt as a starting point for any new harness, not only Codex or Claude.

```text
You are joining an existing software project.

Use the repository as the source of truth for code, and the Obsidian project-context vault as the source of truth for product context, validation history, architectural intent, and known risks.

Start by reading:
- 00 - Start Here.md
- 10 - Known Risks.md
- 12 - File Map.md
- 13 - Recent Worklog.md

Then inspect the codebase before making assumptions.

Important rules:
- preserve the existing architecture direction unless there is a strong reason to change it
- do not reintroduce approaches that were already rejected
- treat the base CV as sovereign in dynamic resume flows
- prefer broad role-family reasoning over brittle one-off title rules
- validate changes using the documented lint, type-check, build, scripts, and artifacts
- use artifacts and timeline evidence before guessing about failures
- do not assume the LinkedIn surface is stable

Current project characteristics:
- LinkedIn Easy Apply automation
- multi-target search
- rule-based scoring with specialization cues
- static and dynamic resume modes
- capability-profile-backed screening answers
- English-first product with active Portuguese support work

Deliver work in a way that preserves:
- factual integrity
- auditability
- reuse across many UI and language variations
```

