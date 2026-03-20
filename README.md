# AI Native Base

`ai-native-base` is a cloneable template for AI-native product development inside a devcontainer. It takes a feature or product description, performs a repository review, produces implementation artifacts, critiques those artifacts with separate agent runs, iterates with Ralph loops, verifies the result, and prepares commits and PRs.

## What The Template Includes

- Python-first orchestration with `uv`.
- Devcontainer and Docker Compose setup for reproducible execution.
- Host credential inheritance for SSH, Git identity, and optional Codex, GitHub CLI, and GitHub Copilot CLI auth inside the devcontainer.
- Structured prompts, JSON schemas, state management, and stage orchestration.
- TDD gates that enforce red/green/refactor and Triple A style test reviews.
- CI that builds and runs the template inside Docker.

## Quick Start

1. Open the repository in a devcontainer.
2. Confirm the devcontainer mounted `~/.ssh` and `~/.gitconfig`. Mount `~/.codex` only if you plan to use Codex, and `~/.copilot` only if you plan to use GitHub Copilot CLI.
3. Run `make bootstrap`.
4. Pick the target repository directory you want the agents to modify.
5. Copy or create a spec inside that target repository.
6. Run `make run SPEC=specs/task-management.md TARGET_DIR=/path/to/target-repo`.

The `Makefile` auto-detects whether it is running inside the devcontainer. Inside the devcontainer it runs `uv` commands directly. On the host it shells out through `docker compose run`.
`TARGET_DIR` is mandatory for the workflow targets in `make`. The workflow runs the configured agent adapter, git operations, repository recon, and implementation inside that target directory rather than inside the template repo. Relative spec paths are resolved from `TARGET_DIR`.
`TARGET_DIR` must be either a standalone directory that ai-native can initialize as a git repository or an existing repository root. Nested directories inside another repository are rejected to avoid binding worktrees, branches, and PRs to the wrong git root.
If a relative spec path is not present under `TARGET_DIR`, the CLI falls back to the same relative path in the template repo.
If the planning step needs clarification, `make run` now pauses and asks the questions directly in the terminal, then feeds the answers back into the planning loop.
Every run now persists its state and artifacts under `TARGET_DIR/.ai-native/runs/<run-id>/`, so the execution record stays with the target repository instead of the template repo. If `TARGET_DIR` is not already a git repository, the workflow initializes one there on the configured base branch before agent execution starts.
Inside the devcontainer, nested `codex exec` runs default to unsandboxed non-interactive execution because the devcontainer is the outer isolation boundary and Linux Landlock has proven unreliable for nested Codex sessions. Set `AINATIVE_CODEX_CONTAINER_SANDBOX` if you need to override that default.
If planning fails after exhausting its current attempt budget, a resumed run now continues from the latest saved critique attempt rather than restarting grounding/intent/implementation, and the CLI can ask whether to grant additional planning attempts.
`make run` now schedules ready slices in parallel via git worktrees under `TARGET_DIR/.ai-native/worktrees/<run-id>/`. A slice is only runnable when its dependencies satisfy the configured `workspace.dependency_policy` and its `file_impact` does not overlap any currently running slice. With `dependency_policy: wait_for_base_merge`, dependent slices wait until prerequisite commits land on the configured base branch. With `dependency_policy: assume_committed`, dependent slices become runnable once prerequisite slices reach the commit stage, and their worktrees merge those dependency commits locally before execution continues.
When `dependency_policy: assume_committed` is enabled, downstream branches may temporarily contain upstream slice changes until the earlier slices are actually merged to the base branch.
PR creation also stacks when possible: if a slice has a single deepest dependency branch, its PR targets that dependency branch instead of `main`. Slices with multiple incomparable dependencies still fall back to the configured base branch.
Because the scheduler creates worktrees from the base branch, the target repository must be clean outside `.ai-native/` before `make run` starts the slice phase.

## Install As A CLI

You can also install `ai-native-base` as a reusable CLI and run it from other repositories:

1. Install it with `uv tool install /path/to/ai-native-base` for local development, or publish it and install with `uv tool install ai-native-base`.
2. From the target repository, run `ainative doctor` to confirm the runtime and selected-provider auth setup.
3. Run the workflow directly from that repository, for example `ainative run --spec specs/my-feature.md`.

The installed CLI now loads prompts and schemas from the package itself, so it does not need this template checkout at runtime.
If `ainative.yaml` exists in the current repository or one of its parent directories, the CLI uses it automatically.
If no config file is present, the CLI falls back to built-in defaults that mirror the template's current agent setup.
If you want to share a single config across repositories, pass `--config /path/to/ainative.yaml` or set `AINATIVE_CONFIG=/path/to/ainative.yaml`.
`ainative doctor` reports readiness for both Codex and Copilot, but only the provider selected by your current agent config is meaningfully required. If you want Copilot-only execution, start from [docs/examples/ainative.copilot.yaml](docs/examples/ainative.copilot.yaml).

Telemetry settings can be managed directly from the CLI with `ainative telemetry configure`, inspected with `ainative telemetry show`, and validated with `ainative telemetry test`.
Telemetry secrets are masked in CLI output, and remote settings can also be overridden with `AINATIVE_TELEMETRY_*` environment variables.
Run registry publishing can be enabled separately with `registry.remote_url` and `registry.auth_token` in `ainative.yaml`, or with `AINATIVE_RUN_REGISTRY_URL` and `AINATIVE_RUN_REGISTRY_AUTH_TOKEN`.
GitHub Copilot CLI is also supported as a first-class adapter. Use the standalone `copilot` binary rather than `gh copilot`, trust the target workspace in Copilot CLI first, and start from [docs/examples/ainative.copilot.yaml](docs/examples/ainative.copilot.yaml) if you want a ready-made agent profile set.
See [docs/configuration.md](docs/configuration.md) for details.
For the standalone registry backend and operator dashboard, see [docs/self-hosted-run-registry.md](docs/self-hosted-run-registry.md).

## Core Targets

- `make doctor`
- `make bootstrap`
- `make plan SPEC=... TARGET_DIR=...`
- `make architect SPEC=... TARGET_DIR=...`
- `make prd SPEC=... TARGET_DIR=...`
- `make slice SPEC=... TARGET_DIR=...`
- `make loop SPEC=... TARGET_DIR=... SLICE=S001`
- `make verify SPEC=... TARGET_DIR=... SLICE=S001`
- `make commit SPEC=... TARGET_DIR=... SLICE=S001`
- `make pr SPEC=... TARGET_DIR=... SLICE=S001`
- `make run SPEC=... TARGET_DIR=...`

## Auth Model

The devcontainer inherits the following host paths:

- `~/.codex/` when present and you want to use Codex
- `~/.copilot/` when present
- `~/.ssh/`
- `~/.gitconfig`
- `~/.config/gh/` when present

`~/.ssh/` and `~/.gitconfig` are the only host mounts required for the devcontainer bootstrap checks. Codex and Copilot credentials are optional and only needed for the provider you configure.
The root `compose.yaml` does not require host auth mounts so CI and headless smoke tests can run without secrets. The devcontainer override adds the host credentials for interactive AI-native development.

## Workflow Stages

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

See [docs/workflow.md](docs/workflow.md) for the full stage contract and [docs/prompts.md](docs/prompts.md) for prompt design guidance.
For self-hosted runtime hardening guidance (auth modes, isolation, validation, audit logging, secret handling, and production topology), see [docs/self-hosted-runtime-security.md](docs/self-hosted-runtime-security.md).
