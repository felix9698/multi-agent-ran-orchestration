"""The deterministic KPM / E2-control / readback boundary.

Task requirement 2 names this boundary as an upper function; the frozen
precedent puts it behind ``deployment.a1.apiRoot``, which
``permittedTargetKinds`` classes ``LOWER_OWNED_IMPLEMENTATION_UNDER_TEST``.
Both are honoured: the upper **ships** a deterministic stub so no live
substitute is ever needed, and **which side serves it** is an integration
authority assignment the gate refuses to guess.

``prohibitedTargetKinds`` bars ``LIVE_E2_CONTROL`` and ``LIVE_RAN_WRITE``
outright, and the capture schema pins ``/deterministicStubs/writeLedger/*/live``
to ``const false``: a live write is not merely refused here, it is
*unrepresentable* in the evidence.  An attempt would be refused by the egress
guard and would appear under ``/externalCalls/violations`` instead.
"""

from __future__ import annotations

import threading
from contextlib import nullcontext
from typing import Any, Mapping

from oran.contract.jcs import jcs_sha256
from oran.mocks.nearrt.producer import E2Stub

ROUTING_UPPER = "UPPER_DETERMINISTIC_STUB"
ROUTING_LOWER = "LOWER_IMPLEMENTATION_UNDER_TEST"


class DeterministicStubError(RuntimeError):
    """The deterministic boundary was driven outside its declared envelope."""


class DeterministicStubBoundary:
    """Deterministic KPM snapshots, control results and readbacks.

    Every observation is recorded with the JCS digest of its payload rather than
    the payload itself, so the lower runner can compare what crossed the
    boundary without this document carrying a second copy of it.
    """

    def __init__(self, *, routing: str, recorder: Any, clock: Any,
                 admission: Any = None) -> None:
        if routing not in (ROUTING_UPPER, ROUTING_LOWER):
            raise DeterministicStubError(
                "the deterministic stub routing must be an admitted assignment")
        self.routing = routing
        self.recorder = recorder
        self.clock = clock
        self._admission = admission
        self._lock = threading.RLock()
        self._stub = E2Stub()
        self._kpm: list[dict[str, Any]] = []
        self._control: list[dict[str, Any]] = []
        self._readback: list[dict[str, Any]] = []
        self._ledger: list[dict[str, Any]] = []
        self._normal_writes = 0
        self._rollback_writes = 0

    # -- boundary ---------------------------------------------------------
    def kpm_snapshot(self, *, step_id: str, snapshot: Mapping[str, Any]
                     ) -> dict[str, Any]:
        with self._admit("deterministic KPM snapshot"):
            payload = dict(snapshot)
            self._observe(self._kpm, step_id, payload)
            return payload

    def control(self, *, step_id: str, target: Mapping[str, Any],
                rollback: bool = False) -> dict[str, Any]:
        with self._admit("deterministic E2 control result"):
            result, effect = self._stub.control(dict(target), rollback)
            payload = {"result": result, "effectApplied": bool(effect),
                       "resultIsEffectEvidence": False}
            self._observe(self._control, step_id, payload)
            with self._lock:
                kind = "ROLLBACK" if rollback else "NORMAL"
                self._ledger.append({
                    "sequence": len(self._ledger),
                    "kind": kind,
                    # const false in the schema: the boundary is deterministic and
                    # in-process, so no RAN write ever leaves this host.
                    "live": False,
                    "at": self.clock.now(),
                    "targetRef": str(
                        target.get("cellId") or target.get("ref") or step_id),
                })
                if rollback:
                    self._rollback_writes += 1
                else:
                    self._normal_writes += 1
            return payload

    def readback(self, *, step_id: str, observed: Mapping[str, Any]
                 ) -> dict[str, Any]:
        with self._admit("deterministic control readback"):
            payload = dict(observed)
            self._observe(self._readback, step_id, payload)
            return payload

    def _admit(self, what: str):
        return nullcontext() if self._admission is None else self._admission(what)

    def _observe(self, sink: list[dict[str, Any]], step_id: str,
                 payload: Mapping[str, Any]) -> None:
        with self._lock:
            sink.append({
                "stepId": str(step_id),
                "sequence": self.recorder.next_sequence(),
                "at": self.clock.now(),
                "payloadJcsSha256": jcs_sha256(dict(payload)),
                "servedBy": self.routing,
            })

    # -- capture ----------------------------------------------------------
    def observations(self) -> dict[str, Any]:
        with self._lock:
            return {
                "routing": self.routing,
                "kpmSnapshots": [dict(item) for item in self._kpm],
                "controlResults": [dict(item) for item in self._control],
                "readbacks": [dict(item) for item in self._readback],
                "writeLedger": [dict(item) for item in self._ledger],
                "normalRanWrites": self._normal_writes,
                "rollbackRanWrites": self._rollback_writes,
            }

    def publish(self) -> None:
        self.recorder.set_deterministic_stubs(self.observations())
