"""Headless O-RAN rApp boundary adapters.

This package deliberately has no dependency on the legacy OAI executor or
direct KPI collector.  All control and assurance enter through injected R1
ports.
"""

from .ports import AssuranceDecision, AssurancePort, PolicyDispatchPort

__all__ = ["AssuranceDecision", "AssurancePort", "PolicyDispatchPort"]
