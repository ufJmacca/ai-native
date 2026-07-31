# AN-02 attempt-scoped model-gateway contract

## Scope

This document defines the private factory-to-runner model-gateway boundary
implemented in AN-02. The gateway is available only to `factory run`
authoring stages. `factory verify` is deterministic and never constructs an
agent or forwards the gateway capability. The trusted launcher must not mount
or configure a gateway credential for a verify-only attempt.

This is a process and file contract, not a publication or general network
capability. The private factory remains responsible for the attempt sandbox,
the network allowlist, the credential mount, and credential revocation.

## Bootstrap configuration

The trusted attempt launcher may set:

| Environment key | Contract |
|---|---|
| `AINATIVE_FACTORY_AGENT_COMMAND_JSON` | A JSON array containing one or more non-empty, NUL-free string arguments. It is runner bootstrap configuration and is not a shell command. |
| `ATTEMPT_GATEWAY_TOKEN_FILE` | Optional absolute path to the attempt-scoped gateway credential file. It is reserved for the gateway child and is never admitted as a project-command environment key. |

The runner parses `AINATIVE_FACTORY_AGENT_COMMAND_JSON` without shell
expansion. It resolves the executable through the runner-controlled
`PATH`—or validates an absolute executable—and rejects an executable inside
the mutable workspace or protocol-output root. The child is then started with
`shell=False`, closed file descriptors, and standard input connected to
`/dev/null`.

`AINATIVE_FACTORY_AGENT_COMMAND_JSON` is consumed by the runner and is not
forwarded merely because it was present in the launcher environment.

## Credential lifecycle

When a gateway credential is required, the private factory:

1. creates a credential unique to one run attempt;
2. exposes it through a file mounted only into that attempt sandbox;
3. sets `ATTEMPT_GATEWAY_TOKEN_FILE` to the mounted path;
4. grants the credential only the model-gateway capability required by the
   selected profile; and
5. revokes the credential and destroys the mount when the attempt ends.

The credential value is never a CLI argument or an environment value. At
attempt startup, the runner reads the bounded regular credential file through
a no-follow descriptor only to seed its in-memory exact-value secret scanner.
It passes only the file path to gateway children and never logs, checkpoints,
or writes the credential value into protocol output.
Deterministic project commands do not receive
`ATTEMPT_GATEWAY_TOKEN_FILE`. Admission rejects a `RunSpec` that tries to add
that reserved key to `policy.allowed_environment_keys`.

Broad ambient credentials—including GitHub, SSH-agent, cloud-provider,
package-registry, AI Native registry, telemetry, and direct model-provider
keys—cause factory admission to fail even when a repository does not request
them. The gateway-file exception does not weaken that rule.

## Per-call file contract

For every model call, the runner creates a new private temporary directory and
sets these child variables:

| Environment key | Direction | Contract |
|---|---|---|
| `AINATIVE_PROMPT_FILE` | runner → gateway | Path to the UTF-8 prompt written for this call. |
| `AINATIVE_OUTPUT_FILE` | gateway → runner | Path at which the gateway must write one non-empty UTF-8 regular file before exiting successfully. A symlink or missing file is rejected. |
| `AINATIVE_MODEL_PROFILE` | runner → gateway | The admitted `RunSpec.policy.model_profile`. |
| `AINATIVE_SCHEMA_FILE` | runner → gateway | Present only when the stage requests structured output; otherwise the runner removes any stale value. |
| `ATTEMPT_GATEWAY_TOKEN_FILE` | factory → gateway | Present only when supplied by the trusted attempt launcher. It contains a path, never the token value. |

`AINATIVE_IMAGE_PATHS` is always absent. AN-02 factory mode does not support
image inputs.

When `AINATIVE_SCHEMA_FILE` is present, the gateway must return JSON through
`AINATIVE_OUTPUT_FILE`. The adapter parses that JSON before returning it to the
stage; invalid JSON fails with a safe gateway error. Schema-specific stage
validation remains downstream of this file handoff.

The prompt and response files are private scratch data, not protocol
artifacts. The per-call directory is deleted after the call, and the complete
runner-private environment root is deleted when the invocation ends.

## Child environment and mutable state

The gateway receives only:

- admitted keys from `policy.allowed_environment_keys`;
- the reserved gateway-token file path, when present;
- runner-controlled sterile `HOME`, temporary, XDG, Git, authentication,
  pager, and executable-path settings; and
- the per-call file variables above.

It does not inherit the launcher environment. The runner uses a sterile home,
disables user Python packages, Git credential helpers, hooks, external diffs,
SSH prompting, and interactive Git authentication.

The gateway may author within the prepared workspace and use its designated
runner-private mutable directories. Before and after every call, the runner
binds protected runner-owned state. It also snapshots the protocol-output
tree, repairs an attempted mutation where possible, and fails closed. Mutable
scratch roots reject symbolic links and special files and are subject to
entry and byte limits.

These are runner-level checks around the process it directly launches. They
do not make Python capable of containing hostile repository code. A gateway
or admitted test process can itself try to spawn a nested executable. The
private factory's outer sandbox must therefore enforce filesystem mounts,
process and network isolation, and the model-gateway-only egress rule for the
entire descendant tree.

## Turns, model tokens, deadline, and cancellation

All authoring roles share one `max_agent_turns` counter. The runner increments
it before each gateway launch and refuses a launch after the admitted limit is
reached.

AN-02's enforceable `max_model_tokens` measure is deterministic, not
provider-reported billing usage. For each prompt and returned response the
runner counts:

```text
max(1, ceil(UTF-8 byte length / 4))
```

Prompt cost is checked before launch; response cost is added immediately after
the call. Exceeding the shared limit fails the author workflow. Because the
AN-02 response file has no provider-usage field, the gateway and private
factory must apply the same or a stricter provider-side quota when exact
provider token accounting is required.

One absolute monotonic `max_wall_seconds` deadline covers the complete
post-admission invocation, including repository validation, every gateway
call, deterministic verification, and runner-owned Git inspection. A model
call cannot reset that deadline. Each child is constrained by the smaller of
its invocation timeout and the remaining shared deadline.

`SIGTERM` sets the shared cancellation token. On cancellation or timeout the
runner terminates the child process group, waits only the bounded grace
period, escalates to a kill, and on Linux adopts and reaps detached
descendants before returning a terminal result.

## Logging and failure disclosure

Runner progress and safe failure summaries go to standard error. Standard
output remains empty unless `structured-events` is negotiated and the RunSpec
requests streaming; in that case it contains only the exact canonical NDJSON
bytes also committed to `events.ndjson`.

Gateway standard output and standard error are captured only to bounded
in-memory buffers for process supervision. They are not returned as an
`AgentResult`, copied into protocol artifacts, or included in runner log
messages. A gateway must use `AINATIVE_OUTPUT_FILE`; writing a response only
to standard output is treated as missing output.

Gateway failures disclose only a fixed safe category, such as timeout,
cancellation, missing or invalid output, or a numeric exit code. They do not
echo the command configuration, prompt, response, child output, environment,
credential path, or credential value.
