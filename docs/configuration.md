# Configuration

AI Native loads configuration from `ainative.yaml` in the current repository (or nearest parent), unless you pass `--config` or set `AINATIVE_CONFIG`.

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
