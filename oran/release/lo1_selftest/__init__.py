"""Upper-live-O1-harness SELFTEST package: Provider emulator, driver, verifier.

Standing (binding, per ``docs/upper-live-o1-harness/DESIGN.md`` §10 and
``emulator-boundary.1.0.0.json``):

* This package is **upper-readiness evidence only**.  It is never SC-084
  acceptance, never a bilateral disposition and never grounds for any of the
  claims enumerated by digest in ``release-gates.1.0.0.json``.
* The single permitted state label emitted from here is
  ``UPPER_LIVE_O1_HARNESS_SELF_TEST``.
* Nothing here may import ``oran.release.lo1`` (the runtime under test), nor
  ``oran.release.ubm`` / ``oran.release.ubm_selftest`` (the frozen bilateral
  release).  The driver speaks to a separately spawned process over a public
  network boundary and carries a *structural mirror* of the runtime's public
  types, exactly as ``work-split.1.0.0.json#/owners/W3-SELFTEST/importRule``
  freezes it.
* The emulator is driven exclusively from frozen bundle bytes.  A NETCONF
  request that is not byte-equal to a registered golden fixture is answered
  with an ``rpc-error`` and counted as an unknown route; a PM value equal to a
  golden sample value is a ``golden_value_collision`` and fails the run,
  because ``RULE-O1-LIVE-VALUE-INVARIANTS`` forbids that equality for live
  input.

``oran/release`` intentionally has no ``__init__.py``: it is a PEP 420
namespace directory, which is what lets this package sit beside the frozen
bilateral release with zero shared-file edits.
"""

from __future__ import annotations

#: The only state label any artefact produced by this package may carry.
SELF_TEST_STATE_LABEL = "UPPER_LIVE_O1_HARNESS_SELF_TEST"

#: Provider kind this package can ever present itself as.
EMULATOR_KIND = "CONTRACT_FAITHFUL_EMULATOR"

NOT_SC084_ACCEPTANCE = (
    "Upper-side readiness evidence only.  Disposition for SC-084 is owned by "
    "the lower conformance runner under the live-O1 integration authority; "
    "this suite never grants it and never claims it."
)

#: ``sysexits.h`` codes this package uses so that a failure is a real,
#: observable, non-zero process exit rather than a comment.
EXIT_OK = 0
EXIT_FINDING = 65        # EX_DATAERR   -- evidence defect found by adjudication
EXIT_RUNTIME_ABSENT = 69  # EX_UNAVAILABLE -- the runtime under test is not present
EXIT_INTERNAL = 70       # EX_SOFTWARE  -- self-test internal failure
EXIT_REFUSED = 78        # EX_CONFIG    -- admission refused, fail-closed

__all__ = [
    "SELF_TEST_STATE_LABEL",
    "EMULATOR_KIND",
    "NOT_SC084_ACCEPTANCE",
    "EXIT_OK",
    "EXIT_FINDING",
    "EXIT_RUNTIME_ABSENT",
    "EXIT_INTERNAL",
    "EXIT_REFUSED",
]
