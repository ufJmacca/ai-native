# Configuration

AI Native loads configuration from `ainative.yaml` in the current repository (or nearest parent), unless you pass `--config` or set `AINATIVE_CONFIG`.

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
