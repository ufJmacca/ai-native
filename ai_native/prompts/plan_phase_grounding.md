You are running the planning stage in a Plan-Mode-style subworkflow.

Phase 1: ground in the local reality before making product or implementation assumptions.

Instructions:
- Use the feature spec and context report.
- Summarize repo facts, current state, likely constraints, and any ambiguities that remain after exploration.
- Use the target repository as the source of truth. Consider application code, infrastructure, CI, docs, and configuration when they materially affect the feature.
- Do not ask the user questions in this step. If something is unclear, mark it as an ambiguity to resolve in the next phase.
- Write concise markdown with sections for `Ground Truth`, `Constraints`, and `Ambiguities`.

Feature spec:
{spec_text}

Context report:
{context_report}
