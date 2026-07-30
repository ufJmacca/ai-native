# Existing-module inventory for the factory runner

This inventory records AN-00 integration decisions. It does not authorize
factory execution through the legacy orchestrator.

## Reuse through a factory adapter

| Module | Existing responsibility | Factory-runner treatment |
|---|---|---|
| `ai_native/cli.py` | CLI parser and legacy dispatch | Keep legacy paths; attach future factory subcommands to the reserved group |
| `ai_native/adapters/base.py` | Agent and review ports | Reuse the ports with an attempt-scoped factory adapter |
| `ai_native/orchestrator.py` | Stateful stage scheduling | Reuse selected workflow behavior behind a restricted adapter, not `run_all` unchanged |
| `ai_native/stages/common.py` | Execution context and stage failures | Reuse after constructing a factory-safe context |
| `ai_native/stages/planning.py` | Planning workflow | Candidate authoring stage |
| `ai_native/stages/architecture.py` | Architecture workflow | Candidate authoring stage |
| `ai_native/stages/prd.py` | PRD workflow | Candidate authoring stage |
| `ai_native/stages/slicing.py` | Slice planning | Candidate authoring stage |
| `ai_native/stages/loop.py` | Test-first implementation loop | Candidate authoring stage and evidence source |
| `ai_native/stages/verify.py` | Current verification workflow | Reuse selectively; clean factory verification needs a deterministic operation |
| `ai_native/state.py` | Legacy local run state | Map safe structured state into future checkpoints; do not expose its storage layout as protocol |
| `ai_native/workspace_artifacts.py` | Workflow artifact locations | Reuse content where safe, then serialise through protocol output writers |
| `ai_native/gitops` | Git workspace primitives | Reuse read-only inspection and patch-related primitives only |
| `ai_native/prompts/` | Installed prompt assets | Reuse only for permitted authoring stages |

`recon` and `intake` behavior will be evaluated in AN-02 against the immutable
context bundle. The factory must not fetch mutable context that was not
declared in its input.

## Legacy-only or prohibited in factory mode

| Module or path | Reason |
|---|---|
| `ai_native/stages/git_pr.py` | Creates commits and pull requests |
| `gitops.ensure_branch`, `commit_all`, `push_branch`, `create_pull_request` | Publication authority belongs to the trusted factory publisher |
| `cli._ask_questions` and telemetry configuration prompts | Factory mode cannot request interactive input |
| `ai_native/run_registry.py` remote publishing | Runner outputs are local; durable upload is a factory responsibility |
| Telemetry remote destinations | Repository configuration cannot create runner egress |
| `services/run_registry*` | Existing optional services are not the private factory control plane |

## Existing coupling that later phases must isolate

- `WorkflowOrchestrator` constructs all configured adapters and registers
  `commit` and `pr` handlers eagerly.
- `run_all` schedules `loop → verify → commit → pr`; `dry_run_pr` still
  reaches the commit stage and is not a factory mode.
- missing-config provider selection inspects executable and home-directory
  authentication signals.
- the external-command adapter inherits the full process environment.
- the Codex adapter may select `danger-full-access` inside a container, while
  Copilot profiles may request broad permissions.
- registry and telemetry settings can activate remote endpoints.
- `StageName` and related Pydantic `Literal` types remain manually aligned
  with the dependency-free runtime constants in
  `ai_native/workflow_stages.py`; changing stage names requires both runtime
  and persisted-model compatibility review.

AN-00 centralises runtime stage groupings in `ai_native/workflow_stages.py`.
The `ai_native.stages.capabilities` module is a compatibility re-export so
low-level modules do not import the handler package and create cycles.

## Leave untouched in AN-00

- protocol contracts and JSON Schemas;
- non-interactive author and verify execution;
- checkpoints, events, evidence, change sets, and result manifests;
- wheel or OCI release changes;
- all `ai-native-factory` implementation code.

