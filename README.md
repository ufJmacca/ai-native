# AI Native Base

`ai-native-base` is a cloneable template for AI-native product development inside a devcontainer. It takes a feature or product description, performs a repository review, produces implementation artifacts, critiques those artifacts with separate agent runs, iterates with Ralph loops, verifies the result, and prepares commits and PRs.

## What The Template Includes

- Python-first orchestration with `uv`.
- Devcontainer and Docker Compose setup for reproducible execution.
- Host credential inheritance for Codex, SSH, Git identity, and optional GitHub CLI auth inside the devcontainer.
- Structured prompts, JSON schemas, state management, and stage orchestration.
- TDD gates that enforce red/green/refactor and Triple A style test reviews.
- CI that builds and runs the template inside Docker.

## Quick Start

1. Open the repository in a devcontainer.
2. Confirm the devcontainer mounted `~/.codex`, `~/.ssh`, and `~/.gitconfig`.
3. Run `make bootstrap`.
4. Pick the target repository directory you want the agents to modify.
5. Copy or create a spec inside that target repository.
6. Run `make run SPEC=specs/task-management.md TARGET_DIR=/path/to/target-repo`.

The `Makefile` auto-detects whether it is running inside the devcontainer. Inside the devcontainer it runs `uv` commands directly. On the host it shells out through `docker compose run`.
`TARGET_DIR` is mandatory for the workflow targets in `make`. The workflow runs Codex, git operations, repository recon, and implementation inside that target directory rather than inside the template repo. Relative spec paths are resolved from `TARGET_DIR`.
If a relative spec path is not present under `TARGET_DIR`, the CLI falls back to the same relative path in the template repo.
If the planning step needs clarification, `make run` now pauses and asks the questions directly in the terminal, then feeds the answers back into the planning loop.
If planning fails after exhausting its current attempt budget, a resumed run now continues from the latest saved critique attempt rather than restarting grounding/intent/implementation, and the CLI can ask whether to grant additional planning attempts.

## Core Targets

- `make doctor`
- `make bootstrap`
- `make plan SPEC=... TARGET_DIR=...`
- `make architect SPEC=... TARGET_DIR=...`
- `make prd SPEC=... TARGET_DIR=...`
- `make slice SPEC=... TARGET_DIR=...`
- `make loop SPEC=... TARGET_DIR=...`
- `make verify SPEC=... TARGET_DIR=...`
- `make commit SPEC=... TARGET_DIR=...`
- `make pr SPEC=... TARGET_DIR=...`
- `make run SPEC=... TARGET_DIR=...`

## Auth Model

The devcontainer inherits the following host paths:

- `~/.codex/auth.json`
- `~/.codex/config.toml`
- `~/.ssh/`
- `~/.gitconfig`
- `~/.config/gh/` when present

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

See [docs/workflow.md](/Users/jonmcmillin/ai-native-base/docs/workflow.md) for the full stage contract and [docs/prompts.md](/Users/jonmcmillin/ai-native-base/docs/prompts.md) for prompt design guidance.
