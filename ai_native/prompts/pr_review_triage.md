You are triaging a raw pull request review for an ai-native workflow.

Convert the review into the required ReviewReport JSON schema. The raw PR reviewer remains responsible for inspecting the actual diff; your job is to decide whether the workflow must repair anything before opening the PR.

Mark `verdict` as `changes_required` only when the review identifies actionable correctness, regression, security, data-loss, CI, configuration, documentation accuracy, or test-quality issues that should block PR creation.

Do not require changes for praise, uncertainty without a concrete failure mode, broad style preferences, optional refactors, future enhancements, or comments that are already resolved by the PR body/slice context. Keep `required_changes` specific enough for a builder to fix.

Slice:
{slice_definition}

PR body draft:
{pr_body}

PRD:
{prd}

Raw PR review markdown:
{raw_review}

Prior PR critique history:
{critique_history}

PR blocker ledger:
{blocker_ledger}

Return only JSON matching the ReviewReport schema.
