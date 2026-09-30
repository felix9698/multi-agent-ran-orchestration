"""Upper Live-O1 Harness runtime (release ``upper-live-o1-harness/1.0.0``).

This package composes the upper's existing O-RAN components for SC-084 under the
``live-O1`` execution profile.  It produces no verdict: the oracle is the lower
frozen runner and lives in ``scenario-catalog.1.0.1.json``, which the capture
references by JSON pointer and never copies.

``oran/release/`` is a PEP 420 namespace directory, which is exactly why this
package can sit beside the byte-frozen ``oran/release/ubm/`` without a single
shared-file edit.
"""

from .capture import SCHEMA_ID, CaptureRecorder, ObservedClock, RawStore
from .config import (
    PROFILE_LIVE, PROFILE_SELF_TEST, Lo1StartupConfig, load_startup_config)
from .egress import EgressGuard, EgressViolation
from .gate import (
    CheckResult, GateRefused, GateResult, UnresolvedEntry,
    run_identity_authority_gate)

__all__ = [
    "CaptureRecorder",
    "CheckResult",
    "EgressGuard",
    "EgressViolation",
    "GateRefused",
    "GateResult",
    "Lo1StartupConfig",
    "ObservedClock",
    "PROFILE_LIVE",
    "PROFILE_SELF_TEST",
    "RawStore",
    "SCHEMA_ID",
    "UnresolvedEntry",
    "load_startup_config",
    "run_identity_authority_gate",
]
