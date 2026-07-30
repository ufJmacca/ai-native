"""Compatibility imports for stage capability definitions.

The dependency-free source of truth lives at :mod:`ai_native.workflow_stages`
so lower-level runtime modules do not need to import the handler package.
"""

from ai_native.workflow_stages import (
    CLI_STAGE_CHOICES,
    FACTORY_ELIGIBLE_STAGES,
    LEGACY_ORDERED_STAGES,
    PRE_SLICE_STAGES,
    PUBLICATION_STAGES,
    REVIEW_TARGET_STAGES,
    SLICE_PIPELINE_STAGES,
    SLICE_SPECIFIC_STAGES,
)

__all__ = [
    "CLI_STAGE_CHOICES",
    "FACTORY_ELIGIBLE_STAGES",
    "LEGACY_ORDERED_STAGES",
    "PRE_SLICE_STAGES",
    "PUBLICATION_STAGES",
    "REVIEW_TARGET_STAGES",
    "SLICE_PIPELINE_STAGES",
    "SLICE_SPECIFIC_STAGES",
]
