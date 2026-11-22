# Agent Instructions

## General

- Use the repository's devcontainer or Docker Compose `workspace` service for
  project dependencies and project tooling.
- If a repository does not already provide a devcontainer, add one before
  installing project dependencies or running project tooling.
- Do not install project dependencies directly on the host. When running from
  the host, prefer the existing `make` targets, which route commands through
  `docker compose run --rm workspace`.
- Use host tools when interacting with GitHub, Docker, or credentials.
- Prefer `uv` over `pip`.
- Follow TDD: Red → Green → Refactor.
- Create small, reviewable commits.
- Prefer one PR per implementation phase.
- Run tests before marking work complete.
- Never assume authentication is broken without verifying using host tools.

## GitHub CLI

- Use the host-installed `gh` CLI for GitHub operations.
- Assume `gh` is authenticated on the host unless `gh auth status` executed on
  the host reports otherwise.
- Prefer `gh` over direct GitHub REST API calls when both are suitable.
