"""Hardware-free slice PRB actuation chain.

This package owns the project-side A1 binding, E2SM-RC request model, worker,
and effect-evidence contract.  It does not make a live-deployment claim.
"""

from .a1 import POLICY_TYPE_ID

__all__ = ["POLICY_TYPE_ID"]
