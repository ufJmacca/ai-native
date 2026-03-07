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
The run state also records the target workspace directory, so the same spec can be used against different repositories without colliding.

## Target Workspace

The template repo holds the prompts, schemas, and orchestration code. The actual feature work runs inside a user-provided target repository passed as `TARGET_DIR` in `make` or `--workspace-dir` in the CLI.

- Repository scan and context generation run against the target workspace.
- Codex builder, critic, verifier, and PR review commands execute in the target workspace.
- Git branch, commit, push, and PR creation also execute in the target workspace.
- Prompt and schema assets still come from the template repo.

## Plan-Mode Planning

The `plan` stage is intentionally multi-pass. It first writes:

1. `plan/grounding.md`
2. `plan/intent.md`
3. `plan/implementation.md`

Those notes are then synthesized into the structured `plan.json` and rendered `plan.md`, followed by the separate `plan-review.md` critique. If the critique requests changes, the planning agent revises the plan and retries up to the configured attempt limit before the stage fails.

Before intent and implementation are locked in, the planning workflow may also emit a small clarification batch. When that happens, the CLI asks the questions directly in the terminal, stores the results under `plan/questions.*` and `plan/answers.*`, and feeds the answers back into the remaining planning passes.

If the plan still fails critique, the workflow saves each attempt as `plan-attempt-N.*` and `plan-review-attempt-N.*`. A resumed run continues from the latest saved critique attempt instead of restarting the entire planning stage. When the configured attempt limit is exhausted, the CLI can ask whether to continue and how many extra attempts to grant.

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
