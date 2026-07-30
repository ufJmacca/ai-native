# Factory runner boundary

This directory records the public AI Native side of
`factory-runner-protocol/v1`. The implementation is split into five serial
phases defined by
[`phase-manifest.json`](phase-manifest.json).

AN-00 reserved `ainative factory` and established an executable authority
ceiling. AN-01 then defined the language-neutral protocol documents, checked
Draft 2020-12 schemas, canonical digest rules, capability negotiation, and
pure checkpoint compatibility.

AN-02 implements bounded, unattended `factory run` and `factory verify`
operations. It validates immutable inputs and exact repository state, adapts
only admitted legacy authoring stages, runs deterministic commands without a
TTY, rejects configured Git remotes and publication commands, filters child
environments, enforces cancellation and deadlines, and writes a minimal
schema-valid terminal result with `completion.json` last.

AN-03 will extend the minimal terminal surface with the complete append-only
event, checkpoint, evidence, redaction, recovery, and ChangeSet output
semantics. AN-04 owns release artifacts and compatibility certification. Each
phase starts only after automation verifies its prerequisite merge as the
exact default-branch HEAD; protected auto-merge remains gated by required
checks and machine-verifiable phase evidence.

The complete deterministic root-package test command used by CI is:

```bash
make test
```

When invoked from the host, the Makefile runs the suite in the Docker Compose
`workspace` service. The CI-equivalent command is:

```bash
docker compose run --rm --user root workspace uv run pytest
```

The factory contract suite independently validates the golden documents with
the Python models and the packaged JSON Schemas. It also checks schema drift,
canonical digests, protocol negotiation, checkpoint authority narrowing,
installed-wheel schema resources, and the active non-interactive factory CLI
surface.

Protocol resources:

- [Human-readable protocol v1](protocol-v1.md)
- [Checked-in v1 JSON Schemas](../../ai_native/schemas/factory_runner/v1/)
- [Golden minimal and complete documents](../../tests/fixtures/factory_runner/golden/)
- [Schema-invalid fixture corpus](../../tests/fixtures/factory_runner/schema-invalid/)

Boundary records:

- [ADR 0001: two-repository authority boundary](adr/0001-two-repository-authority-boundary.md)
- [ADR 0002: protocol v1 wire decisions](adr/0002-protocol-v1-wire-decisions.md)
- [Existing-module inventory](module-inventory.md)
- [AN-02 attempt-scoped model-gateway contract](gateway-contract.md)
- [Runner security boundary and initial threat analysis](security-boundary.md)
- [AN-00 test evidence](evidence/AN-00.md)
- [AN-01 contract evidence](evidence/AN-01.md)
- [AN-02 runner evidence](evidence/AN-02.md)
