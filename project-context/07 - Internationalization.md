# Internationalization

## Current Direction

The product is English-first, but must support multilingual operation.

Current explicit supported path:

- English
- Portuguese

## Current Contract

- product default language can be English
- job language can differ from base CV language
- dynamic resume should try to target the vacancy language
- if safe localization fails, the system should fall back rather than ship a mixed-language document

## Current Components

- job posting language detection
- base CV language detection
- localized section labels
- localized skill category labels
- localization passes for structured resume items
- residual repair pass for short or stale untranslated fields
- language-aware output validation
- language-aware audit checks

## Recent Progress

Recent commits significantly improved PT localization:

- `e8ed4af`
- `39f1cc3`
- `9bc07a3`
- `7725b9b`
- `adaf322`

## Important Current Truth

This area is improved, but not fully “done”.

What is solid:

- PT vacancy detection works
- PT generation path exists
- mixed-language artifacts are now easier to detect
- builder rejects some unsafe localized outputs instead of silently using them

What still needs final confidence:

- some Portuguese dynamic resumes still leak English-heavy skill/interests suffixes or labels in live artifacts
- the final polish on PT output must be validated on a fresh live or production-like artifact

## Safer Sharing Guidance

If you need another harness to continue this area:

- share the repo
- share this vault
- share sanitized PT artifacts or synthetic PT scenarios
- avoid sharing the real CV unless you intend to

