# Prompt Library

The prompt library is designed to keep the workflow deterministic enough for orchestration while still giving the model room to reason.

## Design Rules

- Builder prompts generate structured JSON when a schema exists.
- Critic prompts return a structured verdict and actionable findings.
- Loop prompts are allowed to mutate the repository because they are the implementation stage.
- Recon prompts receive a repository scan summary so the model does not waste budget rediscovering local facts.
- Test critique prompts explicitly reject test theatre, dead mocks, and assertions that do not prove behavior.

## Files

- `ai_native/prompts/recon.md`
- `ai_native/prompts/plan_phase_grounding.md`
- `ai_native/prompts/plan_phase_intent.md`
- `ai_native/prompts/plan_phase_implementation.md`
- `ai_native/prompts/plan.md`
- `ai_native/prompts/plan_review.md`
- `ai_native/prompts/architecture.md`
- `ai_native/prompts/architecture_review.md`
- `ai_native/prompts/prd.md`
- `ai_native/prompts/prd_review.md`
- `ai_native/prompts/slice.md`
- `ai_native/prompts/loop.md`
- `ai_native/prompts/test_review.md`
- `ai_native/prompts/verify.md`
- `ai_native/prompts/pr_review.md`
