# ADR 0001: two-repository runner authority boundary

- Status: Accepted
- Date: 30 July 2026
- Phase: AN-00

## Context

AI Native already provides an interactive workflow that can inspect a target
repository, run agents, create commits, push branches, and open pull requests.
The private AI Native Factory needs the planning and authoring engine, but it
must not give an untrusted attempt sandbox publication credentials or control
of queueing, durable workflow, sandbox lifecycle, or operator services.

The two repositories also need independent Git histories and release
lifecycles. A sibling checkout is useful for local diagnosis but is not an
immutable integration boundary.

## Decision

`ai-native` owns a portable, non-interactive runner and the versioned
`factory-runner-protocol/v1` contracts. `ai-native-factory` owns the durable
platform, sandbox lifecycle, trusted publisher, and all operator-facing
services.

The repositories integrate only through a released AI Native wheel, a runner
OCI image pinned by digest, published protocol schemas, and a formal release
receipt. The private factory must not import AI Native internals, use a sibling
path dependency, vendor the contracts, or consume a moving branch or tag.

Factory mode has a separate, immutable capability policy:

- permitted capabilities: author and verify;
- prohibited capabilities: interactive input, commit, pull-request
  publication, push, and merge;
- permitted workflow stages: legacy stages other than `commit` and `pr`,
  further narrowed by the future validated `RunSpec`;
- current AN-00 CLI surface: help-only `ainative factory`.

The executable policy is an authority ceiling, not a complete sandbox. The
private factory remains responsible for filesystem, process, credential, and
network isolation.

## Ownership

| Concern | Owner |
|---|---|
| Protocol schemas, validation, runner, local outputs | `ai-native` |
| Wheel, OCI image, compatibility report, release receipt | `ai-native` |
| Queue, runs, attempts, commands, leases, fencing, budgets | `ai-native-factory` |
| Sandbox creation, credentials, network policy | `ai-native-factory` |
| Branch, commit, push, PR lifecycle, merge | Trusted factory publisher |
| Authoring patch and supporting evidence | AI Native runner |

## Consequences

- Existing interactive commands and defaults stay intact.
- Factory execution must use a dedicated adapter rather than invoking the
  publication-capable legacy workflow unchanged.
- Any later runner or protocol gap is fixed and released in `ai-native`
  before a separate factory dependency-lock PR consumes it.
- AN-01 can add language-neutral contracts without redefining repository
  ownership.

## Rejected alternatives

- A shared contracts repository adds coordination and release complexity for
  v1 without clarifying ownership.
- A sibling editable install is mutable and cannot satisfy compatibility or
  provenance gates.
- Copying schemas into the factory would create divergent protocol sources of
  truth.
- Moving the queue, publisher, or sandbox services into this public repository
  would breach the authority boundary.

