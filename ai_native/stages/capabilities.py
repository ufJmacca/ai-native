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

PUBLICATION_STAGES = frozenset({"commit", "pr"})
FACTORY_ELIGIBLE_STAGES = frozenset(
    stage for stage in LEGACY_ORDERED_STAGES if stage not in PUBLICATION_STAGES
)

