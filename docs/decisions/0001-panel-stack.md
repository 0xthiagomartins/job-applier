# ADR 0001 — Panel stack for the MVP

## Status

Accepted

## Decision

The MVP panel uses:

- Next.js with TypeScript for the frontend
- FastAPI only as the backend API
- local gitignored file persistence until the dedicated persistence epic lands

## Why

- gives the panel a real frontend foundation without mixing UI concerns into the Python backend;
- keeps Python focused on API and automation orchestration;
- matches the likely long-term direction better than server-side templates for this product.

## Consequences

- local development will run two processes: Next.js frontend and FastAPI backend;
- the backend must expose API endpoints with CORS for the panel;
- the panel can evolve independently while the backend remains API-first.
