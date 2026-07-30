# ADR 0002: Factory runner protocol v1 wire decisions

- Status: Accepted for AN-01
- Date: 30 July 2026
- Protocol: `factory-runner-protocol/v1`

## Context

The runner implementation plan deliberately fixes the security and ownership
boundary before implementation, but a few wire-level details are ambiguous.
They must be decided before generated schemas become a compatibility promise.

## Decisions

### Contract envelopes

`RunSpec`, `ContextBundle`, `Checkpoint`, `VerificationEvidence`, and
`ChangeSet` use the same document envelope:

- `protocol`, `schema`, `schema_version`, and `created_at` are direct fields;
- factory-supplied work, run, attempt, phase, and correlation identifiers are
  grouped beneath `identity`;
- repository identity, display name, and base commit are grouped beneath
  `repository`.

`RunResult` has the same version and timestamp fields. `identity` and
`repository` are required members whose values may be null only for an early
`outcome=invalid_input`, because those untrusted source values may not have
validated. Every other outcome requires non-null values for both.

`RunnerEvent` is the one deliberate exception. It uses the explicit flat event
envelope from the plan, including `timestamp` instead of a duplicate
`created_at`.

Opaque identifiers are preserved as their exact decoded JSON string values.
The runner does not trim, case-fold, Unicode-normalise, or regenerate them.
Control characters and non-string values are invalid.

All validated Python contract values are durable and immutable. Models are
frozen, repeated values use tuples, and arbitrary JSON mappings are detached
and recursively frozen so post-validation mutation cannot invalidate a digest.
This is an implementation safety property; JSON Schema cannot express
in-memory immutability.

`RunnerBuildIdentity` has three required members: `version`, `image`, and
`source_commit`. The latter two use null to represent unavailable build
metadata; their members are never dropped.

All contract numbers are finite. Integers are restricted to the interoperable
RFC 8785/IEEE-754 safe domain
`-9007199254740991..9007199254740991`, with narrower positive or non-negative
bounds where the field requires them. Because Draft 2020-12 defines integers
by mathematical value, integral JSON numbers such as `1.0` are normalised to
integers by the Python models; booleans, strings, and non-integral numbers are
not accepted as integers. Arbitrary checkpoint state and event payload JSON
uses the same domain.

### Author and verify inputs

`RunSpec.verification_input` is operation-conditional. Authoring requires
`workspace.initial_state=clean_base` and `verification_input=null`.
Verification requires `workspace.initial_state=prepared_verification`, exactly
the `verify` stage, at least one allowed command, and a `verification_input`
containing an absolute ChangeSet path plus its expected digest.

`resume.checkpoint_path` and `resume.expected_digest` are likewise paired:
both are null or both are non-null.

### Capability negotiation and authority

`RunSpec.capabilities` separates `required` from `optional` names. An unknown
required capability fails with `unsupported_capability`; an unknown optional
capability is ignored and reported by negotiation. Capabilities never widen
the authority expressed by policy. Required, optional, and runner-supported
inputs are actual ordered sequences; sets, mappings, strings, and one-shot
iterators are rejected. The sequences reject duplicates, and required and
optional names may not overlap.

Commands are ordered argument arrays, never shell strings. Arguments are
non-empty, contain no NUL, and are at most 16,384 characters. Environment
allowlists contain names only. The model profile is an opaque profile name,
not a provider URL, token, or secret.

Path policy entries use normalised repository-relative POSIX paths. `**`
denotes the whole repository and a terminal `/**` denotes a subtree. Other
wildcards are invalid. A path containing an ASCII control character, `..`, a
backslash, repeated or trailing separators, or a `.git` component cannot be
allowed. A prohibition may name `.git` so the boundary can be stated
explicitly. Deny rules take precedence. Repository, policy, prohibited, and
absolute input/output paths are bounded to 4,096 characters.

Checkpoint authority records the complete original policy. Resume accepts a
new attempt only when its allowed paths, stages, commands, environment names,
and budgets are no wider. It may narrow those values. Network, credential,
and model profiles are opaque in v1 and therefore must match exactly.

Checkpoint compatibility requires an explicit ordered runner
`supported_capabilities` sequence. Every checkpoint-required capability must be
declared by the resumed RunSpec, either required or optional, and must be
enabled by the supplied runner support. The resumed runner version must meet
the checkpoint's exact, at-most-128-character `major.minor.patch` minimum. The
resume reference must bind the stored checkpoint digest, the operation must
match, and a verify checkpoint's stored ChangeSet digest must equal the
resumed verification input digest. Verify checkpoints retain verify-only stage
authority and history plus at least one allowed command, so they could only
have originated from valid verify RunSpecs. The checkpoint self digest must
verify, budgets may not fall below already-consumed values, stored
prohibitions must be preserved, and a stored next permitted stage must remain
allowed. Checkpoint artifact-manifest paths are unique.

The complete portable checkpoint-compatibility algorithm and path-coverage
rules are specified in
[`protocol-v1.md`](../protocol-v1.md#resume-compatibility-algorithm).

### Canonical JSON and digests

Canonical JSON is RFC 8785 JSON Canonicalization Scheme output encoded as
UTF-8. Parsing rejects duplicate object names, non-finite numbers, and integer
tokens outside the interoperable RFC 8785 domain. A digest has the exact
lowercase form
`sha256:<64-lowercase-hex>`.

Artifact digests cover the exact artifact bytes. A self-digesting document is
canonicalised after removing only its own top-level digest field; no other
field is omitted. The self-digest fields are:

- `ContextBundle.bundle_digest`;
- `Checkpoint.checkpoint_digest`;
- `VerificationEvidence.evidence_set_digest`;
- `ChangeSet.change_set_digest`;
- `RunResult.result_digest`.

`RunSpec` and `RunnerEvent` have no self-digest field.

The self-digest algorithm requires the field to exist, copies the whole
decoded document, deletes only that direct member, canonicalises everything
remaining, and hashes those canonical bytes. It does not replace the field
with null or omit any other digest.

The schema manifest lists the seven schemas in lexical POSIX filename order
with each schema identifier, schema `$id`, and canonical decoded-JSON digest.
`schema-set.sha256` contains the digest of the RFC-8785-canonical decoded
manifest. The schema-manifest digest is separate and covers the exact
deterministic, pretty-printed manifest file bytes. The later release receipt
records both. Neither is embedded in the manifest, avoiding self-reference.

### Evidence and change-set digest direction

Authoring evidence may set `change_set_digest` to null because the ChangeSet
does not exist when that evidence is finalised. A clean-verification evidence
document binds the already supplied ChangeSet digest. The ChangeSet binds the
authoring evidence-set digest. This one-way rule avoids an impossible digest
cycle.

Evidence aggregation is deterministic. Red items must expect and observe the
specific behavioral failure, and that satisfied failure does not fail the
aggregate. Every non-Red item must expect `passed`. `overall_status` is derived
with this exact priority: any unexpected non-Red `failed`, otherwise any
`blocked`, otherwise any `not_run`, otherwise `passed`. The declared value must
equal the derived value. Passing, timed-out, failed, blocked, and unstarted
item fields are mutually coherent. Clean-verification documents contain only
verification-phase items; a passing clean verification cannot report
repository-file mutation, while a failed one may.

### Patch representation

Protocol v1 selects a deterministic Git binary patch with media type
`application/vnd.git.binary-patch`. AN-03 generates it with Git's binary and
full-index modes, fixed `a/` and `b/` prefixes, no colour, no external diff,
`LC_ALL=C`, and no `.git` paths:

```text
git diff --binary --full-index --no-color --no-ext-diff \
  --src-prefix=a/ --dst-prefix=b/ <base-commit> --
```

The patch artifact digest covers those exact bytes. The public
`changed_file_manifest_digest(...)` helper calculates `diff_digest` from the
RFC-8785-canonical ordered changed-file JSON array, providing a stable semantic
diff identity distinct from the patch container bytes. ChangeSet model
validation requires `diff_digest` to equal the helper result, so entry order
and content are bound. The helper accepts only an actual ordered sequence,
revalidates every entry, and rejects duplicate resulting or source-side paths.
The patch byte size is positive. JSON Schema validates the two fields
structurally but does not calculate their equality. Patch generation and
verification remain AN-03 work.

### Validation layers and stable failures

JSON decoding rejects malformed input, duplicate member names, and non-finite
numbers, as well as integers outside the interoperable RFC 8785 domain.
Structural validation is defined by the generated Draft 2020-12 JSON Schemas
and mirrored by strict Pydantic models. The schemas encode strict shapes and
important local cross-field conditionals.

Both the `RunnerEvent` model and generated schema reject the fixed set of
secret and control-plane payload keys recursively using ASCII
case-insensitive matching. Full value-based secret scanning remains deferred
runtime enforcement.

Portable semantic validation additionally covers rules such as exact identity
preservation, nanosecond-aware timestamp ordering, keyed uniqueness, internal
budget consistency, evidence and changed-file consistency, digest
verification, capability negotiation, and cross-document checkpoint
compatibility. Schema-only validation does not verify a digest or authorise a
resume.

The public API maps implementation details to stable codes:

- `invalid_json`;
- `invalid_input`;
- `unsupported_protocol`;
- `unsupported_schema`;
- `unsupported_schema_version`;
- `unsupported_capability`;
- `digest_mismatch`;
- `checkpoint_incompatible`;
- `policy_denied`.

Raw Pydantic or JSON Schema error identifiers are not part of the protocol.

The complete normative explanation of these layers is in
[`protocol-v1.md`](../protocol-v1.md#validation-and-enforcement-layers).

## Deferred enforcement

AN-01 validates lexical documents and pure cross-document relationships. It
does not execute commands, inspect a real filesystem, restore checkpoints,
generate patches, stream events, scan secrets, or prove sandbox independence.
It also does not prove artifact bytes, byte sizes, path-policy decisions, or
workspace state merely because a document claims them. Those runtime
responsibilities remain in AN-02 and AN-03.
