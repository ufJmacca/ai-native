You are deciding whether the planning workflow should ask the user a small number of clarification questions.

Only ask questions if they unblock acceptance-critical planning decisions that cannot be resolved safely from the target repository and spec. Prefer fewer questions, not zero at all costs. Ask at most {max_questions} questions.
Ask when a materially different answer would change public interfaces, workflow semantics, data model, auth/bootstrap assumptions, runtime/deployment posture, or v1 scope.
If the spec or context exposes a contradiction that would otherwise force the planner to guess between multiple reasonable product contracts, ask instead of silently picking one.
Use the context report's recommended questions when they are still relevant.

Return JSON that matches the provided schema.

Feature spec:
{spec_text}

Context report:
{context_report}

Grounding notes:
{grounding_notes}
