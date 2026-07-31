# Existing-module inventory for the factory runner

This inventory records the integration decisions implemented through AN-03.
Factory execution uses a restricted dispatcher around selected stage ports; it
never invokes the legacy orchestrator or its publication stages.

## Reuse through a factory adapter

| Module | Existing responsibility | Factory-runner treatment |
|---|---|---|
| `ai_native/cli.py` | CLI parser and legacy dispatch | Keeps legacy paths and lazily dispatches `factory run` and `factory verify` |
| `ai_native/adapters/base.py` | Agent and review ports | Reused by the attempt-scoped, process-bounded [gateway adapter](gateway-contract.md) |
| `ai_native/orchestrator.py` | Stateful stage scheduling | Not imported by factory execution; selected stage handlers are dispatched through a restricted registry |
| `ai_native/stages/common.py` | Execution context and stage failures | Reuse after constructing a factory-safe context |
| `ai_native/stages/planning.py` | Planning workflow | Reused only when `plan` is admitted |
| `ai_native/stages/architecture.py` | Architecture workflow | Reused only when `architecture` is admitted |
| `ai_native/stages/prd.py` | PRD workflow | Reused only when `prd` is admitted |
| `ai_native/stages/slicing.py` | Slice planning | Reused only when `slice` is admitted |
| `ai_native/stages/loop.py` | Test-first implementation loop | Reused only when `loop` is admitted |
| `ai_native/stages/verify.py` | Agent-assisted authoring review | Reused only in author mode; clean verify mode never instantiates an agent |
| `ai_native/state.py` | Legacy local run state | Remains in ephemeral runner scratch; allowlisted private bytes are tokenised into content-addressed checkpoint objects at safe boundaries |
| `ai_native/workspace_artifacts.py` | Workflow artifact locations | Redirected to ephemeral runner scratch in factory mode |
| `ai_native/prompts/` | Installed prompt assets | Reused only for admitted authoring stages |

The factory writes a private specification from every immutable task decision
and the digest-verified `ContextBundle`: outcome, acceptance criteria,
non-goals, constraints, repository instructions, trusted policy summary,
approved repository memory, dependency outputs, and operator input. It
materialises the factory-safe context report from the same admitted policy and
path authority and does not fetch mutable recon context. `intake` and `recon`
execute only when the current `RunSpec` explicitly admits them; recon may scan
the prepared workspace and use the bounded gateway but cannot fetch
undeclared external context.

## AN-02 and AN-03 factory-only modules

| Module | Responsibility |
|---|---|
| `factory_runner/admission.py` | Contract, digest, identity, capability, workspace, ChangeSet, path, and repository-topology admission |
| `factory_runner/runner.py` | Operation separation, safe-boundary coordination, exit classification, and terminal result dispatch |
| `factory_runner/author.py` | Restricted legacy-stage adapter with turn/token budgets and non-interactive clarification handling |
| `factory_runner/verification.py` | Exact phased command execution, genuine-red classification, and authoring versus clean-verification evidence |
| `factory_runner/process.py` | Closed-stdin process supervision, bounded capture, cancellation, deadlines, process-group cleanup, and Linux detached-descendant reaping |
| `factory_runner/process_policy.py` | Environment filtering, credential rejection, trusted executable resolution, and immutable Git/GitHub/shell denial |
| `factory_runner/git_runtime.py` | Bounded runner-owned Git inspection through the central process supervisor |
| `factory_runner/changes.py` | Repository boundary checks plus deterministic add, modify, delete, rename, binary, mode, patch, and ChangeSet generation |
| `factory_runner/outputs.py` | Bounded no-follow atomic artifacts, staged event support, typed protocol manifests, and completion-last sealing |
| `factory_runner/events.py` | Ordered canonical NDJSON staging, durable append, optional identical stdout mirroring, and atomic finalisation |
| `factory_runner/checkpoints.py` | Immutable checkpoint publication, artifact validation, and transactional workspace restore |
| `factory_runner/checkpoint_runtime.py` | Portable checkpoint construction, lineage, authority, budgets, and content-addressed state objects |
| `factory_runner/evidence.py` | Evidence status derivation and expected behavioral Red classification |
| `factory_runner/phase_checkpoint.py` | Snapshot and restore of complete phased evidence across attempts |
| `factory_runner/private_state.py` | Bounded path-tokenised snapshot and restore of private author scratch state |
| `factory_runner/redaction.py` | Producer-side text redaction and streaming secret-canary detection |
| `factory_runner/attempt_secrets.py` | Attempt-scoped secret policy construction without persisting credential values |
| `factory_runner/workflow_adapter.py` | Documented attempt-scoped gateway file contract, process adaptation, and protection of runner-owned state |

## Legacy-only or prohibited in factory mode

| Module or path | Reason |
|---|---|
| `ai_native/stages/git_pr.py` | Creates commits and pull requests |
| `gitops.ensure_branch`, `commit_all`, `push_branch`, `create_pull_request` | Publication authority belongs to the trusted factory publisher |
| `cli._ask_questions` and telemetry configuration prompts | Factory mode cannot request interactive input |
| `ai_native/run_registry.py` remote publishing | Runner outputs are local; durable upload is a factory responsibility |
| Telemetry remote destinations | Repository configuration cannot create runner egress |
| `services/run_registry*` | Existing optional services are not the private factory control plane |

These module and command exclusions constrain runner dispatch; they do not
claim that Python can contain a hostile nested process. A directly admitted
Python, test, or gateway child remains inside the private factory's outer
filesystem, process, credential, and network sandbox.

## Legacy coupling kept behind the factory boundary

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
- `StageName` and related Pydantic `Literal` types remain aligned
  with the dependency-free runtime constants in
  `ai_native/workflow_stages.py`; changing stage names requires both runtime
  and persisted-model compatibility review.

AN-00 centralises runtime stage groupings in `ai_native/workflow_stages.py`.
The `ai_native.stages.capabilities` module is a compatibility re-export so
low-level modules do not import the handler package and create cycles.

## Deferred beyond AN-03

- wheel or OCI release certification and receipts (AN-04);
- all `ai-native-factory` implementation code.
