from __future__ import annotations

LEGACY_ORDERED_STAGES = (
    "intake",
    "recon",
    "plan",
    "architecture",
    "prd",
    "slice",
    "loop",
    "verify",
    "commit",
    "pr",
)

PRE_SLICE_STAGES = LEGACY_ORDERED_STAGES[:6]
SLICE_PIPELINE_STAGES = LEGACY_ORDERED_STAGES[6:]
SLICE_SPECIFIC_STAGES = frozenset(SLICE_PIPELINE_STAGES)
CLI_STAGE_CHOICES = LEGACY_ORDERED_STAGES[2:]
REVIEW_TARGET_STAGES = (
    "plan",
    "architecture",
    "prd",
    "slice",
    "verify",
    "pr",
)
PUBLICATION_STAGES = frozenset({"commit", "pr"})
FACTORY_ELIGIBLE_STAGES = frozenset(
    stage for stage in LEGACY_ORDERED_STAGES if stage not in PUBLICATION_STAGES
)
