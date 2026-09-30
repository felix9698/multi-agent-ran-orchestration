"""Upper-bilateral-mock SELFTEST package (external black-box driver + lower double).

Scope and standing (binding, per ``docs/upper-bilateral-mock/DESIGN.md`` §11):

* This package is **upper-readiness evidence only**.  It is *never* bilateral
  acceptance: the oracle for the four bilateral scenarios belongs to the lower
  frozen runner at commit ``51d73ca098743b25fe074d184904e695af37fd95``.
* The single permitted state label emitted by this package is
  ``UPPER_ARTIFACT_SELF_TEST``.
* Nothing here may import ``oran.release.ubm`` (the upper runtime under test).
  The driver only speaks HTTPS to a separately spawned upper process.
* ``ContractFaithfulLowerDouble`` is driven exclusively from the frozen bytes of
  ``scenario-catalog.1.0.1.json`` and ``scenario-runner-contract.1.0.1.json``.
  A route or operation that is not derivable from those bytes is a hard failure,
  never an ignored or guessed one.

``oran/release`` intentionally has no ``__init__.py``: it is a PEP 420 namespace
directory so that the RUNTIME executor (``oran/release/ubm/**``) and this
package can be authored independently without contending for a shared file.
"""

from __future__ import annotations

SELF_TEST_STATE_LABEL = "UPPER_ARTIFACT_SELF_TEST"
NOT_BILATERAL_ACCEPTANCE = (
    "Upper-side readiness evidence only. Disposition for the four bilateral "
    "scenarios is owned by the lower frozen runner; this suite never grants it."
)

__all__ = ["SELF_TEST_STATE_LABEL", "NOT_BILATERAL_ACCEPTANCE"]
