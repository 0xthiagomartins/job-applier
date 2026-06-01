# Decisions Log

## Broad Role Targets Over Narrow Job Titles

Decision:

- search should use broad role families
- stack specifics should influence scoring and tailoring, not be the main search family

Reason:

- narrow titles like `Python Automation Engineer` were too brittle

## Base CV Identity Is Sovereign

Decision:

- dynamic resumes must preserve the base CV identity

Reason:

- the product should tailor positioning, not rewrite the candidate’s story

## Capability Profile as Screening Memory

Decision:

- screening answers should be backed by a structured capability layer

Reason:

- avoids guessing from the form alone
- gives users review/control

## Competitive but Plausible Screening

Decision:

- use competitive upper-plausible values when exact values are missing

Reason:

- optimize for recruiter filters without going outside reality

## Skip Early Instead of Forcing Broken Applies

Decision:

- closed jobs, external-apply-only jobs, already-applied jobs, and daily-limit states should terminate or skip cleanly

Reason:

- preserves run integrity
- avoids wasted work and confusing loops

## PT-BR Priority in Local Setup

Decision:

- current local target order prioritizes Brazilian roles first

Reason:

- user requested higher priority for Brazilian opportunities

## Dynamic Resume Audit as a First-Class Tool

Decision:

- add a reusable local auditor instead of relying only on visual inspection

Reason:

- faster iteration
- easier handoff to other harnesses
- better regression detection

