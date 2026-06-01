# Resume Modes

## Static Mode

`static` means:

- use the uploaded base CV exactly
- do not generate a tailored markdown/PDF variant
- upload the base file in the application flow

This is the strictest and safest mode.

## Dynamic Mode

`dynamic` means:

- keep the base CV as the factual source of truth
- generate a tailored resume per matched job
- preserve identity, history, dates, employers, education, and certifications
- emphasize the target job only within grounded evidence

## Design Contract

The product contract is:

- base identity must survive dynamic tailoring
- dynamic mode must never become a freeform rewrite
- tailoring may strengthen positioning, but not invent experience
- if safe tailoring cannot be produced, the system should fall back instead of shipping a misleading resume

## Current Operational Behavior

Dynamic mode currently:

- can build job-specific markdown
- renders PDF with a CSS-backed theme
- tracks source and target resume languages
- can fall back to base CV when language alignment or rendering fails

## Current Caveat

Portuguese localization is much better than before, but live PT artifacts still need final polishing in some skill label/suffix cases. See [[07 - Internationalization]] and [[10 - Known Risks]].

