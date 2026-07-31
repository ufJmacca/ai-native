# Factory runner security boundary

## Scope

This is the public-runner threat analysis updated through AN-03. It does not
claim that Python policy can contain hostile repository code. The private
factory must enforce the outer sandbox, filesystem mounts, network policy,
credentials, resource limits, and attempt lifecycle.

## Trust boundaries and assets

Untrusted inputs include repository contents, work-item text, context objects,
agent output, command output, and any resume artifact. Trusted inputs are
limited to schema-valid protocol documents whose identities and content
digests have been verified, the released runner build, and attempt-scoped
capabilities supplied by the factory. Resume input is admitted only after its
checkpoint contract, self digest, artifact bytes, run/repository identity,
operation, capability requirements, original authority, and remaining
budgets validate; restoration is transactional.

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

## Executable runner invariants

`FactoryModeCapabilities` permits author and verify while denying:

- interactive input;
- commit;
- pull-request creation;
- push;
- merge.

Factory-eligible stages exclude the legacy `commit` and `pr` stages. AN-02
narrows that ceiling from the validated `RunSpec`, directly dispatches only
the admitted authoring handlers, and gives clean verify mode no agent adapter.

AN-02 also:

- rejects broad ambient credentials and reserves the gateway token for the
  gateway child only;
- requires a repository with no configured remotes, external Git directory,
  linked worktree, or writable-path symlink topology;
- forces sterile home, temporary, XDG, Git helper, hook, prompt, protocol, and
  executable-path settings;
- permits only exact declared commands and a narrow read-only Git subcommand
  set, with trusted executable resolution outside mutable roots;
- supervises gateway children, deterministic commands, and post-admission
  runner-owned Git through one bounded process runner with closed standard
  input and a shared deadline;
- binds Git configuration, hooks, refs, index, and worktree metadata at every
  author and verification boundary;
- terminates process groups and, on Linux, adopts and reaps detached
  descendants before returning success;
- requires a fresh output root and uses descriptor-relative, no-follow atomic
  writes that never overwrite an existing artifact;
- protects runner-owned output state while gateway and deterministic-command
  children are active, repairing attempted output mutation where possible
  before failing closed; and
- validates referenced ChangeSet artifacts, prepared file content/modes,
  allowed paths, and canonical patch bytes before clean verification.

AN-03 additionally:

- scans every durable artifact for exact and built-in secret canaries and
  keeps gateway credentials outside durable runner state;
- stages and fsyncs each canonical event line without exposing a valid event
  artifact before atomic finalisation;
- writes immutable content-addressed checkpoints at configured stage and TDD
  boundaries, including tokenised portable private author state and complete
  phase evidence;
- verifies checkpoint lineage and restores repository state transactionally
  without widening path, command, stage, environment, network, credential,
  model, or resource authority;
- classifies genuine red behavioral failures separately from infrastructure,
  dependency, collection, syntax, timeout, and unrelated failures;
- emits deterministic complete ChangeSets and distinguishes authoring
  evidence from clean-verification evidence; and
- validates the acyclic protocol manifest and writes the completion marker
  last, so a missing marker denotes an interrupted attempt.

The exact gateway environment, file, credential, budget, deadline, and
logging rules are recorded in the
[AN-02 attempt-scoped model-gateway contract](gateway-contract.md).

The executable and environment checks above apply to commands directly
admitted and launched by the runner. An admitted Python, test, or gateway
process can attempt to spawn a nested executable through an absolute path.
Python-level policy cannot reliably interpose on every descendant or contain
hostile repository code. No-publication assurance therefore combines the
runner's lack of remotes and publication credentials with the private
factory's outer filesystem, process, and network sandbox.

## Initial threats and required controls

| Threat | Existing exposure | Required runner control |
|---|---|---|
| Host credential discovery | Interactive configuration checks home and provider paths | Factory startup must bypass discovery and reject broad credential variables |
| Publication from an attempt | Legacy Git and PR stages can commit, push, and call GitHub; an admitted child can spawn nested tools | Factory dispatch makes publication modules unreachable and disables remotes, hooks, helpers, prompts, and credentials; the outer sandbox denies descendant egress |
| Interactive blocking or guessed answers | CLI callbacks use `input()` and `getpass()` | All decisions come from validated input; missing information returns `blocked` |
| Repository prompt injection | Repository text is passed to agents | Treat content as data; immutable policy and capabilities cannot be broadened by instructions |
| Environment leakage | External commands inherit `os.environ` | Build child environments from an explicit allowlist |
| Excess agent authority | Interactive adapters may use broad tool permissions | Factory adapter uses an attempt-scoped gateway and factory-specific permission profile |
| Undeclared egress | Registry, telemetry, agents, or repository commands may contact remote services | Disable runner-managed remote endpoints; outer sandbox enforces an allowlisted network profile |
| Path traversal or symlink escape | Target and artifact paths are repository-controlled | Resolve roots, reject traversal/special files/symlink escapes, and atomically write bounded output |
| Secret persistence | Logs, patches, events, checkpoints, and agent output may contain secrets | Do not persist gateway child output; scan every durable write and fail finalisation on contamination |
| Tampered context or resume data | Local files may be replaced or crossed between attempts | Verify every admitted digest, artifact, identity, lineage, and authority field before transactional restore |
| Evidence spoofing | Unrelated failures can look like red TDD evidence | Classify expected behavioral failure and distinguish authoring from clean verification |
| Resource exhaustion | Agents, commands, logs, and patches may be unbounded | Enforce wall-time, turn, token, command, and output budgets |
| Docker-socket or host mount access | Interactive devcontainer may mount both | Factory image and sandbox must not receive them |

## Verification responsibilities

Through AN-03, the AI Native runner is responsible for deterministic
validation, permission checks, local output safety, redaction and secret
scanning, portable checkpoints, complete evidence and ChangeSets, and the
content-addressed terminal protocol chain. The private factory independently
provides:

- ephemeral sandbox creation and destruction;
- authoring/verification isolation;
- credential brokering;
- network and process containment;
- durable object acknowledgement;
- sterile patch application and publication.

Both layers must fail closed. Runner checks are defence in depth, not a
substitute for the sandbox.
