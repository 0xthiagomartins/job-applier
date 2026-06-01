# Architecture

## High-Level Shape

The system has three major surfaces:

1. backend application
2. local panel UI
3. LinkedIn automation/runtime

## Backend

Main stack:

- FastAPI
- Python 3.14
- SQLite via repository layer
- Alembic migrations

Core responsibilities:

- store and serve panel state
- orchestrate staged or full runs
- persist postings, submissions, answers, artifacts, and execution events
- coordinate scoring, resume generation, and apply execution

## Panel

Main stack:

- Next.js
- TypeScript

Responsibilities:

- profile setup
- search preferences
- resume mode selection
- capability profile review/override
- history and operations visibility

## Runtime / Automation

Main stack:

- Playwright-based LinkedIn interaction
- optional semantic repair flows
- artifact capture into local filesystem

Responsibilities:

- fetch job pages
- determine whether `Easy Apply` is actually usable
- open and traverse the modal
- answer fields
- upload the right CV
- confirm submission or skip with diagnosis

## Persistence Layers

- `artifacts/runtime/job-applier.db`: main local database
- `artifacts/runtime/panel/`: saved panel state and CV copies
- `artifacts/runtime/artifacts/`: per-submission evidence bundles
- `artifacts/last-run/`: latest-run troubleshooting bundle

## Major Internal Subsystems

- search and detail extraction
- job scoring
- capability profile inference
- dynamic resume building and localization
- Easy Apply execution
- timeline/artifact/event telemetry

## Design Intent

The architecture intentionally separates:

- semantic understanding
- deterministic browser control
- evidence capture

This makes it easier to debug whether a failure came from:

- weak job extraction
- weak matching
- weak resume generation
- weak form understanding
- fragile control execution

