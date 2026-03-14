# Self-Hosted Runtime Security Controls

This guide defines the baseline security controls for operators running a self-hosted runtime with ingest/read APIs.

## 1) Authentication Modes (Ingest + Read APIs)

Support the following auth modes via explicit configuration:

- **`api_key`**: requests include an API key header (for example `X-API-Key`).
- **`bearer_jwt`**: requests include `Authorization: Bearer <token>` and token/JWT validation is enforced.
- **`none`**: optional no-auth mode for local development only.

### Required enforcement

- Default to **authenticated** mode in all non-local environments.
- Fail closed on auth configuration errors.
- Reject mixed or ambiguous auth configuration.
- Emit audit log events for allow/deny outcomes.

### Example config shape

```yaml
auth:
  mode: bearer_jwt # api_key | bearer_jwt | none
  allow_no_auth_local_only: true
  api_key:
    header_name: X-API-Key
    keys_env_var: RUNTIME_API_KEYS
  jwt:
    issuer: https://issuer.example.com/
    audience: runtime-api
    jwks_url: https://issuer.example.com/.well-known/jwks.json
```

## 2) Tenant and Project Isolation

When multi-tenant operation is enabled, enforce tenant/project isolation as part of request authorization and data access.

### Required controls

- Bind every read/write request to an effective `(tenant_id, project_id)` scope.
- Deny cross-tenant and cross-project access by default.
- Require tenant/project identifiers to pass strict format validation.
- Ensure storage queries and indexes include tenant/project scope.
- Record effective scope in audit events.

### Example config shape

```yaml
isolation:
  enabled: true
  require_tenant_scope: true
  require_project_scope: true
  tenant_id_source: token_claim_or_header
  project_id_source: token_claim_or_header
```

## 3) Request Validation, Size Limits, and Audit Logging

### Request validation

- Validate schema, required fields, and data types for ingest and read endpoints.
- Reject malformed tenant/project identifiers and unsupported filters.
- Return deterministic 4xx responses for client input errors.

### Payload size limits

- Enforce request body size limits at both reverse proxy and application layers.
- Cap maximum item count/chunk count per ingest request.
- Reject oversize payloads with clear, non-sensitive error messages.

### Audit logs (reads and writes)

Log all read and write operations with:

- Timestamp and request ID.
- Caller identity (subject, API key ID, or `local-dev-no-auth`).
- Tenant/project scope.
- Operation and target resource.
- Decision/outcome: allow, deny, validation_error, execution_error.

Do **not** log raw secrets or full sensitive payload fields.

## 4) Secret Handling

### Required guidance

- Use environment variables or managed secret stores (Vault, cloud secret manager, Kubernetes secrets).
- Never hardcode secrets in source control.
- Never return secrets in API responses, debug payloads, or UI surfaces.
- Redact sensitive payload fields before persistence and long-term logging.

### Recommended redaction policy

Treat the following as sensitive by default unless explicitly overridden:

- Access tokens, refresh tokens, API keys.
- Passwords, private keys, connection strings.
- PII-bearing free text fields where applicable.

Persist only redacted values (or hash/token references) when auditability requires field presence.

## 5) Recommended Production Topology

Use a defense-in-depth deployment layout:

1. **TLS termination + reverse proxy**
   - Terminate TLS at ingress/reverse proxy.
   - Enforce HTTPS and modern TLS settings.
   - Apply request body limits, rate limits, and basic WAF rules.
2. **Runtime service tier**
   - Place runtime in a private network segment.
   - Accept traffic only from trusted ingress/proxy.
   - Restrict egress to approved dependencies.
3. **Data tier**
   - Use least-privilege DB credentials scoped to runtime needs.
   - Separate credentials by environment and rotate periodically.
4. **Operations controls**
   - Centralize audit logs and protect retention/immutability as required.
   - Run encrypted backups and test restore procedures.
   - Define data retention and deletion windows aligned with policy/compliance.

## Operator Checklist

- [ ] Auth mode is explicitly set (`api_key` or `bearer_jwt`) for production.
- [ ] No-auth mode is disabled outside local development.
- [ ] Tenant/project isolation is enabled where required by workload.
- [ ] Request validation and payload limits are enabled and tested.
- [ ] Read/write audit logs are emitted and centralized.
- [ ] Secret sources are externalized; no secrets are returned by APIs.
- [ ] Sensitive fields are redacted before persistence.
- [ ] TLS, reverse proxy limits, DB least privilege, backup, and retention controls are in place.
