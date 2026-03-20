# Configuration

AI Native loads configuration from `ainative.yaml` in the current repository (or nearest parent), unless you pass `--config` or set `AINATIVE_CONFIG`.

## Agent adapters

AI Native ships with Codex defaults, but you can point any role at GitHub Copilot CLI with `type: copilot-cli`.
If no explicit `ainative.yaml` is present, the built-in defaults still select Codex. Copilot-only users should copy the example below into their repository and use that as their local config.

### Copilot CLI example

The repository includes a copyable example at `docs/examples/ainative.copilot.yaml`.

```yaml
agents:
  builder:
    type: copilot-cli
    autopilot: true
    allow_all_permissions: true
    silent: true
    no_ask_user: true
    max_autopilot_continues: 10
  critic:
    type: copilot-cli
    autopilot: true
    allow_all_permissions: true
    silent: true
    no_ask_user: true
    max_autopilot_continues: 10
  verifier:
    type: copilot-cli
    autopilot: true
    allow_all_permissions: true
    silent: true
    no_ask_user: true
    max_autopilot_continues: 10
  pr_reviewer:
    type: copilot-cli
    autopilot: false
    allow_all_permissions: false
    silent: true
    no_ask_user: true
    allow_tools:
      - read
      - shell(git:*)
```

### Copilot-specific fields

- `autopilot`: enable or disable autonomous continuation. Defaults to `true` for `copilot-cli`.
- `allow_all_permissions`: when `true`, passes Copilot's full-permission mode. Defaults to `true` for `copilot-cli`.
- `silent`: suppress usage and progress noise in stdout. Defaults to `true` for `copilot-cli`.
- `no_ask_user`: make permission denials fail instead of prompting interactively. Defaults to `true` for `copilot-cli`.
- `max_autopilot_continues`: bound the number of autonomous turns. Defaults to `10` for `copilot-cli`.
- `allow_tools`, `deny_tools`, `allow_urls`, `deny_urls`: permission overrides used when `allow_all_permissions: false`.
- `extra_args`: escape hatch for additional Copilot CLI flags.

`sandbox`, `search`, and `base_branch` do not map to Copilot CLI and are ignored by the `copilot-cli` adapter.

### Copilot prerequisites

- Install the standalone `copilot` binary and make sure it is on `PATH`.
- Authenticate Copilot CLI. `ainative doctor` reports `providers.copilot.ready: true` when the standalone CLI is installed, while actual Copilot auth may come from env vars, keychain, `gh auth`, or local config depending on your setup.
- Trust the target workspace root in Copilot CLI before running AI Native, so the generated worktrees under `.ai-native/worktrees/` inherit that trust.
- This adapter intentionally does not shell through `gh copilot`; use the standalone CLI directly in v1.

### Doctor output

`ainative doctor` keeps the existing `commands` and `paths` inventory, and also reports additive provider readiness:

- `providers.codex.selected`: at least one configured agent uses `codex-exec` or `codex-review`
- `providers.codex.ready`: the Codex CLI plus `~/.codex/auth.json` and `~/.codex/config.toml` are present
- `providers.copilot.selected`: at least one configured agent uses `copilot-cli`
- `providers.copilot.ready`: the Copilot CLI is present; Copilot auth may come from env vars, keychain, `gh auth`, or local config

The command stays non-blocking. Missing credentials for an unselected provider are informational only.

## Run registry publishing

`ai_native` can publish rich run snapshots to the standalone run registry service. Publishing is best-effort: if the remote registry is unavailable or returns an error, the local workflow continues and emits a warning instead of failing the run.

### Persisted config shape

```yaml
registry:
  heartbeat_interval_seconds: 15
  liveness_ttl_seconds: 60
  liveness_grace_period_seconds: 120
  remote_url: https://registry.example.com
  auth_token: replace-me
  timeout_seconds: 5.0
```

`remote_url` and `auth_token` are optional. If either is missing, registry publishing is disabled and runs stay local-only.
For secrets, prefer `AINATIVE_RUN_REGISTRY_AUTH_TOKEN` instead of checking a real token into `ainative.yaml`.

### Environment overrides

`AppConfig` supports registry publishing overrides via environment variables:

- `AINATIVE_RUN_REGISTRY_URL`
- `AINATIVE_RUN_REGISTRY_AUTH_TOKEN`
- `AINATIVE_RUN_REGISTRY_TIMEOUT_SECONDS`

### Published snapshot behavior

- A run snapshot is published when a run is created.
- Stage transitions, scheduler updates, heartbeat ticks, and terminal states publish fresh snapshots.
- The snapshot includes run summary fields, `run_projection`, `stage_status`, `slice_states`, and registry-visible metadata.
- The registry backend computes liveness from the published heartbeat timestamps.

## Telemetry

You can configure remote telemetry settings from the CLI:

```bash
ainative telemetry configure --url https://telemetry.example.com/ingest --auth-type api_key --api-key <key> --tenant <project>
ainative telemetry show
ainative telemetry test
```

### Supported auth modes

- `api_key` using `--api-key`
- `bearer` using `--token`
- `basic` using `--username` and `--password`
- `none`

When run from an interactive terminal, `ainative telemetry configure` prompts for any missing values.

### Persisted config shape

```yaml
telemetry:
  enabled: true
  url: https://telemetry.example.com/ingest
  auth_type: api_key
  api_key: your-key
  tenant: your-project
```

### Environment overrides

`AppConfig` supports telemetry overrides via environment variables:

- `AINATIVE_TELEMETRY_ENABLED`
- `AINATIVE_TELEMETRY_URL`
- `AINATIVE_TELEMETRY_AUTH_TYPE`
- `AINATIVE_TELEMETRY_API_KEY`
- `AINATIVE_TELEMETRY_TOKEN`
- `AINATIVE_TELEMETRY_USERNAME`
- `AINATIVE_TELEMETRY_PASSWORD`
- `AINATIVE_TELEMETRY_TENANT`

### Secret handling

- `ainative telemetry show` masks `api_key`, `token`, and `password`.
- `ainative telemetry configure` prints masked values in terminal output.
- `ainative telemetry test` does not print auth headers or raw secrets.
