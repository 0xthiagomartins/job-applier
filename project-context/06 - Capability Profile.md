# Capability Profile

## Purpose

The capability profile is the product’s structured candidate-memory layer.

It exists so the agent does not answer screening questions by guessing from the current form alone.

## What It Contains

Per capability, the system can store:

- capability name
- min years
- max years
- recommended years
- confidence
- source
- enabled/disabled state
- optional user override

Examples:

- Python
- JavaScript
- TypeScript
- AWS
- Linux
- RPA
- UiPath
- LangChain

## Sources

Current sources include:

- explicit profile years
- reviewed user overrides
- inferred ranges from the base CV

## Priority Rules

Priority order:

1. exact explicit profile values
2. reviewed user overrides
3. inferred competitive-but-plausible ranges

## Product Policy

The current screening policy is not “perfect truth estimation”.
It is:

- plausible
- competitive
- bounded by the candidate’s reality

That means the system may choose the upper plausible range when trying to pass recruiter filters, but it should not jump into fantasy.

## Panel Role

The panel exposes this profile so the user can:

- accept the inference
- tighten it
- raise it when under-confident
- disable misleading capabilities

## Why This Matters

Without this layer, the agent would:

- overfit to the text of one question
- under-answer strong skills
- or invent inconsistent experience

