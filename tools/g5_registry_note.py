"""What Gate 5 prepared in stage 1, and what stage 2 then earned.

The registry transition a Gate 5 run makes is
``HARDWARE_FREE_VERIFIED -> OTA_VERIFICATION_PENDING -> OTA_LIVE_VERIFIED``.
Stage 1 could honestly prepare only the first hop: an ``OTA_LIVE_VERIFIED``
record needs retained evidence references and a submittable deployment, and on
the night of stage 1 there was no OTA evidence because there had been no OTA
run.  Both refusals are asserted in
``tests/assurance/test_g5_objective_gate.py`` rather than described.

Stage 2 (2026-08-25) ran them.  ``TrafficSteeringPreference`` and
``UELevelTarget`` each reached ``SETTLED_SUCCESS`` / ``CLOSED_PASS`` on real
radio and each produced a recorded non-success with the full reverse-rollback
recovery, so both records now carry ``OTA_LIVE_VERIFIED`` with the run,
event-stream, attribution and gNB-log references the validator demands --
``docs/integration/evidence/GATE5-OTA-RECORD.md``.

A later stage (2026-08-28) took ``QoSTarget`` and ``QoSandTSP`` the same way,
after the gNB2 O1 export wiring was fixed and the E2 epochs were re-pinned to
211 / 209.  They are deliberately **not** added to the map below: stage 1
examined them and declined to prepare them, and back-dating that decision would
falsify the one thing this module exists to record.  Their transition is in the
registry with its own references.

This module keeps the *prepared* map as the historical statement of what stage 1
could claim.  It is not the registry: the registry is
``assurance/objectives/registry.py``, and it is what moved.
"""

from __future__ import annotations

from typing import Mapping

from assurance.objectives import SupportState

#: The families whose actuator (``AIC_UECellSteering_1.0.0``) and whose evidence
#: counter (the KPM Style-4 per-UE attribution) this deployment demonstrably
#: delivers -- the same pair Gate 3 drove OTA end to end.  Listed as *pending*,
#: which was the honest state for "hardware-free verified, no OTA run yet".
#: Stage 2 carried both the rest of the way; the registry, not this map, is
#: where that is recorded.
PREPARED_TRANSITIONS: Mapping[str, SupportState] = {
    "TrafficSteeringPreference": SupportState.OTA_VERIFICATION_PENDING,
    "UELevelTarget": SupportState.OTA_VERIFICATION_PENDING,
}

__all__ = ["PREPARED_TRANSITIONS"]
