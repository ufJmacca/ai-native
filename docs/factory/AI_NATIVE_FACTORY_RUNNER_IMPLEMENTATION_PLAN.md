# AI Native Factory Runner

## Public repository implementation plan

**Status:** Proposed  
**Date:** 30 July 2026  
**Repository:** [`ufJmacca/ai-native`](https://github.com/ufJmacca/ai-native)  
**Repository role:** Public, reusable execution engine and development template  
**Protocol:** `factory-runner-protocol/v1`  
**Required handoff:** A formal, machine-readable factory-runner release receipt  

---

## 1. How this plan fits into the two-repository build

This document covers changes made **only** in the public `ai-native`
repository. It is the first of the two implementation plans and must be
executed before the private factory begins consuming the runner.

Execution order:

1. Complete `AN-00` through `AN-04` serially in this repository, with one
   branch and one PR per phase.
2. Keep every required CI, security, policy, packaging, and compatibility
   check blocking.
3. After the phase evidence is complete, have the trusted publisher/control
   plane mark the PR ready and enable GitHub auto-merge.
4. Verify that GitHub has actually merged the PR to the default branch before
   starting the next phase from the updated default branch.
5. Complete the post-merge release job for `AN-04`.
6. Obtain and verify the `factory-runner-protocol/v1` release receipt.
7. Start the private factory repository's `FF-00` phase using that receipt.

```mermaid
flowchart LR
    A0["AN-00\nBoundary"] --> A1["AN-01\nContracts"]
    A1 --> A2["AN-02\nRunner"]
    A2 --> A3["AN-03\nOutputs"]
    A3 --> A4["AN-04\nRelease"]
    A4 --> R["Release receipt"]
    R --> F0["Factory FF-00"]
```

The private factory must not consume an unmerged AI Native branch, an
unpinned Git dependency, a mutable OCI tag, or locally copied contract files.
The release receipt is the only supported handoff from this plan to the
factory's first phase.

Routine progression is automated. Operator intervention is reserved for a
true exception such as a scope ambiguity, security finding, missing
permission, unresolved merge conflict, or failed required check. Resolving an
exception never waives a required check or branch-protection rule; the
protected automated path resumes after the underlying issue is corrected.

---

## 2. Intended outcome

AI Native will remain a useful interactive CLI and cloneable development
template while gaining a stable, non-interactive execution surface that a
separate factory can run inside an ephemeral sandbox.

The public repository will own:

- versioned, factory-neutral runner contracts;
- validation and compatibility rules for those contracts;
- a deterministic, non-interactive runner command;
- adapters around the existing AI Native workflow;
- local event, checkpoint, evidence, change-set, and result outputs;
- factory-mode safety defaults;
- a reusable authoring operation;
- a deterministic verification operation suitable for a separate clean
  sandbox;
- a distributable Python wheel;
- a digest-pinned OCI runner image;
- compatibility fixtures and a conformance test suite;
- supply-chain metadata and a formal release receipt.

The public repository will **not** own the factory queue, durable workflow
service, sandbox allocation service, publication service, database, operator
UI, or deployment.

---

## 3. Existing behaviour that must be preserved

The following existing AI Native responsibilities remain supported:

- the `ainative` CLI and its existing commands;
- the cloneable repository template;
- interactive developer use inside the devcontainer;
- `uv`-managed Python 3.12+ development;
- repository recon, planning, architecture, PRD, slicing, implementation,
  verification, commit, and pull-request stages;
- red → green → refactor enforcement;
- per-run files beneath `.ai-native/runs/<run-id>/`;
- worktree-based slice execution;
- Codex and other configured agent adapters;
- installed-CLI use from a target repository;
- existing telemetry and run-registry support for interactive mode;
- existing Make targets and configuration behaviour.

Factory mode is an additional hardened execution profile. It must not silently
change the defaults of existing interactive commands.

Characterisation tests added in `AN-00` must protect these behaviours through
all later phases.

---

## 4. Repository boundary

### 4.1 AI Native owns

| Capability | AI Native responsibility |
|---|---|
| Protocol | Define and validate `factory-runner-protocol/v1` |
| Runner | Turn a validated `RunSpec` into local outputs |
| Agent execution | Adapt existing planning and implementation stages |
| TDD evidence | Capture structured red, green, refactor, and authoring verification evidence |
| Verification operation | Run supplied deterministic commands without agent authoring |
| Checkpoint representation | Create portable, content-addressed checkpoints |
| Change representation | Produce a sanitised `ChangeSet`; never publish it |
| Event production | Write ordered, sanitised attempt-local events |
| Packaging | Publish a wheel and OCI image |
| Compatibility | Maintain golden fixtures and a conformance suite |
| Release handoff | Publish a signed release receipt with immutable references |

### 4.2 The private factory owns

The following are explicit non-goals for this repository:

- GitHub Issue Forms, labels, webhooks, reconciliation, or queue admission;
- Temporal or another durable control-plane implementation;
- work-item, run, attempt, or lease database tables;
- worker leasing, fencing, retries, or scheduling;
- creation or destruction of Docker containers;
- host network policy and container-runtime enforcement;
- GitHub App installation-token handling;
- branch creation, pushing, PR creation, PR updates, review polling, or merge;
- authoritative event storage, transactional outbox, SSE, or Mattermost;
- object-store upload and durable acknowledgement;
- operator authentication, commands, dashboard, or deployment;
- cross-repository memory;
- production service topology.

AI Native may define neutral contracts or hooks needed by those capabilities,
but it must not implement the platform around them.

### 4.3 Authority boundary

Factory mode may:

- read a prepared target workspace at an exact base commit;
- read a validated context bundle;
- run configured AI Native stages;
- invoke explicitly supplied test and verification commands;
- modify files only inside the target workspace;
- write protocol outputs only beneath an explicit output directory;
- make model calls through an explicitly configured, attempt-scoped gateway;
- emit sanitised local events and checkpoints;
- produce a patch or Git bundle and supporting evidence.

Factory mode may not:

- prompt a human;
- infer missing required input from an interactive terminal;
- read mutable issue or PR content directly;
- call GitHub;
- push, commit for publication, create a branch, create a PR, or merge;
- run the existing `commit` or `pr` publication stages;
- discover credentials from a host home directory;
- mount or access the Docker socket;
- upload events, artifacts, or telemetry using repository-supplied endpoints;
- widen its allowed paths, commands, network destinations, or budget;
- claim that authoring-sandbox verification is independent clean verification.

---

## 5. Protocol design principles

`factory-runner-protocol/v1` is the public integration boundary. The factory
must depend on this protocol rather than importing AI Native workflow
internals.

The protocol must be:

- **versioned:** every document declares a protocol and schema version;
- **portable:** JSON documents plus content-addressed files;
- **language-neutral:** the factory need not import Python models;
- **strict:** unknown fields fail validation unless explicitly designated as
  extensions;
- **immutable:** input documents are not rewritten;
- **content-addressed:** important documents and artifacts carry SHA-256
  digests;
- **attempt-scoped:** events and mutable outputs cannot cross attempt
  boundaries;
- **repository-scoped:** all repository-dependent documents identify the
  repository and base SHA;
- **non-secret:** secret values are prohibited in durable protocol documents;
- **forward-compatible:** a v1 consumer can reject an unsupported major
  version clearly while tolerating declared minor capabilities;
- **independent of factory technology:** no Temporal, PostgreSQL, GitHub App,
  Docker API, or deployment object appears in the protocol.

Python Pydantic models are the implementation source of truth. Checked-in JSON
Schemas and examples are generated from those models and verified for drift in
CI.

---

## 6. Invocation and filesystem contract

### 6.1 Commands

Add a subcommand beneath the existing CLI:

```bash
ainative factory run \
  --run-spec /factory/input/run-spec.json \
  --output-dir /factory/output
```

For a clean verification sandbox:

```bash
ainative factory verify \
  --run-spec /factory/input/run-spec.json \
  --output-dir /factory/output
```

Both commands must:

- work without a TTY;
- never call `input()` or an equivalent prompt;
- consume all required decisions from the input documents;
- validate inputs before executing repository code;
- use stable exit codes;
- write a terminal `RunResult` for every failure occurring after the output
  directory is validated;
- handle `SIGTERM` at safe stage boundaries by creating a best-effort
  checkpoint and terminal result;
- write protocol data to files, not mixed human prose on standard output.

Human-readable log output may go to standard error. Standard output is reserved
for newline-delimited, schema-valid runner events when streaming is enabled.

### 6.2 Input layout

```text
/factory/input/
  run-spec.json
  context/
    context-bundle.json
    objects/
      <sha256>
  resume/
    checkpoint.json       # optional
    objects/              # optional checkpoint objects
```

The target Git checkout is mounted separately at the exact path declared by
`RunSpec.workspace`. The runner verifies that:

- the path is a repository root;
- `HEAD` equals the declared base SHA for authoring runs;
- the worktree satisfies the declared cleanliness policy;
- all referenced context objects match their digests;
- an optional checkpoint is compatible before it is applied.

### 6.3 Output layout

```text
/factory/output/
  protocol-manifest.json
  events.ndjson
  checkpoints/
    <sequence>/
      checkpoint.json
      objects/
  evidence/
    verification-evidence.json
    objects/
  changeset/
    change-set.json
    change.patch
    objects/
  result/
    run-result.json
  completion.json
```

Requirements:

- write each JSON document to a temporary sibling and atomically rename it;
- calculate and record SHA-256 after the final bytes are written;
- never follow an output symlink outside the output root;
- use monotonically increasing checkpoint and event sequence numbers;
- write `completion.json` last;
- treat a missing `completion.json` as an interrupted attempt;
- include a manifest digest in `completion.json`;
- make repeat reads safe after process exit;
- never mutate an acknowledged checkpoint.

The runner writes to local storage only. Upload, acknowledgement, retention,
and deletion are factory responsibilities.

---

## 7. Versioned contracts

All identifiers are opaque strings. All timestamps use UTC RFC 3339. All
digests use lowercase `sha256:<hex>`. Repository paths are POSIX-style,
relative, normalised, and may not contain `..`.

### 7.1 Common envelope fields

Every top-level contract contains:

| Field | Requirement |
|---|---|
| `protocol` | Exactly `factory-runner-protocol/v1` |
| `schema` | Contract name and v1 schema identifier |
| `schema_version` | Integer `1` |
| `created_at` | UTC RFC 3339 timestamp |
| `run_id` | Factory-supplied opaque run identity |
| `attempt_id` | Factory-supplied opaque attempt identity |
| `correlation_id` | Factory-supplied opaque correlation identity |

Documents that describe a source revision also contain:

- `work_item_id`;
- `work_item_revision_id`;
- `delivery_phase_id`;
- `repository_id`;
- `base_commit_sha`.

The runner preserves factory-supplied identities byte-for-byte and never
generates replacements for them.

### 7.2 `RunSpec`

`RunSpec` is the complete, immutable instruction to one runner invocation.

Required sections:

```yaml
protocol: factory-runner-protocol/v1
schema: run-spec/v1
schema_version: 1
operation: author | verify
identity:
  work_item_id: opaque
  work_item_revision_id: opaque
  delivery_phase_id: opaque
  run_id: opaque
  attempt_id: opaque
  correlation_id: opaque
repository:
  repository_id: opaque
  display_name: owner/name
  base_commit_sha: 40-character-sha
workspace:
  path: /workspace/target
  initial_state: clean_base | prepared_verification
task:
  outcome: text
  acceptance_criteria: []
  non_goals: []
  constraints: []
policy:
  allowed_paths: []
  prohibited_paths: []
  allowed_stages: []
  allowed_commands: []
  network_profile: opaque-policy-name
  credential_profile: no-external-credentials
  max_wall_seconds: integer
  max_agent_turns: integer
  max_model_tokens: integer
context:
  manifest_path: /factory/input/context/context-bundle.json
  expected_digest: sha256:...
resume:
  checkpoint_path: null
  expected_digest: null
outputs:
  output_dir: /factory/output
  stream_events_to_stdout: false
```

Contract rules:

- `operation=author` permits only the configured planning, implementation, and
  authoring-verification stages;
- `operation=verify` prohibits agent authoring and repository mutation;
- `commit` and `pr` stages are invalid in factory mode;
- `allowed_paths` is mandatory, even if it explicitly represents the whole
  repository;
- verification commands must be provided as argument arrays, not shell
  strings;
- environment variables passed to commands use an explicit allowlist;
- model selection is a profile identifier, not a provider secret;
- an unsupported operation, stage, or capability fails before execution;
- unknown major protocol versions fail with `unsupported_protocol`;
- conflicting or missing constraints fail with `invalid_input`.

### 7.3 `ContextBundle`

`ContextBundle` is deterministic, repository-scoped input assembled outside
AI Native.

It contains:

- all common and repository identity fields;
- `context_bundle_id`;
- ordered manifest entries with relative logical path, media type, byte size,
  SHA-256 digest, and classification;
- the normalised work-item revision;
- applicable repository instructions;
- trusted policy summary;
- repository memory explicitly admitted by provenance, repository-scope, and
  policy checks;
- dependency outputs explicitly attached to this phase;
- exception-resolution input explicitly attached to this run, when present;
- construction metadata and bundle digest.

Allowed classifications:

- `work_item_revision`;
- `repository_instruction`;
- `trusted_policy`;
- `approved_project_memory`;
- `dependency_output`;
- `operator_input`;
- `supporting_artifact`.

`approved_project_memory` is the stable v1 wire classification name. In this
automated flow, “approved” means admitted by deterministic provenance,
repository-scope, and policy checks through the automated admission path.
`operator_input` is reserved for auditable exception resolution; routine phase
progression remains automated.

The bundle must not contain:

- unrelated issue or chat history;
- other repositories' memory;
- provider or GitHub secrets;
- raw hidden model reasoning;
- mutable URLs whose content is not captured by digest;
- unvalidated memory presented as policy-admitted project memory.

The runner verifies every object before using any content. Bundle validation
must be deterministic and must not fetch missing objects from the network.

### 7.4 `Checkpoint`

`Checkpoint` is a portable safe-boundary recovery package. It represents
acknowledgeable runner progress, not a live process snapshot.

Required content:

- common and repository identity fields;
- `checkpoint_id`;
- `sequence`;
- `producer_attempt_id`;
- protocol and runner compatibility requirements;
- context-bundle digest;
- input `RunSpec` digest;
- current workspace patch digest;
- completed stages;
- next permitted stage;
- structured AI Native workflow state;
- red, green, refactor, and verification evidence references created so far;
- artifact manifest;
- consumed and remaining runner budgets;
- sanitised decisions, assumptions, and open questions;
- checkpoint object digests;
- checkpoint digest.

Compatibility rules:

- a later attempt may resume only the same run, work-item revision, delivery
  phase, repository, base commit, and context-bundle digest;
- the new attempt ID differs from `producer_attempt_id`;
- unknown checkpoint major versions fail closed;
- a checkpoint may not grant a stage, path, command, credential, network
  destination, or budget absent from the new `RunSpec`;
- the new `RunSpec` may narrow authority;
- an incompatible checkpoint returns `checkpoint_incompatible` without
  changing the workspace;
- restore is transactional from the runner's perspective: either the complete
  checkpoint is applied or the workspace remains at its prepared state.

### 7.5 `VerificationEvidence`

`VerificationEvidence` is structured, reproducible evidence. It must
distinguish authoring evidence from evidence produced in a separately
provisioned clean verification sandbox.

Required content:

- common and repository identity fields;
- `environment_kind`: `authoring` or `clean_verification`;
- runner image and runner version, when available;
- context and change-set digests;
- ordered evidence items;
- overall deterministic status;
- advisory observations in a separate section;
- evidence-set digest.

Each evidence item contains:

- phase: `red`, `green`, `refactor`, or `verification`;
- command as an argument array;
- repository-relative working directory;
- sanitised environment-key allowlist;
- start and finish timestamps;
- duration;
- exit code and termination reason;
- expected status;
- actual status;
- failure classification;
- standard-output and standard-error artifact references and digests;
- machine-readable test-report references where available;
- tool and dependency versions;
- whether repository files changed during the command.

Red evidence is valid only when:

- the intended behavioural test was executed;
- the command failed for the expected missing behaviour;
- the failure was not caused by syntax, collection, dependency, credential,
  infrastructure, timeout, or unrelated test failure.

An authoring run may only emit `environment_kind=authoring`. A factory obtains
`clean_verification` evidence by starting a fresh sandbox and invoking
`ainative factory verify`; AI Native does not decide whether that sandbox is
independent.

### 7.6 `ChangeSet`

`ChangeSet` is the sanitised output of an authoring operation. It is not a Git
publication instruction.

Required content:

- common and repository identity fields;
- `change_set_id`;
- base commit SHA;
- runner and context digests;
- patch artifact path, media type, byte size, and digest;
- diff digest;
- ordered changed-file manifest;
- evidence-set digest and evidence references;
- acceptance-criterion results;
- concise outcome summary;
- assumptions;
- residual risks;
- policy observations;
- generated artifact references;
- change-set digest.

Each changed-file entry contains:

- normalised relative path;
- operation: `add`, `modify`, `delete`, or `rename`;
- previous path for a rename;
- previous and resulting blob digest where applicable;
- previous and resulting file mode;
- binary indicator;
- allowed-path decision.

Change-set rules:

- v1 uses a deterministic Git binary patch or another precisely documented,
  lossless patch format selected in `AN-01`;
- the patch may contain no `.git` internals;
- all paths must satisfy the input path policy;
- unexpected submodules, symlinks, special files, or file modes fail closed;
- the base commit must match the `RunSpec`;
- secret scanning runs before finalisation;
- `ChangeSet` contains no remote URL containing credentials;
- branch name, commit identity, PR title, PR body, labels, and publication
  token are outside the contract;
- a no-change outcome produces no empty patch and no misleading `ChangeSet`.

### 7.7 `RunResult`

`RunResult` is the terminal receipt for one runner invocation.

Allowed outcomes:

- `succeeded`;
- `no_change`;
- `blocked`;
- `failed`;
- `cancelled`;
- `timed_out`;
- `invalid_input`;
- `checkpoint_incompatible`;
- `policy_denied`.

Required content:

- common and repository identity fields when input validation reached that
  point;
- operation;
- outcome and stable reason code;
- concise sanitised message;
- started and finished timestamps;
- completed stages;
- latest checkpoint reference;
- `ChangeSet` reference for a successful authoring operation;
- `VerificationEvidence` reference for a successful verification operation;
- event-stream digest;
- output-manifest digest;
- runner build identity;
- result digest.

A stack trace may be written as a separately sanitised diagnostic artifact but
must not be embedded in the stable result contract.

### 7.8 `RunnerEvent`

Runner events report attempt-local facts. They are inputs to, not replacements
for, the private factory's authoritative domain event stream.

Required envelope:

```json
{
  "protocol": "factory-runner-protocol/v1",
  "schema": "runner-event/v1",
  "schema_version": 1,
  "run_id": "opaque",
  "attempt_id": "opaque",
  "sequence": 17,
  "timestamp": "RFC-3339",
  "event_type": "StageCompleted",
  "correlation_id": "opaque",
  "causation_id": "opaque-or-null",
  "sanitised_payload": {},
  "artifact_refs": []
}
```

Initial event types:

- `RunnerStarted`;
- `InputValidated`;
- `CheckpointRestored`;
- `StageStarted`;
- `StageCompleted`;
- `ToolStarted`;
- `ToolCompleted`;
- `TestStarted`;
- `TestCompleted`;
- `FileManifestChanged`;
- `CheckpointWritten`;
- `ChangeSetWritten`;
- `VerificationEvidenceWritten`;
- `PolicyDenied`;
- `RunnerCancellationRequested`;
- `RunnerCompleted`;
- `RunnerFailed`.

The runner must not emit control-plane states such as `RunQueued`,
`SandboxLeaseGranted`, `PullRequestOpened`, or `PullRequestMerged`.

Events must not contain hidden model reasoning, full source files, raw secrets,
or unbounded tool output. Large output is represented by a redacted artifact
reference and digest.

### 7.9 Schema-set digest

The release records one canonical schema-set digest:

1. Generate all v1 JSON Schemas from the Pydantic models.
2. Canonicalise each schema using the documented canonical JSON procedure.
3. Create an ordered manifest of schema path and individual digest.
4. Canonicalise the manifest.
5. Calculate its SHA-256 digest.
6. Fail CI if regeneration changes checked-in files unexpectedly.

The factory compares this digest with the value in the release receipt before
accepting a runner release.

---

## 8. Runner execution behaviour

### 8.1 Author operation

The author operation:

1. validates the `RunSpec`, context bundle, workspace, policy, and optional
   checkpoint;
2. records `RunnerStarted` and `InputValidated`;
3. restores a compatible checkpoint if supplied;
4. invokes allowed existing AI Native stages through an adapter;
5. prevents interactive clarification and returns `blocked` when required
   information is absent;
6. captures red → green → refactor evidence;
7. runs configured authoring verification;
8. checks the resulting repository change against path and file policy;
9. scans outputs for secrets;
10. produces a deterministic `ChangeSet`;
11. writes `RunResult`;
12. writes `completion.json` last.

It stops before commit and publication. A local temporary commit used only as
an implementation detail is prohibited unless a later ADR proves it necessary
and demonstrates that it cannot reach a remote. The preferred v1 output is a
patch from the prepared base.

### 8.2 Verify operation

The verify operation:

1. validates a workspace already prepared by the factory at the expected
   change-set state;
2. disables agent authoring;
3. runs only the exact deterministic verification commands in the `RunSpec`;
4. checks that verification does not unexpectedly mutate tracked files;
5. emits `environment_kind=clean_verification` evidence because the caller
   explicitly selected the verify operation;
6. produces `VerificationEvidence` and `RunResult`;
7. never creates a `ChangeSet`.

The factory remains responsible for starting verify in a genuinely fresh
sandbox. The trusted publisher applies the blocking verification and
publication policy to decide whether the evidence is sufficient; an operator
is involved only when that policy reports a true exception.

### 8.3 Stable exit codes

| Code | Meaning |
|---:|---|
| `0` | `succeeded` or valid `no_change` |
| `2` | invalid input or unsupported protocol |
| `3` | policy denied |
| `4` | blocked pending better requirements, policy-admitted input, or exception resolution |
| `5` | checkpoint incompatible |
| `6` | deterministic verification failed |
| `7` | runner or agent execution failed |
| `8` | cancelled at a safe boundary |
| `9` | runner-enforced timeout |

The JSON `RunResult` is authoritative. Exit codes provide process-level
classification only.

### 8.4 Determinism boundaries

Model-generated code is not deterministic. The following must be
deterministic:

- input validation;
- protocol negotiation;
- schema generation;
- context and checkpoint digest verification;
- stage permission checks;
- path policy;
- evidence classification rules;
- changed-file manifest generation;
- patch generation for identical worktree state;
- secret redaction;
- result and completion writing;
- compatibility fixtures using fake agent adapters.

---

## 9. Factory-mode safety

### 9.1 Credential rules

On factory-mode startup:

- do not read `~/.ssh`, `~/.gitconfig`, `~/.config/gh`, `~/.codex`,
  `~/.copilot`, cloud credential folders, or netrc files;
- ignore interactive-mode host-auth discovery;
- fail when prohibited broad credentials are present in the environment,
  including GitHub, SSH-agent, and cloud-provider credential variables;
- permit only the explicitly documented attempt-scoped model-gateway
  capability;
- accept a gateway token through an ephemeral file or descriptor, not a CLI
  argument;
- redact the capability value before any logging or artifact creation;
- do not persist the capability in a checkpoint;
- ensure child commands receive only their declared environment-key allowlist;
- disable Git credential helpers and interactive Git authentication;
- set Git to non-interactive mode;
- prevent any configured AI Native publication or remote-registry credential
  from activating.

The sandbox platform must still enforce filesystem and network isolation.
Runner checks are defence in depth, not a claim of hostile-code containment.

### 9.2 Publication rules

Factory mode:

- has no GitHub client;
- never calls `gh`;
- never executes `git push`;
- rejects `commit` and `pr` stages;
- removes or disables Git remotes before agent execution unless a documented
  read-only local remote is required;
- prohibits repository hooks;
- rejects publication configuration in `RunSpec`;
- does not generate branch or PR idempotency markers.

The private factory's trusted publisher applies a validated `ChangeSet` in a
sterile checkout and owns all GitHub side effects.

The trusted publisher runs outside the attempt sandbox. An attempt sandbox
has no GitHub merge, administration, review-approval, or deployment
credentials. It may never bypass branch protection, force-push, approve its
own change, deploy, or merge directly.

### 9.3 Redaction and output safety

- redact before writing, not only when displaying;
- use exact secret canaries in tests;
- cap inline event and result fields;
- store bounded logs as artifacts;
- scan events, checkpoints, evidence, patches, result documents, and
  completion manifests;
- refuse to finalise contaminated output;
- avoid echoing untrusted issue or repository text in error messages;
- record secret identifiers or redaction counts, never values;
- do not retain model hidden reasoning.

### 9.4 Filesystem safety

- resolve input and output roots once and reject traversal;
- reject output roots inside the target repository;
- reject symlink escapes and special files;
- never write outside the target workspace and output root;
- validate file mode transitions;
- bound patch, event, log, checkpoint, and total output size;
- leave incomplete temporary files unreferenced after interruption.

---

## 10. Proposed repository layout

Exact existing integration points must be confirmed during `AN-00`. The
following is the preferred target without moving stable modules solely to
match this diagram:

```text
ai_native/
  cli.py                              # existing entrypoint; add factory group
  factory_runner/
    __init__.py
    cli.py
    protocol.py
    runner.py
    workflow_adapter.py
    verify.py
    policy.py
    redaction.py
    filesystem.py
    outputs.py
    events.py
    checkpoints.py
    evidence.py
    changesets.py
    result.py
    contracts/
      common.py
      run_spec.py
      context_bundle.py
      checkpoint.py
      verification_evidence.py
      change_set.py
      run_result.py
      runner_event.py
  schemas/
    factory_runner/
      v1/
        run-spec.schema.json
        context-bundle.schema.json
        checkpoint.schema.json
        verification-evidence.schema.json
        change-set.schema.json
        run-result.schema.json
        runner-event.schema.json
        release-receipt.schema.json
        schema-manifest.json

tests/
  factory_runner/
    contract/
    compatibility/
    security/
    integration/
  fixtures/
    factory_runner/
      target_repo/
      context/
      checkpoints/
      golden/

images/
  factory-runner/
    Dockerfile

docs/
  factory/
    AI_NATIVE_FACTORY_RUNNER_IMPLEMENTATION_PLAN.md
  factory-runner/
    protocol-v1.md
    security-boundary.md
    compatibility.md
    releasing.md

.github/workflows/
  factory-runner-ci.yml
  factory-runner-release.yml

scripts/
  generate_factory_runner_schemas.py
  run_factory_runner_compatibility.py
  build_factory_runner_release_receipt.py
```

Likely existing files affected:

- `ai_native/cli.py`;
- existing workflow, state, stage, agent-adapter, verification, telemetry, and
  configuration modules identified in `AN-00`;
- `pyproject.toml`;
- `uv.lock`;
- `Makefile`;
- `README.md`;
- `ainative.yaml` defaults only where an explicit factory profile is useful;
- package-data configuration for checked-in schemas;
- existing CI workflows.

Do not move the existing run registry, rewrite the workflow engine, or place
factory control-plane services in this repository as part of these phases.

---

## 11. Test strategy

### 11.1 Required test layers

| Layer | Purpose |
|---|---|
| Characterisation | Preserve existing interactive CLI and template behaviour |
| Model unit tests | Validate contract invariants and policy decisions |
| JSON Schema tests | Validate Python/JSON interoperability and schema drift |
| Golden compatibility | Prove stable input and output documents |
| Workflow-adapter tests | Exercise the existing workflow through fake agents |
| Runner integration | Execute author and verify commands without a TTY |
| Security regression | Credential, traversal, symlink, secret, remote, and publication denial |
| Interruption/recovery | Kill at safe boundaries and restore checkpoints |
| Package smoke | Install the built wheel into a clean environment |
| OCI smoke | Run the image as non-root with read-only root filesystem |
| Release conformance | Verify immutable artifacts and receipt contents |

### 11.2 Mandatory fixtures

Maintain:

- a minimal target repository with a deterministic behavioural change;
- a documentation-only task with a policy-authorized substitute check;
- a no-change task;
- a task requiring clarification that must return `blocked`;
- a changed file outside `allowed_paths`;
- a repository containing prompt-injection instructions;
- secret canaries in environment, file, command output, and model output;
- a checkpoint created by the current protocol;
- deliberately incompatible checkpoints;
- a prepared verification checkout;
- golden v1 input and output documents.

### 11.3 Compatibility promise

For protocol v1:

- patch releases may fix implementation without changing valid v1 documents;
- additive optional capability requires explicit negotiation;
- a breaking schema or semantic change requires a new protocol major version;
- checked-in golden v1 fixtures remain readable throughout v1 support;
- the compatibility suite tests the wheel and OCI image, not only source-tree
  imports;
- deprecation requires documentation and at least one released transition
  path.

---

## 12. Delivery rules

Every phase:

- is implemented on its own branch;
- produces exactly one focused PR;
- uses strict red → green → refactor;
- includes the failing test or deterministic check in the PR history/evidence;
- updates relevant documentation in the same PR;
- keeps existing CLI tests green;
- keeps every required CI, security, policy, packaging, and compatibility
  check blocking;
- hands its draft PR to the trusted publisher/control plane, which marks it
  ready and enables GitHub auto-merge only after the phase evidence is
  complete;
- must not begin its dependent successor until GitHub reports the PR actually
  merged to the default branch;
- publishes no mutable or unreleased dependency for the factory to consume.

Suggested branch names:

```text
factory-runner/an-00-boundary
factory-runner/an-01-contracts
factory-runner/an-02-runner
factory-runner/an-03-outputs
factory-runner/an-04-release
```

If a phase becomes too large for one focused PR and its automated evidence,
split it into suffixed subphases such as `AN-03A` and `AN-03B`, update both
repository plans, and preserve dependency order. Do not silently combine
multiple phases in one PR.

### 12.1 Protected automated merge policy

The normal phase lifecycle is:

1. The phase implementation worker creates the phase branch, commits only
   phase-scoped work, pushes that feature branch, and opens one draft PR with
   machine-verifiable red, green, refactor, and final evidence.
2. GitHub runs all required CI, security, policy, packaging, and compatibility
   checks. Those checks remain required branch-protection checks.
3. A trusted publisher/control plane, running outside the attempt sandbox,
   verifies the phase manifest, evidence, required check conclusions, and
   branch-protection state. It marks the PR ready and enables GitHub
   auto-merge.
4. GitHub performs the protected merge only when all required checks and
   repository rules are satisfied.
5. The orchestrator verifies that the resulting commit is present on the
   default branch, refreshes the local default branch, and only then starts the
   dependent phase.

The factory-mode attempt sandbox defined in Section 9 has no GitHub client or
credentials at all. When the phase implementation workspace uses trusted host
GitHub tooling, that tooling receives only the minimum feature-branch and
draft-PR authority needed for its task. Neither environment receives GitHub
merge, administration, review-approval, branch-protection-bypass, force-push,
or deployment credentials. Neither may mark its own work approved, weaken or
skip a required check, enable an unprotected direct merge, force-push, deploy,
or merge the default branch.

Operator intervention is not a routine readiness or merge step. It is allowed
only for a true exception: scope ambiguity, a security finding, a missing
permission, a merge conflict the protected automation cannot resolve, or a
failed required check that needs diagnosis. The operator resolves the cause;
the trusted publisher then restarts the same protected check and auto-merge
path without bypassing policy.

---

## 13. Phased implementation

### AN-00 — Baseline characterisation and boundary lock

**Objective:** Protect existing AI Native behaviour and establish the public
runner boundary before adding protocol details.

**Entry criteria:**

- the repository is checked out on a clean branch from the current default
  branch;
- repository `AGENTS.md`, configuration, current CLI, Make targets, tests,
  workflow documentation, packaging, and CI have been inspected;
- the current full deterministic test command is known;
- this plan is the authoritative scope source for the phase.

**Red:**

1. Add a failing boundary-policy test proving factory mode permits authoring
   and verification capabilities but excludes commit, PR, push, merge, and
   interactive input.
2. Add a failing CLI contract test reserving `ainative factory` without
   changing existing command semantics.
3. Capture characterisation tests for existing commands. Existing behaviour
   recorded by those tests is baseline evidence and need not be made to fail.

**Green deliverables:**

- an ADR confirming the two-repository split and authority boundary;
- inventory of existing modules reused by the runner;
- `FactoryModeCapabilities` or equivalent immutable boundary policy;
- reserved `ainative factory --help` command group with no executable run
  implementation;
- explicit legacy/factory mode separation;
- characterisation tests for interactive CLI, configuration discovery,
  workflow stage selection, run paths, and publication behaviour;
- one documented deterministic test command;
- initial threat analysis limited to the runner;
- phase manifest containing `AN-00` through `AN-04`.

**Refactor:**

- centralise stage capability definitions instead of duplicating stage-name
  lists;
- isolate CLI dispatch without changing current public options;
- document any unavoidable legacy coupling for later phases.

**Likely files/modules:**

- `ai_native/cli.py`;
- existing stage/workflow/configuration modules discovered during inspection;
- new `ai_native/factory_runner/policy.py`;
- new `docs/factory-runner/security-boundary.md`;
- new factory-runner test directories;
- `Makefile` and CI test configuration.

**Required tests:**

- all pre-existing tests;
- legacy CLI help and representative command parsing;
- factory capability allow/deny matrix;
- factory command group never prompts;
- no existing command defaults change;
- package import and installed CLI smoke test.

**Exit criteria:**

- current interactive and template flows remain green;
- factory authority is executable policy, not documentation alone;
- publication and interactive capabilities are excluded from factory mode;
- existing modules to wrap, modify, or leave untouched are documented;
- `AN-01` can add contracts without redesigning the repository boundary.

**Handoff to next phase:** GitHub reports the protected `AN-00` PR actually
merged to the default branch after all blocking checks and publisher-controlled
auto-merge, and the boundary ADR is present on that branch.

---

### AN-01 — Factory runner protocol v1 contracts

**Objective:** Define and freeze the language-neutral contract surface before
building runner execution.

**Entry criteria:**

- the `AN-00` merge commit is present on the current default branch;
- the boundary ADR and capability policy are green;
- the contract decisions in Sections 5–7 are encoded in versioned schemas,
  policy checks, and blocking compatibility tests.

**Red:**

1. Add failing model tests for valid and invalid `RunSpec`,
   `ContextBundle`, `Checkpoint`, `VerificationEvidence`, `ChangeSet`,
   `RunResult`, and `RunnerEvent` documents.
2. Add failing schema round-trip and drift tests.
3. Add failing tests for protocol negotiation, path normalisation, identity
   preservation, digest verification, and incompatible checkpoints.

**Green deliverables:**

- Pydantic v1 protocol models;
- checked-in generated JSON Schemas;
- canonical JSON and digest utilities;
- schema-set manifest and deterministic digest;
- human-readable protocol v1 documentation;
- minimal and complete examples for every contract;
- golden valid and invalid fixture corpus;
- protocol capability negotiation;
- stable validation error codes;
- package-data configuration making schemas available from the installed
  wheel;
- a public Python import surface for contract validation without importing
  workflow internals.

**Refactor:**

- remove duplicated common identity, digest, path, timestamp, and artifact
  types;
- keep factory-specific business entities out of the models;
- ensure schema generation is a one-command deterministic operation.

**Likely files/modules:**

- `ai_native/factory_runner/contracts/`;
- `ai_native/factory_runner/protocol.py`;
- `ai_native/schemas/factory_runner/v1/`;
- `scripts/generate_factory_runner_schemas.py`;
- `tests/factory_runner/contract/`;
- `tests/fixtures/factory_runner/golden/`;
- `pyproject.toml`;
- protocol documentation.

**Required tests:**

- model validation;
- JSON Schema validation independent of Python models;
- schema-generation drift;
- canonical digest repeatability;
- unknown fields and unsupported major versions;
- opaque identity preservation;
- path traversal and malformed digest rejection;
- checkpoint authority narrowing and escalation rejection;
- golden fixture compatibility;
- wheel package-data smoke test.

**Exit criteria:**

- all seven contracts and runner events have stable v1 schemas;
- golden examples validate through both Pydantic and JSON Schema;
- schema generation is reproducible;
- the schema-set digest is reproducible;
- no contract imports Temporal, GitHub, database, Docker, or control-plane
  types;
- `AN-02` can implement the runner without casually changing schemas.

**Handoff to next phase:** GitHub reports the protected `AN-01` PR actually
merged to the default branch after all blocking checks and publisher-controlled
auto-merge, with checked-in schemas, golden fixtures, and schema-set digest
tooling present on that branch.

---

### AN-02 — Non-interactive factory runner

**Objective:** Execute author and verify operations through the v1 contracts
without prompts, publication, or host credential discovery.

**Entry criteria:**

- the `AN-01` merge commit is present on the current default branch;
- protocol v1 schemas and examples are green;
- a deterministic fake-agent fixture is available;
- the existing workflow integration points are documented.

**Red:**

1. Add a failing end-to-end test for `ainative factory run` using a valid
   fixture `RunSpec`.
2. Add a failing test proving incomplete requirements return `blocked`
   without prompting.
3. Add failing negative tests for `commit`, `pr`, GitHub access, broad
   credentials, dirty or wrong-base workspaces, and undeclared commands.
4. Add a failing `ainative factory verify` test proving agent authoring is
   disabled.

**Green deliverables:**

- `ainative factory run`;
- `ainative factory verify`;
- workflow adapter around existing AI Native stages;
- non-interactive clarification handling;
- author and verify operation separation;
- explicit workspace and base-SHA validation;
- factory-mode configuration overlay;
- deadline and cancellation handling at safe stage boundaries;
- environment-key filtering for child commands;
- Git remote, hook, credential-helper, and prompt suppression;
- fake-agent adapter and deterministic integration fixture;
- initial terminal `RunResult` and minimal completion output;
- human-readable standard-error logging with machine-readable standard-output
  discipline.

**Refactor:**

- reuse existing workflow logic through ports rather than copying it;
- isolate interactive-only functionality behind existing-mode adapters;
- centralise process execution and environment filtering;
- retain existing local runner behaviour.

**Likely files/modules:**

- `ai_native/cli.py`;
- `ai_native/factory_runner/cli.py`;
- `ai_native/factory_runner/runner.py`;
- `ai_native/factory_runner/workflow_adapter.py`;
- `ai_native/factory_runner/verify.py`;
- `ai_native/factory_runner/policy.py`;
- existing workflow, agent, command, state, and verification modules;
- `tests/factory_runner/integration/`;
- fixture target repository.

**Required tests:**

- author happy path with fake agent;
- verify happy and failing paths;
- no TTY and closed standard input;
- missing input returns `blocked`;
- base-SHA and cleanliness mismatch;
- forbidden stages and commands;
- prohibited credential variables;
- Git remote/push/publication denial;
- SIGTERM at each safe boundary;
- deadline expiry;
- existing interactive CLI regression suite.

**Exit criteria:**

- both commands run unattended from validated documents;
- no code path invokes interactive input;
- factory mode cannot reach existing commit or PR stages;
- missing requirements block clearly rather than being guessed;
- verify mode cannot author changes;
- existing workflow code is adapted rather than duplicated;
- all outputs conform to the `AN-01` result schemas.

**Handoff to next phase:** GitHub reports the protected `AN-02` PR actually
merged to the default branch after all blocking checks and publisher-controlled
auto-merge, and the merged source-tree runner passes deterministic fixture
tests.

---

### AN-03 — Event, checkpoint, evidence and change-set outputs

**Objective:** Make an authoring sandbox disposable by producing complete,
sanitised, content-addressed outputs at safe boundaries.

**Entry criteria:**

- the `AN-02` merge commit is present on the current default branch;
- author and verify commands execute end to end;
- all output contracts are stable v1;
- the output filesystem rules are encoded in blocking policy and security
  tests.

**Red:**

1. Add failing tests for ordered atomic runner events and completion-marker
   behaviour.
2. Add failing kill-and-resume tests at every defined safe boundary.
3. Add failing TDD evidence classification tests, including false-red cases.
4. Add failing deterministic change-set and path-policy tests.
5. Add failing secret-canary, traversal, symlink, and output-size tests.
6. Add a failing test proving author evidence cannot masquerade as clean
   verification.

**Green deliverables:**

- append-only local NDJSON event sink and optional stdout stream;
- event sequence and digest finalisation;
- atomic checkpoint writer and transactional restore;
- checkpoint creation after configured AI Native stage boundaries, red, green,
  refactor, authoring verification, and cancellation;
- structured verification evidence;
- explicit red-failure classification;
- deterministic changed-file manifest;
- deterministic patch and `ChangeSet`;
- no-change handling;
- producer-side redaction and secret scanning;
- output path, symlink, special-file, file-mode, and size policy;
- terminal `RunResult`, protocol manifest, completion manifest, and digest
  chain;
- verify-operation evidence with no `ChangeSet`;
- golden output fixtures updated from real command execution.

**Refactor:**

- share artifact and digest primitives across events, checkpoints, evidence,
  and change sets;
- keep event production separate from human logging;
- make sinks replaceable through small local interfaces without adding remote
  transport;
- minimise workflow-stage knowledge inside the serializers.

**Likely files/modules:**

- `ai_native/factory_runner/events.py`;
- `ai_native/factory_runner/checkpoints.py`;
- `ai_native/factory_runner/evidence.py`;
- `ai_native/factory_runner/changesets.py`;
- `ai_native/factory_runner/outputs.py`;
- `ai_native/factory_runner/redaction.py`;
- `ai_native/factory_runner/filesystem.py`;
- `ai_native/factory_runner/result.py`;
- existing TDD and verification modules;
- compatibility, recovery, and security tests.

**Required tests:**

- event order, duplicate prevention, and digest;
- stdout/file event equivalence;
- interrupted output lacks a valid completion marker;
- restore after every safe checkpoint;
- corrupt, cross-run, cross-repository, and authority-expanding checkpoints;
- genuine and false red evidence;
- deterministic patch and changed-file manifest;
- add, modify, delete, rename, binary, and mode-change policy;
- no-change result;
- author versus clean-verification provenance;
- secret canaries in all ingress and egress locations;
- traversal, symlink escape, special file, and output limits;
- full existing CLI regression suite.

**Exit criteria:**

- a killed runner can resume in a new attempt from the latest compatible
  checkpoint;
- all completed output is content-addressed and schema-valid;
- red → green → refactor evidence is machine-verifiable;
- identical worktree state produces the same diff and manifest digest;
- no secret canary appears in durable output;
- author and verify results are distinguishable;
- an authoring run ends with either one valid `ChangeSet` or an explicit
  non-change terminal outcome.

**Handoff to next phase:** GitHub reports the protected `AN-03` PR actually
merged to the default branch after all blocking checks and publisher-controlled
auto-merge, with the complete golden output corpus and passing
recovery/security tests present on that branch.

---

### AN-04 — Release packaging and compatibility certification

**Objective:** Publish an immutable, verifiable runner release that the private
factory can consume without source-tree coupling.

**Entry criteria:**

- the `AN-03` merge commit is present on the current default branch;
- the protocol, runner, outputs, security tests, and existing CLI suite are
  green;
- the policy-authorized wheel registry and OCI registry are configured;
- the release signing and provenance mechanism is configured and enforced by
  blocking release policy;
- no semantic version is preselected by this plan.

**Red:**

1. Add a failing clean-wheel installation and golden-run test.
2. Add a failing OCI test requiring non-root execution, read-only root
   filesystem, explicit writable mounts, and no embedded credentials.
3. Add a failing cross-artifact compatibility test proving the wheel and OCI
   image emit equivalent v1 outputs for deterministic fixtures.
4. Add a failing release-receipt validation test.
5. Add failing tests for mutable image references, mismatched schema digests,
   incorrect source SHA, failed compatibility results, and missing supply-chain
   evidence.

**Green deliverables:**

- production wheel build for the existing Python distribution;
- minimal factory-runner OCI image;
- OCI entrypoint invoking the existing `ainative` CLI;
- image runs as a fixed non-root user;
- image digest, SBOM, vulnerability scan result, signature, and provenance;
- CI compatibility suite against source, installed wheel, and built image;
- release workflow tied to an immutable Git commit/tag;
- schema manifest and schema-set digest embedded in both artifacts;
- release notes documenting supported protocol and known limitations;
- formal `factory-runner-protocol/v1` release receipt;
- receipt signature or verifiable provenance;
- published compatibility-suite report.

**Refactor:**

- use one build-identity source for wheel, image, output manifests, and
  receipt;
- ensure release workflow regenerates nothing differently from CI;
- keep development image concerns separate from the minimal runner image;
- remove any release-time dependence on a local checkout outside the tagged
  source.

**Likely files/modules:**

- `pyproject.toml`;
- `uv.lock`;
- package build identity module;
- `images/factory-runner/Dockerfile`;
- `.dockerignore`;
- `.github/workflows/factory-runner-ci.yml`;
- `.github/workflows/factory-runner-release.yml`;
- release and compatibility scripts;
- `docs/factory-runner/releasing.md`;
- release-receipt schema and tests;
- `Makefile`.

**Required tests and gates:**

- complete deterministic source test suite;
- build wheel and install into an empty Python environment;
- run golden author and verify fixtures from the installed wheel;
- build OCI image without secret-bearing layers;
- inspect image user, entrypoint, labels, packages, and environment;
- run OCI image with read-only root, non-root user, no network, and only
  declared mounts;
- run security and canary suite inside the image;
- compare source, wheel, and OCI protocol outputs;
- validate SBOM, scan policy, signature, and provenance;
- verify receipt against the exact published artifacts;
- rerun compatibility suite using only receipt-resolved artifacts.

**Exit criteria:**

- the wheel is published with its actual distribution name and version;
- the OCI image is published and resolvable by immutable digest;
- wheel and image trace to the same Git commit;
- both expose exactly the receipt's schema-set digest;
- the compatibility suite passes against published artifacts;
- the formal release receipt validates and is available to the private
  factory;
- no mutable tag is required to reproduce the tested runner;
- existing interactive CLI usage remains supported.

**Handoff to factory `FF-00`:** GitHub first reports the protected `AN-04` PR
actually merged to the default branch after all blocking checks and
publisher-controlled auto-merge. The post-merge workflow then produces the
formal release receipt described below. `FF-00` must not begin until this
receipt has been generated from published artifacts and independently
validated.

---

## 14. Formal release receipt

The post-merge `AN-04` workflow produces:

```text
factory-runner-release-receipt.json
```

Its receipt schema is `factory-runner-release-receipt/v1`. The plan does not
invent or reserve a semantic version; the workflow records the actual released
wheel version.

Minimum content:

```json
{
  "receipt_schema": "factory-runner-release-receipt/v1",
  "protocol": "factory-runner-protocol/v1",
  "released_at": "RFC-3339",
  "source": {
    "repository": "ufJmacca/ai-native",
    "git_commit_sha": "<actual-40-character-sha>",
    "git_tag": "<actual-release-tag>"
  },
  "wheel": {
    "distribution": "<actual-distribution-from-AN-04>",
    "version": "<actual-released-version>",
    "filename": "<actual-wheel-filename>",
    "sha256": "sha256:<actual-digest>",
    "download_url": "<immutable-or-versioned-location>"
  },
  "oci_image": {
    "repository": "<actual-image-repository>",
    "digest": "sha256:<actual-manifest-digest>",
    "pinned_reference": "<repository>@sha256:<actual-manifest-digest>",
    "platforms": ["<published-platform>"]
  },
  "contracts": {
    "schema_set_digest": "sha256:<actual-digest>",
    "schema_manifest_sha256": "sha256:<actual-digest>"
  },
  "compatibility": {
    "suite_version": "<actual-suite-version>",
    "status": "passed",
    "report_url": "<published-report>",
    "report_sha256": "sha256:<actual-digest>"
  },
  "supply_chain": {
    "sbom_url": "<published-sbom>",
    "sbom_sha256": "sha256:<actual-digest>",
    "provenance_url": "<published-provenance>",
    "signature_reference": "<verifiable-signature>"
  }
}
```

Release-receipt rules:

- all placeholder values above are replaced by actual release outputs;
- the receipt is always generated by the release workflow from actual outputs;
- its commit SHA equals the source used for both artifacts;
- `pinned_reference` includes the OCI digest;
- the wheel digest is verified after download;
- the schema-set digest equals the digest exposed by wheel and image;
- compatibility status must be `passed`;
- referenced reports and attestations are immutable or content-addressed;
- the release job fails instead of publishing an incomplete receipt;
- receipt verification is available as a standalone command usable by the
  factory repository;
- a corrected artifact requires a new release and receipt, never in-place
  replacement.

---

## 15. Cross-repository change protocol

Most work should affect one repository at a time.

When a future factory requirement needs an AI Native contract or runner
change:

1. Open an AI Native issue describing a factory-neutral requirement.
2. Classify whether the change is backward-compatible using the protocol
   compatibility policy and fixture suite.
3. Implement it in `ai-native` using a dedicated branch and PR.
4. Add or update compatibility fixtures.
5. Keep all required checks blocking; have the trusted publisher mark the PR
   ready and enable GitHub auto-merge, then verify the actual default-branch
   merge before publishing a new immutable release.
6. Produce a new release receipt.
7. In a separate factory PR, update the stored receipt and pinned OCI
   reference.
8. Run the factory consumer compatibility suite.
9. Keep the factory upgrade's migration and rollback compatibility checks
   blocking, and allow its trusted publisher to enable auto-merge only after
   both pass.

Prohibited integration shortcuts:

- editable installs from a sibling checkout in CI;
- `path = "../ai-native"` production dependencies;
- Git submodules;
- copying schemas between repositories;
- depending on `main`;
- mutable `latest` image tags;
- building an unofficial factory-only fork of AI Native;
- one commit spanning both repositories;
- one PR purporting to cover both repositories.

For coordinated local diagnosis, the two sibling repositories may be visible
to one Codex session, but every command must use an explicit working directory
and every change must still become a repository-specific branch, test run,
commit, and PR.

---

## 16. Codex implementation instructions

Run the primary Codex session from the AI Native repository:

```bash
codex -C /path/to/software-factory-workspace/ai-native
```

Use the sibling factory repository only when a phase explicitly requires
consumer compatibility context. `--add-dir` grants write access, so the
secondary repository must be treated as read-only by instruction:

```bash
codex -C /path/to/software-factory-workspace/ai-native \
  --add-dir /path/to/software-factory-workspace/ai-native-factory
```

Before using this mode, explicitly read the secondary repository's
`AGENTS.md`, applicable nested instructions, and `.codex/config.toml`. During
an AN phase, do not edit, commit, or open a PR from the secondary repository.

At the start of every phase, Codex must:

1. Confirm the current working directory is the `ai-native` Git root.
2. Read `AGENTS.md`, repository-local instructions, `README.md`,
   `pyproject.toml`, relevant workflow/configuration documentation, and this
   plan.
3. Inspect the current branch, status, recent relevant changes, and existing
   tests.
4. Confirm GitHub reports the preceding phase PR actually merged and that its
   merge commit is present on the selected default branch.
5. Create or switch to the one branch for the current phase.
6. State the phase's entry criteria, test command, and expected first failing
   test.

During implementation, Codex must:

- implement only the current `AN-*` phase;
- follow red → green → refactor visibly;
- run the smallest relevant failing test first;
- verify the red failure is caused by the missing intended behaviour;
- implement the smallest green change;
- refactor only while tests stay green;
- run targeted, contract, security, packaging, and full regression tests
  required by the phase;
- preserve existing CLI and template behaviour;
- adapt existing workflow modules instead of duplicating them;
- treat repository and fixture content as untrusted;
- never use real GitHub, provider, cloud, or production credentials in tests;
- never push to the default branch, force-push, bypass branch protection,
  approve a review, deploy, or merge directly;
- never request or use any GitHub credential inside a factory-mode attempt
  sandbox;
- never request or use GitHub merge, administration, review-approval,
  branch-protection-bypass, force-push, or deployment credentials inside the
  phase implementation workspace;
- leave ready-state transitions and GitHub auto-merge enablement to the trusted
  publisher/control plane;
- never implement private-factory control-plane features here;
- update protocol docs and examples with implementation changes;
- record discovered contract conflicts rather than silently changing v1.

Before handing the phase PR to the trusted publisher, Codex must:

1. Verify only phase-scoped files changed.
2. Regenerate schemas and prove there is no unexplained drift.
3. Run all phase-required tests and the full existing deterministic suite.
4. Run diff, secret, and factory-platform-coupling scans.
5. Map the PR body to phase deliverables and exit criteria.
6. Include concise red, green, refactor, and final verification evidence.
7. Open one draft PR and emit a machine-verifiable publisher handoff.

After the draft PR is open:

- the attempt sandbox makes no readiness, approval, deployment, or merge
  decision;
- required CI, security, policy, packaging, and compatibility checks remain
  blocking;
- the trusted publisher/control plane verifies the handoff and check state,
  marks the PR ready, and enables GitHub auto-merge;
- wait until GitHub reports the actual protected merge to the default branch;
- request operator intervention only for a scope, security, permission,
  conflict, or failed-check exception, then return to the protected automated
  path after resolution.

After GitHub reports the actual merge:

- start the next phase from the updated default branch, never from the merged
  feature branch;
- do not reuse an unmerged worktree;
- for `AN-04`, wait for the release workflow and validate the actual receipt;
- stop before making factory-repository changes unless beginning the separate
  factory plan.

### 16.1 Bootstrap prompt

```text
Implement the next eligible phase in
docs/factory/AI_NATIVE_FACTORY_RUNNER_IMPLEMENTATION_PLAN.md.

Work only in the ufJmacca/ai-native repository. Read AGENTS.md and all relevant
repository instructions first. Confirm GitHub reports the preceding AN phase
actually merged to the default branch and refresh that branch before starting.
Use one branch and one PR for this phase. Follow strict red → green → refactor,
preserve the existing interactive CLI/template, and run every blocking CI,
security, policy, packaging, compatibility, and release gate required by the
phase.

Do not implement the GitHub issue queue, Temporal control plane, sandbox
allocation, publisher, event service, memory service, UI, or deployment. Do not
use real credentials. A factory-mode attempt sandbox must not receive any
GitHub client or credential. The phase implementation workspace must not
receive GitHub merge, administration, review-approval,
branch-protection-bypass, force-push, or deployment credentials. Do not push
to the default branch, force-push, bypass branch protection, approve, deploy,
or merge directly.

After opening the draft phase PR, emit the machine-verifiable handoff and leave
readiness and merge control to the trusted publisher/control plane. It marks
the PR ready and enables GitHub auto-merge only while all required checks and
branch protections remain blocking. Wait for GitHub to report the actual merge
to the default branch, then begin the next eligible AN phase from the updated
default branch. Request operator intervention only for a scope, security,
permission, conflict, or failed-check exception; never use an exception to
bypass a check. After AN-04, validate and report the formal
factory-runner-protocol/v1 release receipt, then stop before changing the
private factory repository.
```

---

## 17. AI Native plan definition of done

This repository's factory-runner work is complete when:

- each `AN-*` phase was delivered as its own serial PR and GitHub reports each
  protected merge on the default branch;
- required CI, security, policy, packaging, and compatibility checks remained
  blocking, and the trusted publisher/control plane controlled readiness and
  GitHub auto-merge;
- no attempt sandbox received merge, administration, review-approval,
  branch-protection-bypass, force-push, or deployment credentials;
- any operator intervention was limited to a documented scope, security,
  permission, conflict, or failed-check exception and did not bypass policy;
- existing interactive CLI and template workflows remain green;
- `factory-runner-protocol/v1` contracts are documented, generated,
  schema-valid, and language-neutral;
- `RunSpec`, `ContextBundle`, `Checkpoint`, `VerificationEvidence`,
  `ChangeSet`, `RunResult`, and `RunnerEvent` have stable v1 schemas;
- `ainative factory run` executes without a TTY or prompt;
- `ainative factory verify` performs deterministic verification without agent
  authoring;
- factory mode cannot invoke commit, push, PR, merge, or credential discovery;
- factory mode accepts only the documented attempt-scoped model-gateway
  capability;
- events, checkpoints, evidence, change sets, results, and completion
  manifests are atomic, sanitised, and content-addressed;
- a replacement attempt can restore a compatible checkpoint;
- invalid or authority-expanding checkpoints fail closed;
- TDD evidence rejects false-red failures;
- authoring evidence cannot be represented as clean verification;
- path, mode, symlink, traversal, size, and secret policies fail closed;
- no-change, blocked, failed, cancelled, and timeout outcomes are explicit;
- the wheel installs and runs from an empty environment;
- the OCI runner works as non-root with a read-only root filesystem;
- wheel and OCI output pass the same compatibility suite;
- the OCI image is published by immutable digest;
- the released wheel and image trace to the same Git commit;
- SBOM, scan, signature, and provenance are available;
- the formal release receipt contains the actual wheel version, Git commit
  SHA, OCI digest-pinned reference, schema-set digest, schema-manifest digest,
  and passing compatibility-suite results;
- the private factory can begin `FF-00` using only that receipt and published
  artifacts.

`AN-00` is the first implementation phase.
