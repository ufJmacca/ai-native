---
ainative:
  workflow_profile: reference_driven_web
  references:
    - id: landing-desktop
      label: Marketing landing page desktop reference
      kind: image
      path: ./references/landing-desktop.png
      route: /
      viewport:
        width: 1440
        height: 1600
        label: desktop
      notes: Preserve the hero hierarchy, section order, and card spacing rhythm.
    - id: landing-mobile
      label: Marketing landing page mobile reference
      kind: image
      path: ./references/landing-mobile.png
      route: /
      viewport:
        width: 390
        height: 1200
        label: mobile
    - id: landing-export
      label: Static HTML export of the approved design
      kind: html_export
      path: ./references/landing-export.html
      route: /
      viewport:
        width: 1440
        height: 1600
        label: desktop
  preview:
    url: http://127.0.0.1:4173
    command: npm run dev -- --host 127.0.0.1 --port 4173
    readiness:
      timeout_seconds: 90
      interval_seconds: 1
      expect_status: 200
---
# Reference-Driven Landing Page Refresh

## Goal

Rebuild the landing page to match the supplied design references with high visual fidelity while still fitting the repository's component architecture and conventions.

## Requirements

- Treat the references as the implementation target rather than loose inspiration.
- Reuse stable design primitives such as typography scale, card spacing, and CTA styling across the page.
- Preserve the major section order and content hierarchy from the approved design.
- Keep the implementation responsive and maintain the intended mobile stack behavior.

## Constraints

- Use existing site primitives where they already express the same product language.
- Do not introduce a new design system if the repository already has one.
- Keep implementation notes focused on fidelity-critical decisions and responsive tradeoffs.
