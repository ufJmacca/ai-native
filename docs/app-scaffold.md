# App Scaffold

This repository now exposes a minimal `financial-rag` Python application scaffold alongside the existing template tooling.

## Local smoke checks

- `uv sync`
- `uv run financial-rag --help`
- `uv run pytest tests/smoke/test_bootstrap.py`

## Bootstrap path

Run `uv run financial-rag bootstrap` to validate that the package, docs, fixtures, and `.env.example` are present before later ADK slices add runtime integrations.
