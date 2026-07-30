# Factory runner boundary

This directory records the public AI Native side of
`factory-runner-protocol/v1`. The implementation is split into five serial
phases defined by
[`phase-manifest.json`](phase-manifest.json).

AN-00 reserves `ainative factory` and establishes an executable authority
ceiling. It does not implement `factory run`, `factory verify`, protocol
documents, release artifacts, or any private factory service.

AN-01 defines the language-neutral protocol documents, checked Draft 2020-12
schemas, canonical digest rules, capability negotiation, and pure checkpoint
compatibility. It still does not execute `factory run` or `factory verify`.
Execution, workspace enforcement, patches, and release artifacts belong to
later phases and may begin only after their prerequisite PR is human-merged.

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
installed-wheel schema resources, and the reserved factory CLI boundary.

Protocol resources:

- [Human-readable protocol v1](protocol-v1.md)
- [Checked-in v1 JSON Schemas](../../ai_native/schemas/factory_runner/v1/)
- [Golden minimal and complete documents](../../tests/fixtures/factory_runner/golden/)
- [Schema-invalid fixture corpus](../../tests/fixtures/factory_runner/schema-invalid/)

Boundary records:

- [ADR 0001: two-repository authority boundary](adr/0001-two-repository-authority-boundary.md)
- [ADR 0002: protocol v1 wire decisions](adr/0002-protocol-v1-wire-decisions.md)
- [Existing-module inventory](module-inventory.md)
- [Runner security boundary and initial threat analysis](security-boundary.md)
- [AN-00 test evidence](evidence/AN-00.md)
- [AN-01 contract evidence](evidence/AN-01.md)
