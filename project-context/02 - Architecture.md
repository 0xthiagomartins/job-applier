# Architecture

## High-Level Shape

The system has three major surfaces:

1. backend application
2. local settings/runtime state
3. LinkedIn automation/runtime

## Backend

Main stack:

- FastAPI
- Python 3.14
- SQLite via repository layer
- Alembic migrations

Core responsibilities:

- store and serve local settings state
- orchestrate staged or full runs
- persist postings, submissions, answers, artifacts, and execution events
- persist a canonical resume source snapshot for the current local owner
- coordinate scoring, resume generation, and apply execution

## Local Control Surface

There is no active frontend application today.

The control surface is:

- local runtime settings under `artifacts/runtime/panel/`
- FastAPI endpoints
- direct debug/prod execution modes

## Runtime / Automation

Main stack:

- Playwright-based LinkedIn interaction
- optional semantic repair flows
- adaptive local apply memory backed by `diskcache`
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
- `artifacts/runtime/cache/apply-action-memory/`: adaptive apply memory cache
- `artifacts/runtime/artifacts/`: per-submission evidence bundles
- `artifacts/last-run/`: latest-run troubleshooting bundle

## Major Internal Subsystems

- search and detail extraction
- job scoring
- capability profile inference
- canonical resume source snapshot persistence
- dynamic resume building and localization
- Easy Apply execution
- adaptive apply memory and field replay
- timeline/artifact/event telemetry

## Single-User But Future-Ready

The runtime is intentionally single-user today, but some newer persistence is already shaped for a future SaaS direction.

Current example:

- persisted resume source snapshots are scoped by a local `owner_key`
- the default owner is `local-default`
- this keeps the local runtime simple now while avoiding a hard single-user schema dead-end later

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
