"""Deployment validation, canary, and recovery evidence helpers.

These modules deliberately avoid provider credentials and tenant payloads.  They turn the
production runbook into executable, fail-closed checks; passing them is repository evidence only,
not proof that a managed service or human launch gate has been approved.
"""

from .canary import CanaryOptions, CanaryReport, run_canary
from .config_validation import ProductionConfigReport, validate_production_config

__all__ = [
    "CanaryOptions",
    "CanaryReport",
    "ProductionConfigReport",
    "run_canary",
    "validate_production_config",
]
