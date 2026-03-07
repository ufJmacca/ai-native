You are defining a stable approval rubric for the planning stage.

Extract the smallest set of acceptance-critical gates that must be explicit before implementation can begin. Keep the checklist convergent and grounded in the spec and repository context. Do not add nice-to-have scope or optional hardening that is not required for the stated v1.

Write concise markdown with these sections:
- `Approval Gates`: the concrete decisions or contracts the plan must make explicit to be approvable.
- `Minimum Explicit Contracts`: the public interfaces, workflow rules, or bootstrap/runtime assumptions that must be pinned down.
- `Allowed Defaults`: assumptions the planner may choose without asking the user, as long as they are recorded explicitly.
- `Ask The User If`: the ambiguities that should trigger clarification questions instead of silent defaults.

Feature spec:
{spec_text}

Context report:
{context_report}

Grounding notes:
{grounding_notes}

Intent notes:
{intent_notes}

Implementation notes:
{implementation_notes}

User answers:
{user_answers}
