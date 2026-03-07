# Workflow

## Stage Order

The template treats the workflow as a stateful pipeline:

1. `intake`
2. `recon`
3. `plan`
4. `architecture`
5. `prd`
6. `slice`
7. `loop`
8. `verify`
9. `commit`
10. `pr`

Each stage writes artifacts under `artifacts/<run-id>/`.

## Plan-Mode Planning

The `plan` stage is intentionally multi-pass. It first writes:

1. `plan/grounding.md`
2. `plan/intent.md`
3. `plan/implementation.md`

Those notes are then synthesized into the structured `plan.json` and rendered `plan.md`, followed by the separate `plan-review.md` critique.

## Ralph Loop Contract

Each slice runs through the following sequence:

1. Write or adjust tests first.
2. Capture a failing test run as `red.log`.
3. Critique the tests for behavioral value and Triple A quality.
4. Implement until the slice is green and capture `green.log`.
5. Refactor and record the reasoning in `refactor-notes.md`.
6. Run a separate verifier agent before allowing commit or PR stages.

## Critique Stages

The following artifacts require separate critiques:

- plan
- architecture diagram
- PRD
- tests created during loop execution
- PR diff before opening the pull request

## Run State

Every run persists `state.json` so the workflow can be resumed without guessing which stages already completed.
