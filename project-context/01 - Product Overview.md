# Product Overview

## Product Goal

Automate LinkedIn `Easy Apply` job applications without turning the candidate profile into spam or fiction.

The product should:

- search across broad role families
- score jobs conservatively but usefully
- optionally tailor a resume per job
- answer screening questions competitively but plausibly
- apply through LinkedIn while capturing enough evidence to debug failures

## Core User Model

The user provides:

- one truthful base CV
- broad role targets
- a reviewed capability profile
- preferences and defaults in the panel

The system then:

1. searches LinkedIn
2. scores postings against the configured role families
3. skips unfit or unsupported jobs
4. applies with either the base CV or a dynamic variant
5. stores logs and artifacts for audit

## Current MVP Scope

Supported today:

- LinkedIn only
- multi-target search
- `static` and `dynamic` resume modes
- dynamic resume rendering to PDF
- capability inference from the base CV
- English-first product behavior
- supported multilingual targeting for Portuguese
- local FastAPI + Next.js + SQLite runtime

Not promised today:

- perfect success on every LinkedIn variation
- all languages
- all job boards
- a visual resume theme editor
- perfect high-recall scoring for every title naming pattern

## Product Principles

- one truthful base CV is sovereign
- dynamic tailoring must not invent experience
- role targets should be broad families, not overly narrow titles
- stack emphasis should come from real evidence
- the system should skip unsupported cases cleanly instead of forcing broken flows
- artifacts matter; if it cannot be inspected, it is hard to trust

## Recommended Role Targets

Broad families:

- Automation Engineer
- Automation Developer
- RPA Developer
- Backend Developer
- Full Stack Developer
- Software Engineer
- Software Developer

Portuguese aliases are also supported in the current local setup.

## Current Local Target Order

The current local configuration prioritizes Brazilian targets first:

1. `Engenheiro de Software`
2. `Desenvolvedor Full Stack`
3. `Desenvolvedor Backend`
4. `Desenvolvedor de Software`
5. `Engenheiro de Automação`
6. `Desenvolvedor de Automação`
7. `Desenvolvedor RPA`
8. `Software Engineer`
9. `Full Stack Developer`
10. `Backend Developer`
11. `Software Developer`
12. `Automation Engineer`
13. `Automation Developer`
14. `RPA Developer`

