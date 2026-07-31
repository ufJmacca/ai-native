# Factory runner protocol v1

`factory-runner-protocol/v1` is the language-neutral boundary between a
factory and one non-interactive AI Native runner invocation. AN-01 defines the
initial domain documents and pure compatibility rules. AN-02 implements
bounded execution, and AN-03 implements the complete content-addressed output
and recovery lifecycle.

The authoritative machine-readable definitions are the checked-in
[Draft 2020-12 JSON Schemas](../../ai_native/schemas/factory_runner/v1/).
This document explains their intent and the semantic procedures that a
conforming implementation must apply in addition to schema validation.

## Compatibility promise

Protocol v1 contains nine top-level schemas:

| Contract | Schema identifier | Purpose |
|---|---|---|
| `RunSpec` | `run-spec/v1` | Immutable instruction for one author or verify invocation |
| `ContextBundle` | `context-bundle/v1` | Deterministic, repository-scoped input context |
| `Checkpoint` | `checkpoint/v1` | Portable progress and authority snapshot |
| `VerificationEvidence` | `verification-evidence/v1` | Structured red, green, refactor, and verification evidence |
| `ChangeSet` | `change-set/v1` | Sanitised authoring output and deterministic patch reference |
| `ProtocolManifest` | `protocol-manifest/v1` | Acyclic content-addressed output inventory |
| `RunResult` | `run-result/v1` | Terminal invocation receipt |
| `CompletionManifest` | `completion/v1` | Last-write marker binding the result and output manifest |
| `RunnerEvent` | `runner-event/v1` | Attempt-local progress fact |

All documents use:

- `protocol: "factory-runner-protocol/v1"`;
- their exact schema identifier;
- integer `schema_version: 1`;
- strict field types with unknown fields rejected.

A breaking document or semantic change requires a new protocol major version.
An additive optional capability is usable only after explicit negotiation.

## Validation and enforcement layers

Conformance has three distinct layers. Passing one layer does not imply that a
later layer has passed.

### 1. JSON decoding and structural validation

A wire document is a UTF-8 JSON object. Decoding rejects malformed JSON,
duplicate object member names, non-finite numeric tokens, and integer tokens
outside the interoperable RFC 8785 domain. The generated JSON Schema then
checks document shape, required fields, strict primitive types, unknown
fields, enumerations, lexical formats, numeric bounds, and the cross-field
conditionals encoded in that schema.

The schemas deliberately encode important local rules such as the `RunSpec`
author/verify split, change-file operation fields, evidence digest direction,
result references, nested JSON bounds, and prohibited event-payload keys.
Duplicate member detection still belongs to the decoder because a JSON Schema
validator receives an already-decoded object.

### 2. Portable document semantics

Every implementation must also implement the deterministic semantic rules in
this document. These include:

- preserving opaque identity values exactly;
- comparing timestamps without losing their optional nanosecond precision;
- enforcing uniqueness keyed by logical path where whole-object JSON Schema
  uniqueness is insufficient;
- enforcing internally consistent checkpoint budgets and stage state;
- enforcing evidence, changed-file, and result cross-field rules;
- enforcing sorted acyclic terminal manifests and canonical artifact paths;
- RFC 8785 canonicalisation and digest verification;
- protocol and capability negotiation;
- cross-document checkpoint compatibility and authority narrowing.

The Python models implement these rules, and the public validation API maps
failures to stable protocol codes. A non-Python consumer must implement
equivalent behavior; validating only against the JSON Schemas is not enough to
verify a digest or authorise a resume.

### 3. Runtime enforcement

Schema and portable semantic validation do not prove anything about a real
workspace or execute an operation. The AN-02 and AN-03 runtime enforces:

- input and artifact paths against a real filesystem;
- artifact byte sizes and digests against actual bytes;
- workspace base state, patch application, and transactional restore;
- command, environment, network, credential, stage, and path authority while
  work is running;
- evidence capture, event sequencing, output limits, and cancellation;
- deterministic patch generation and changed-file inspection;
- secret scanning, redaction, and runner-level publication denial independent
  of the outer sandbox controls.

A structurally valid `allowed_path_decision: "allowed"` remains a claim to be
established by runtime policy enforcement, not proof by itself. Hostile-code
containment and publication authority remain private-factory responsibilities.

## Common wire rules

### Document envelopes

`RunSpec`, `ContextBundle`, `Checkpoint`, `VerificationEvidence`, and
`ChangeSet` share a nested envelope:

- direct `protocol`, `schema`, `schema_version`, and `created_at` fields;
- `identity`, containing work-item, revision, delivery-phase, run, attempt,
  and correlation identifiers;
- `repository`, containing repository identity, display name, and the
  40-character lowercase base commit SHA.

`RunResult` uses the same version and timestamp fields. Its `identity` and
`repository` members are always required, but their values may be `null` for
an early `outcome: "invalid_input"`. Both values must be non-null for every
other outcome.

`RunnerEvent` deliberately uses a flat event envelope with `run_id`,
`attempt_id`, `correlation_id`, nullable `causation_id`, and `timestamp`.
It does not have `created_at`, `identity`, or `repository`.

### Opaque identities

Factory-supplied identities are non-empty strings. Implementations preserve
their decoded JSON string values exactly: no trimming, case folding, Unicode
normalisation, or replacement. Control characters and non-string values are
invalid.

### Timestamps

Timestamps use UTC RFC 3339 with a literal `Z` and may include one through nine
fractional digits. When ordering two timestamps, implementations compare all
provided fractional digits rather than truncating to microseconds.

### Digests and numeric domain

A digest has the exact lowercase form `sha256:<64-lowercase-hex>`.

Numbers must be finite and stay within the interoperable RFC 8785/IEEE-754
domain. Integer values are limited to
`-9007199254740991..9007199254740991`; counts and byte sizes have the narrower
positive or non-negative bounds declared by their schemas. Arbitrary JSON in
checkpoint state and event payloads follows the same domain. JSON Schema
defines an integer by mathematical value, so an integral JSON number such as
`1.0` is accepted for an integer field and normalised to `1`; booleans,
strings, non-integral numbers, and out-of-range values are rejected.

### Paths

Artifact and repository paths are normalised, repository-relative POSIX paths.
They are at most 4,096 characters.
They may not:

- be absolute or empty;
- contain ASCII control characters (`U+0000` through `U+001F` or `U+007F`),
  backslashes, repeated separators, `.` or `..` segments;
- end in a separator;
- address a `.git` component;
- contain wildcards.

Workspace, input, resume, and output locations in `RunSpec` are normalised
absolute, non-root POSIX paths of at most 4,096 characters without those ASCII
control characters or dot segments.

Policy paths use a deliberately small grammar:

- `**` covers the whole repository;
- `path` covers that exact path;
- `path/**` covers that path and its subtree.

No other wildcard is valid. Allowed paths may not address `.git`. Prohibited
paths may name `.git` so the boundary can be denied explicitly. A prohibition
wins over an allowance at runtime.

### Commands, environment, and profiles

Commands are non-empty ordered arrays of non-empty arguments, never shell
strings. Each argument is at most 16,384 characters and contains no NUL.
Environment authority lists variable names only, never values. Network,
credential, and model selection are opaque profile names. The only v1
credential profile is `no-external-credentials`, and a model profile may not
look like a URL or secret reference.

### Immutable validated values

The wire representation is ordinary JSON. In the Python API, validated
contract models are frozen, repeated fields are tuples, and arbitrary durable
JSON mappings are detached and recursively frozen. This includes checkpoint
`workflow_state`, event `sanitised_payload`, and evidence `tool_versions`.
Validation does not mutate the caller's input.

This in-memory immutability prevents a document from changing after it has
been validated or digested. It is an implementation safety property rather
than something JSON Schema can express.

### Runner build identity

`RunnerBuildIdentity` always contains the required members `version`, `image`,
and `source_commit`. `image` and `source_commit` use `null` to state that the
value is unavailable; implementations do not drop those members.

## `RunSpec`

`RunSpec` is the complete instruction for one invocation. Its required
top-level sections are:

- `operation`: `author` or `verify`;
- `workspace`: absolute path and declared initial state;
- `task`: outcome, acceptance criteria, non-goals, and constraints;
- `policy`: paths, stages, commands, environment names, profiles, and budgets;
- `capabilities`: disjoint required and optional capability names;
- `context`: context-manifest path and expected bundle digest;
- `verification_input`: an operation-conditional ChangeSet binding or `null`;
- `resume`: a required object containing a nullable checkpoint binding;
- `outputs`: output directory and event-stream selection.

Operation rules are exact:

| Operation | Workspace state | Allowed stages | `verification_input` |
|---|---|---|---|
| `author` | `clean_base` | One or more v1 factory stages | Must be `null` |
| `verify` | `prepared_verification` | Exactly `["verify"]`, with at least one allowed command | Required and digest-bound |

The v1 stage vocabulary is `intake`, `recon`, `plan`, `architecture`, `prd`,
`slice`, `loop`, and `verify`. Commit, pull-request, push, and merge stages are
not representable.

`verification_input.change_set_path` is an absolute POSIX path and
`verification_input.expected_digest` declares the expected self digest of the
supplied ChangeSet document.
This input lets the verify operation validate an already-prepared checkout;
verify never authors a new change set.

`resume.checkpoint_path` and `resume.expected_digest` must either both be
`null` or both be non-null. `allowed_paths` and `allowed_stages` are non-empty.
All authority lists reject duplicates, required and optional capabilities
must not overlap, and a policy path cannot appear identically in both the
allowed and prohibited lists.

## `ContextBundle`

`ContextBundle` contains:

- a context-bundle identity;
- one or more ordered manifest entries;
- the normalised work-item revision;
- repository instructions and trusted policy summary;
- approved repository memory, dependency outputs, and operator input;
- construction metadata and source digests;
- `bundle_digest`.

Every manifest entry has a unique logical path, media type, non-negative byte
size, digest, and one of `work_item_revision`, `repository_instruction`,
`trusted_policy`, `approved_project_memory`, `dependency_output`,
`operator_input`, or `supporting_artifact`. Exactly one entry has
classification `work_item_revision`. Construction source digests are unique.

The contract describes content-addressed inputs. Confirming that referenced
objects exist and match their byte size and digest is runtime work.

## `Checkpoint`

`Checkpoint` is a portable safe-boundary record, not a process snapshot. It
contains:

- the producing attempt, a positive sequence, and compatibility requirements;
- the original `operation` and a required-nullable verification ChangeSet
  digest binding;
- context, input RunSpec, optional workspace patch, and object digests;
- completed stages and the next permitted stage;
- bounded structured `workflow_state`;
- evidence and artifact references;
- the complete original `RunPolicy` authority;
- consumed and remaining budgets;
- sanitised decisions, assumptions, open questions, and a self digest.

The producing attempt must equal `identity.attempt_id`. Completed stages and
the optional next stage must be inside the stored authority. For wall time,
agent turns, and model tokens, `consumed + remaining` must exactly equal the
corresponding original authority limit. Artifact-manifest logical paths are
unique.

An author checkpoint has `operation: "author"` and
`verification_change_set_digest: null`. A verify checkpoint has
`operation: "verify"` and a non-null `verification_change_set_digest` that
binds the ChangeSet being verified. It also preserves its valid verify origin:
stored stage authority is exactly `["verify"]`, allowed commands are non-empty,
completed stages contain only `verify`, and the next permitted stage is either
`verify` or `null`.

`workflow_state` is JSON-only, has at most 16 nested collection levels, uses
the safe numeric domain, and is limited to 262,144 RFC-8785-canonical JSON
bytes. The generated schema expresses its recursive structural and numeric
bounds; the encoded byte limit is a semantic model check.

### Resume compatibility algorithm

`validate_checkpoint_compatibility` requires the caller to provide the
runner's `supported_capabilities`. It accepts a checkpoint and later RunSpec
only when all of these conditions hold:

1. Both documents are valid, the checkpoint self digest verifies, and the
   outer checkpoint protocol, checkpoint compatibility protocol, and RunSpec
   protocol are exactly `factory-runner-protocol/v1`.
2. Work-item ID, work-item revision ID, delivery phase ID, run ID, repository
   ID, base commit SHA, and context-bundle digest match.
3. The resumed attempt differs from `producer_attempt_id`.
4. The RunSpec names a checkpoint path and its expected digest equals the
   stored `checkpoint_digest`.
5. The checkpoint operation equals the resumed RunSpec operation. For verify,
   the checkpoint's `verification_change_set_digest` equals
   `verification_input.expected_digest`.
6. The effective runner version is an exact, at-most-128-character
   `major.minor.patch` semantic version greater than or equal to
   `minimum_runner_version`.
7. Every checkpoint-required capability is declared by the resumed RunSpec,
   either required or optional, and is enabled by the supplied runner
   capability set. Normal RunSpec capability negotiation must also succeed.
8. Allowed stages, environment names, commands, and paths do not exceed the
   stored authority. Allowed path comparison applies the `**` and terminal
   `/**` coverage rules rather than comparing strings only.
9. Every stored prohibition is preserved. A resumed prohibition may cover a
   larger subtree because that narrows authority.
10. Network, credential, and model profiles match exactly.
11. Each resumed budget is no greater than its original authority limit and
    no less than the amount already consumed.
12. A stored `next_permitted_stage`, when present, remains allowed.

Successful validation returns an immutable receipt containing both attempt
IDs and the names of authority fields that were narrowed. Any failure is
`checkpoint_incompatible`; compatibility validation does not restore or
modify a workspace.

## `VerificationEvidence`

`VerificationEvidence` records an ordered, non-empty sequence of command
evidence items plus runner identity, context binding, overall status, advisory
observations, and a self digest.

Each evidence item records an argument-array command, repository-relative
working directory, environment-name allowlist, timestamps, duration, exit and
termination data, expected and actual status, failure classification,
stdout/stderr and test-report artifacts, tool versions, and whether repository
files changed.

Semantic rules include:

- finish time must not precede start time;
- a passing item exits normally with code zero and failure classification
  `none`;
- red evidence terminates by normal process exit with a non-zero code;
- red evidence expects and observes `failed`;
- red evidence uses `expected_behavioral_failure`, not a syntax, collection,
  dependency, credential, infrastructure, timeout, or unrelated failure;
- every green, refactor, and verification item expects `passed`;
- a timed-out item is failed with classification `timeout`, and that
  classification is used only with timed-out termination;
- a `not_run` item has no exit code, did not start, and has failure
  classification `none`;
- an item that did not start is either `blocked` or `not_run`;
- a failed item must have started and carry a non-`none` classification.

`overall_status` is derived, not advisory. An expected Red behavioral failure
is satisfied evidence and does not make the aggregate fail. Implementations
derive the aggregate from `items` in this exact priority order:

1. `failed` when any non-Red item has `actual_status: "failed"`;
2. otherwise `blocked` when any item has `actual_status: "blocked"`;
3. otherwise `not_run` when any item has `actual_status: "not_run"`;
4. otherwise `passed`.

The declared `overall_status` must equal that derived value. Consequently, a
valid expected Red failure by itself aggregates to `passed`, as does an
expected Red failure followed by passing Green evidence.

Digest direction is intentionally one-way:

- `environment_kind: "authoring"` requires `change_set_digest: null`;
- `environment_kind: "clean_verification"` requires a non-null
  `change_set_digest`, contains only `phase: "verification"` items, and, when
  passing overall, reports no repository-file mutation. Failed clean
  verification may report repository-file mutation as evidence of the
  failure.

The authoring evidence can therefore be finalised before the ChangeSet, and
the ChangeSet can bind that evidence set without a digest cycle.

## `ChangeSet`

`ChangeSet` is a sanitised authoring output, not a commit or publication
request. It binds:

- runner and context digests;
- a deterministic patch artifact;
- a semantic changed-file-manifest digest;
- one or more changed files;
- the authoring evidence-set digest and evidence artifacts;
- acceptance-criterion results and sanitised summaries;
- generated artifacts and its own self digest.

Protocol v1 patch media type is exactly
`application/vnd.git.binary-patch`. The later output phase generates the
artifact with:

```text
git diff --binary --full-index --no-color --no-ext-diff \
  --src-prefix=a/ --dst-prefix=b/ <base-commit> --
```

Generation uses `LC_ALL=C`, no external diff, fixed prefixes, and no `.git`
paths. The patch artifact digest covers the exact patch bytes.
`diff_digest` instead covers the RFC-8785-canonical ordered changed-file
manifest.

The public `changed_file_manifest_digest(...)` helper requires an actual
non-empty ordered sequence rather than a set or one-shot iterator. It
revalidates every changed-file entry, enforces unique resulting paths and
unique source-side paths, serialises entries in the supplied order as a JSON
array, RFC-8785-canonicalises that array, and returns its prefixed SHA-256
digest. ChangeSet model validation requires:

```text
diff_digest == changed_file_manifest_digest(changed_files)
```

Reordering or modifying an entry therefore invalidates the ChangeSet until
`diff_digest` is recomputed. Draft 2020-12 validation checks the manifest and
digest field structurally; computing and comparing this digest is portable
semantic validation.

Resulting paths are unique, as are source-side paths across modify, delete, and
rename entries. Each entry has operation `add`, `modify`, `delete`, or
`rename`; only regular modes `100644` and `100755` are supported. Nullable
previous/resulting path, blob, and mode fields must agree with the operation.
A modify must change its blob digest or mode, and a rename must have a distinct
previous path. The patch artifact has a positive byte size.
`allowed_path_decision` is exactly `allowed`.

Branch names, commit identity, pull-request metadata, labels, merge state,
publication tokens, and other control-plane data are outside this contract.

## `ProtocolManifest`

`ProtocolManifest` is the immutable inventory of all acknowledged artifacts
that exist before the terminal RunResult. Its `artifacts` sequence is
non-empty, sorted lexically by logical path, and has unique paths. It contains
exactly the declared `events.ndjson` reference with media type
`application/x-ndjson`.

The manifest deliberately excludes `protocol-manifest.json`,
`result/run-result.json`, and `completion.json`. This keeps the digest graph
acyclic: the RunResult binds the exact-byte digest of the protocol manifest,
and the completion marker can then bind both documents. JSON Schema enforces
the canonical event path, exactly one event entry, and terminal-path
exclusion; semantic validation additionally checks sort order and exact
event-reference equality.

## `RunResult`

`RunResult` is the terminal receipt. Outcomes are:

- `succeeded`;
- `no_change`;
- `blocked`;
- `failed`;
- `cancelled`;
- `timed_out`;
- `invalid_input`;
- `checkpoint_incompatible`;
- `policy_denied`.

It includes operation, outcome, stable reason code, sanitised message, timing,
completed stages, nullable output references, stream and manifest digests,
runner build identity, and a self digest.
The message is non-empty and at most 4,096 characters.

An early `invalid_input` result may use `null` for the required `identity` and
`repository` members because those untrusted values might never have
validated. Every other outcome requires non-null values for both. Further
rules are:

- author results require `verification_evidence: null`;
- verify results never reference a ChangeSet;
- successful author results require a ChangeSet reference;
- successful verify results require a VerificationEvidence reference;
- verify results may list only the `verify` completed stage, and a successful
  verify result lists exactly `["verify"]`;
- `no_change` is author-only and has neither a `change_set` nor
  `verification_evidence` reference;
- finish time must not precede start time.

## `CompletionManifest`

`CompletionManifest` is the last-written marker for one sealed output
directory. It references the canonical `result/run-result.json` and requires
the exact RunResult digest. When `protocol_manifest` is present, it references
`protocol-manifest.json`; `output_manifest_digest` must equal that reference's
digest. When the protocol-manifest reference is absent, the output-manifest
digest remains a required legacy AN-02 artifact-inventory binding.

The AN-03 writer always supplies the protocol-manifest reference. The nullable
shape preserves compatibility with the minimal AN-02 completion chain while
giving consumers one typed contract for both forms. Consumers treat an absent
or invalid completion marker as an incomplete attempt, never as successful
publication.

## `RunnerEvent`

`RunnerEvent` reports an attempt-local fact through the explicit flat
envelope. `sequence` is positive, and `event_type` is one of:

`RunnerStarted`, `InputValidated`, `CheckpointRestored`, `StageStarted`,
`StageCompleted`, `ToolStarted`, `ToolCompleted`, `TestStarted`,
`TestCompleted`, `FileManifestChanged`, `CheckpointWritten`,
`ChangeSetWritten`, `VerificationEvidenceWritten`, `PolicyDenied`,
`RunnerCancellationRequested`, `RunnerCompleted`, or `RunnerFailed`.

Control-plane facts such as queueing, sandbox leasing, pull requests, database
transactions, and merges are invalid event types.

`sanitised_payload` is JSON-only, deeply immutable after Python validation,
limited to eight nested collection levels and 16,384 RFC-8785-canonical JSON
bytes, and limits each inline string to 4,096 characters. Prohibited key names
are checked recursively and ASCII case-insensitively by both the generated
schema and the semantic model. The exact names are `authorization`, `branch_name`,
`credential`, `github_token`, `hidden_reasoning`, `merge`, `password`,
`pr_body`, `pr_title`, `publication_token`, `pull_request`, `raw_reasoning`,
`secret`, and `token`. Large or sensitive output belongs in a redacted
artifact referenced by `artifact_refs`, not inline.

Runner events have no self digest. A complete stream may be bound by the
`RunResult.event_stream_digest`.

## Capability negotiation

Negotiation requires the exact protocol plus ordered, duplicate-free
sequences of required, optional, and runner-supported capability names.
Unordered sets, mappings, strings, and one-shot iterators are invalid.
Required and optional names may not overlap.

- An unsupported required capability fails with
  `unsupported_capability`.
- A supported optional capability is enabled.
- An unsupported optional capability is ignored and reported.
- Negotiated order is all required names followed by supported optional names,
  preserving declaration order.

Capabilities select supported behavior; they never widen `RunPolicy`
authority.

## Canonical JSON and digests

Canonical JSON is RFC 8785 JSON Canonicalization Scheme output encoded as
UTF-8. SHA-256 is calculated over those exact bytes and rendered with the
lowercase `sha256:` prefix.

Artifact digests are different: they cover exact artifact bytes without JSON
parsing or canonicalisation.

### Self-digest algorithm

Five document types are self-digesting:

| Schema | Own digest field |
|---|---|
| `context-bundle/v1` | `bundle_digest` |
| `checkpoint/v1` | `checkpoint_digest` |
| `verification-evidence/v1` | `evidence_set_digest` |
| `change-set/v1` | `change_set_digest` |
| `run-result/v1` | `result_digest` |

To calculate or verify one:

1. Require the document's own top-level digest field.
2. Copy the complete decoded document.
3. Delete only that one top-level member. Do not replace it with `null` and do
   not remove any other digest field.
4. RFC-8785-canonicalise the remaining object.
5. SHA-256 the canonical bytes and add the `sha256:` prefix.
6. Compare the result with the declared value.

`RunSpec`, `RunnerEvent`, `ProtocolManifest`, and `CompletionManifest` have no
self-digest field. When an external digest is needed for one of them, it
covers the canonical complete document.

### Schema manifest

Schema generation writes nine deterministic, pretty-printed schema files,
`schema-manifest.json`, and `schema-set.sha256`.

The manifest:

- identifies protocol, manifest version, JSON Schema draft, and RFC 8785;
- lists schemas in lexical POSIX filename order;
- records each filename, schema identifier, schema `$id`, and the digest of
  that schema's RFC-8785-canonical decoded JSON.

`schema-set.sha256` is the digest of the RFC-8785-canonical decoded manifest.
It is the canonical schema-set identity.

The schema-manifest digest exposed by the Python API is separate: it hashes the
exact deterministic pretty-printed `schema-manifest.json` file bytes. This raw
manifest digest must not be substituted for the schema-set digest. The
manifest contains neither value, avoiding self-reference.

## Stable validation failures

Consumers may depend on these machine-readable codes:

| Code | Meaning |
|---|---|
| `invalid_json` | Malformed UTF-8 JSON, duplicate member, non-finite number, or integer outside the interoperable RFC 8785 domain |
| `invalid_input` | Structurally or semantically invalid contract input |
| `unsupported_protocol` | Protocol identifier is not supported |
| `unsupported_schema` | Schema identifier is unknown or not the expected schema |
| `unsupported_schema_version` | Schema version is not the mathematical integer value `1` |
| `unsupported_capability` | A required capability is not supported |
| `digest_mismatch` | Exact-byte or canonical document digest does not match |
| `checkpoint_incompatible` | A checkpoint cannot safely resume under the supplied RunSpec and runner |
| `policy_denied` | Runtime policy denied an attempted action |

Error messages may become more precise. Raw Pydantic and JSON Schema validator
identifiers are not stable protocol values.

## Schemas, examples, and generation

- [Checked-in v1 schemas](../../ai_native/schemas/factory_runner/v1/)
- [Minimal and complete golden documents](../../tests/fixtures/factory_runner/golden/)
- [Schema-invalid corpus](../../tests/fixtures/factory_runner/schema-invalid/)
- [Wire-decision ADR](adr/0002-protocol-v1-wire-decisions.md)

Regenerate or check the schema set inside the repository workspace container:

```bash
docker compose run --rm workspace \
  uv run python scripts/generate_factory_runner_schemas.py --write

docker compose run --rm workspace \
  uv run python scripts/generate_factory_runner_schemas.py --check

docker compose run --rm workspace \
  uv run python scripts/generate_factory_runner_goldens.py --write

docker compose run --rm workspace \
  uv run python scripts/generate_factory_runner_goldens.py --check
```

CI treats checked-in schema drift, writer-generated terminal-golden drift,
missing package resources, and golden fixture disagreement between Pydantic
and independent Draft 2020-12 validation as failures.
