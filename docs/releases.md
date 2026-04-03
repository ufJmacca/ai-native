# Releases

Automated SemVer releases are currently enabled only for the root `ai-native-base` CLI package.

## Current scope

- Release Please manages the root package version in `pyproject.toml`, the root `CHANGELOG.md`, component-prefixed tags, and GitHub Releases.
- The active component name is `ai-native-base`, so release tags will look like `ai-native-base-v1.0.0`.
- Root release parsing excludes docs-only changes and the self-hosted service paths so service and UI work can continue independently without moving the root release line.

## Required GitHub secret

- `RELEASE_PLEASE_TOKEN`: a dedicated token with permission to open and update release PRs and push the `uv.lock` sync commit back to those PR branches.

## Release PR automation

- `.github/workflows/release-please.yml` opens and maintains the release PR on pushes to `main`.
- `.github/workflows/release-pr-uv-lock.yml` runs on release PRs labeled `autorelease: pending`, regenerates the root `uv.lock`, and pushes the lockfile update back to the same PR branch when needed.
- `.github/workflows/semantic-pr-title.yml` enforces conventional PR titles so squash merges produce SemVer-friendly commit messages.

## Reserved future components

These components are intentionally not active yet and remain at `0.1.0`:

- `run-registry`: `services/run_registry`
- `run-registry-ui`: `services/run_registry_ui`

To activate either later, add a manifest entry for the component path, give it a dedicated changelog, and remove that path from the root component's `exclude-paths`.
