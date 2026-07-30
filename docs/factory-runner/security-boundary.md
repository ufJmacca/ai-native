# Factory runner security boundary

## Scope

This is the initial AN-00 threat analysis for the public runner only. It does
not claim that Python policy can contain hostile repository code. The private
factory must enforce the outer sandbox, filesystem mounts, network policy,
credentials, resource limits, and attempt lifecycle.

## Trust boundaries and assets

Untrusted inputs include repository contents, work-item text, context objects,
agent output, command output, and any resume artifact. Trusted inputs are
limited to a future schema-valid `RunSpec`, verified content digests, the
released runner build, and attempt-scoped capabilities supplied by the
factory.

Assets to protect are:

- model-gateway and publication credentials;
- source repositories and unrelated host files;
- protocol identities and digests;
- test and TDD evidence;
- change-set integrity;
- attempt isolation and bounded compute;
- the trusted publisher's authority.

The runner may modify only the prepared target workspace and write protocol
outputs only below the explicit output root. It never owns durable upload or
publication.

## Executable AN-00 invariants

`FactoryModeCapabilities` permits author and verify while denying:

- interactive input;
- commit;
- pull-request creation;
- push;
- merge.

Factory-eligible stages exclude the legacy `commit` and `pr` stages. Later
phases must narrow the stage set further from a validated input; they may not
widen this ceiling. AN-00 exposes no executable factory runner.

## Initial threats and required controls

| Threat | Existing exposure | Required runner control |
|---|---|---|
| Host credential discovery | Interactive configuration checks home and provider paths | Factory startup must bypass discovery and reject broad credential variables |
| Publication from an attempt | Legacy Git and PR stages can commit, push, and call GitHub | Factory dispatch must make those modules unreachable and disable remotes, hooks, helpers, and prompts |
| Interactive blocking or guessed answers | CLI callbacks use `input()` and `getpass()` | All decisions come from validated input; missing information returns `blocked` |
| Repository prompt injection | Repository text is passed to agents | Treat content as data; immutable policy and capabilities cannot be broadened by instructions |
| Environment leakage | External commands inherit `os.environ` | Build child environments from an explicit allowlist |
| Excess agent authority | Interactive adapters may use broad tool permissions | Factory adapter uses an attempt-scoped gateway and factory-specific permission profile |
| Undeclared egress | Registry, telemetry, agents, or repository commands may contact remote services | Disable runner-managed remote endpoints; outer sandbox enforces an allowlisted network profile |
| Path traversal or symlink escape | Target and artifact paths are repository-controlled | Resolve roots, reject traversal/special files/symlink escapes, and atomically write bounded output |
| Secret persistence | Logs, patches, events, checkpoints, and agent output may contain secrets | Redact and scan before every durable write; fail finalisation on contamination |
| Tampered context or resume data | Local files may be replaced or crossed between attempts | Verify every digest and identity before use; restore transactionally and fail closed |
| Evidence spoofing | Unrelated failures can look like red TDD evidence | Classify expected behavioral failure and distinguish authoring from clean verification |
| Resource exhaustion | Agents, commands, logs, and patches may be unbounded | Enforce wall-time, turn, token, command, and output budgets |
| Docker-socket or host mount access | Interactive devcontainer may mount both | Factory image and sandbox must not receive them |

## Verification responsibilities

AI Native provides deterministic validation, permission checks, local output
safety, redaction, and protocol evidence. The private factory independently
provides:

- ephemeral sandbox creation and destruction;
- authoring/verification isolation;
- credential brokering;
- network and process containment;
- durable object acknowledgement;
- sterile patch application and publication.

Both layers must fail closed. Runner checks are defence in depth, not a
substitute for the sandbox.

