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

Each stage writes artifacts under `TARGET_DIR/.ai-native/runs/<run-id>/`.
The run state also records the target workspace directory, so the same spec can be used against different repositories without colliding.

## Target Workspace

The template repo holds the prompts, schemas, and orchestration code. The actual feature work runs inside a user-provided target repository passed as `TARGET_DIR` in `make` or `--workspace-dir` in the CLI.

- Repository scan and context generation run against the target workspace.
- The configured builder, critic, verifier, and PR review adapters execute in the target workspace.
- Git branch, commit, push, and PR creation also execute in the target workspace.
- If the target workspace is not already a git repository, the workflow initializes one there before stage execution begins.
- Prompt and schema assets still come from the template repo.

## Worktree Scheduler

After `slice`, the workflow switches from serial artifact generation to a git worktree scheduler:

- The scheduler resolves a single immutable `base_ref` for the run from the configured base branch.
- Each ready slice gets its own branch and worktree under `TARGET_DIR/.ai-native/worktrees/<run-id>/<slice-id>/`.
- A slice is ready only when all dependency slice commits are already merged into that `base_ref`.
- Current-run slice completions do not unblock dependents. Dependents wait for prerequisite merges and a resumed run.
- Slices with overlapping `file_impact` entries do not run at the same time.
- The target repository must be clean outside `.ai-native/` before the scheduler starts.

The scheduler records per-slice state in `state.json` and writes a summary under `scheduler/summary.{json,md}`.

## Plan-Mode Planning

The `plan` stage is intentionally multi-pass. It first writes:

1. `plan/grounding.md`
2. `plan/intent.md`
3. `plan/implementation.md`

Those notes are then synthesized into the structured `plan.json` and rendered `plan.md`, followed by the separate `plan-review.md` critique. If the critique requests changes, the planning agent revises the plan and retries up to the configured attempt limit before the stage fails.

Before intent and implementation are locked in, the planning workflow may also emit a small clarification batch. When that happens, the CLI asks the questions directly in the terminal, stores the results under `plan/questions.*` and `plan/answers.*`, and feeds the answers back into the remaining planning passes.

If the plan still fails critique, the workflow saves each attempt as `plan-attempt-N.*` and `plan-review-attempt-N.*`. A resumed run continues from the latest saved critique attempt instead of restarting the entire planning stage. When the configured attempt limit is exhausted, the CLI can ask whether to continue and how many extra attempts to grant.

## Reference-Driven Web Fidelity

Specs can opt into a reference-led web workflow by adding `ainative.workflow_profile: reference_driven_web` YAML frontmatter plus a `references[]` list and `preview` config. This does not add a new top-level stage. Instead, the existing stages receive extra reference-aware behavior:

- `recon` persists a normalized `reference-manifest.json`, a deterministic `reference-scan.json`, and a synthesized `reference-context.{json,md}` artifact.
- `plan`, `prd`, `slice`, and `loop` receive the reference context as an implementation constraint so the supplied design is treated as the target, not inspiration.
- `verify` starts the configured preview when needed, captures full-page screenshots at the declared routes and viewports, runs a dedicated visual critique, and only allows final verification to pass after the visual review is approved.

Codex supports the full multimodal path for this profile. Copilot can still participate when the references include machine-readable inputs such as `html_export` or `url`, but image-only reference sets fail early with an actionable capability error.

## Ralph Loop Contract

Each slice runs through the following sequence:

1. Write or adjust tests first.
2. Capture a failing test run as `red.log`.
3. Critique the tests for behavioral value and Triple A quality.
4. Implement until the slice is green and capture `green.log`.
5. Refactor and record the reasoning in `refactor-notes.md`.
6. Run a separate verifier agent before allowing commit or PR stages.

For `reference_driven_web` slices, `verify` also runs a screenshot capture and visual-fidelity critique loop before the normal verification report is allowed to pass.

When using `make run`, each ready slice runs `loop -> verify -> commit -> pr` inside its own worktree. Commits remain slice-specific, and blocked slices stay pending until their dependencies are merged into the configured base branch.

## Critique Stages

The following artifacts require separate critiques:

- plan
- architecture diagram
- PRD
- tests created during loop execution
- PR diff before opening the pull request

## Run State

Every run persists `state.json` so the workflow can be resumed without guessing which stages already completed.
Manual recovery commands also support slice targeting:

- `make loop SPEC=... TARGET_DIR=... SLICE=S001`
- `make verify SPEC=... TARGET_DIR=... SLICE=S001`
- `make commit SPEC=... TARGET_DIR=... SLICE=S001`
- `make pr SPEC=... TARGET_DIR=... SLICE=S001`

If `SLICE` is omitted, the CLI only proceeds when exactly one non-completed slice candidate remains for that stage.
