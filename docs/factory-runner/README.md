# Factory runner boundary

This directory records the public AI Native side of
`factory-runner-protocol/v1`. The implementation is split into five serial
phases defined by
[`phase-manifest.json`](phase-manifest.json).

AN-00 reserves `ainative factory` and establishes an executable authority
ceiling. It does not implement `factory run`, `factory verify`, protocol
documents, release artifacts, or any private factory service. Those changes
belong to later phases and may begin only after their prerequisite PR is
human-merged.

The complete deterministic repository test command is:

```bash
make test
```

When invoked from the host, the Makefile runs the suite in the Docker Compose
`workspace` service. The CI-equivalent command is:

```bash
docker compose run --rm --user root workspace uv run pytest
```

The new factory tests include an installed console-script smoke check, so the
same command verifies both package import and `ainative factory --help`.

Boundary records:

- [ADR 0001: two-repository authority boundary](adr/0001-two-repository-authority-boundary.md)
- [Existing-module inventory](module-inventory.md)
- [Runner security boundary and initial threat analysis](security-boundary.md)
- [AN-00 test evidence](evidence/AN-00.md)

