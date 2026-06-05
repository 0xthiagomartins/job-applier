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
- supported multilingual targeting for Portuguese and English
- adaptive local apply memory for repeated `Easy Apply` interactions
- local FastAPI + Playwright + SQLite runtime

Not promised today:

- perfect success on every LinkedIn variation
- all languages
- all job boards
- a local GUI/frontend
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

- Desenvolvedor RPA
- Desenvolvedor Full Stack
- Desenvolvedor de Automação
- Desenvolvedor Backend
- Software Engineer
- Software Developer

Portuguese aliases are also supported in the current local setup.

## Current Local Target Order

The current local configuration prioritizes Brazilian targets first:

1. `Desenvolvedor RPA`
2. `Desenvolvedor Full Stack`
3. `Desenvolvedor de Automação`
4. `Desenvolvedor Backend`
