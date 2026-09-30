"""The real Coordinator execution path.

SC-084's ``real-coordinator`` step is ``COORDINATOR_PROCESS_INTENT`` and the
scenario's own expectation declares ``requiresRealCoordinatorExecution``.  The
only way this boundary moves the FSM is by calling the production
``RAppCoordinatorAdapter.process_intent``; the synthetic
``harness_transition`` path is refused by name, and any synthetic transition
that reached the adapter anyway is *counted* and surfaces as
``coordinator.syntheticTransitionCount``, which LO1-ST-N01 requires to be zero.

The evidence records handed to ``process_intent`` are the live normalized
records W1 produced.  Their ``recordJcsSha256`` values are recorded in
``coordinator.evidenceRecordsSuppliedFromNormalization`` so the lower runner can
prove the Coordinator consumed the live records and not a substitute.
"""

from __future__ import annotations

import copy
import json
import threading
from contextlib import nullcontext
from datetime import datetime
from typing import Any, Mapping

from oran.contract.jcs import jcs_sha256


class CoordinatorBoundaryError(RuntimeError):
    """The Coordinator boundary was driven outside its declared envelope."""


SYNTHETIC_OP = "COORDINATOR_TRANSITION"


class RealCoordinatorBoundary:
    """Run the production ``process_intent`` and record what it actually did."""

    def __init__(self, *, adapter: Any, recorder: Any,
                 requires_real_execution: bool = True,
                 admission: Any = None) -> None:
        self.adapter = adapter
        self.recorder = recorder
        self.requires_real_execution = bool(requires_real_execution)
        self._admission = admission
        self._lock = threading.RLock()
        self._synthetic_attempts = 0

    # -- refused by name --------------------------------------------------
    def refuse_synthetic_transition(self) -> None:
        """A synthetic transition is never evidence that the Coordinator ran."""
        with self._lock:
            self._synthetic_attempts += 1
        raise CoordinatorBoundaryError(
            "%s is denied on this profile: a synthetic transition is never "
            "accepted as evidence that the Coordinator executed" % SYNTHETIC_OP)

    @property
    def synthetic_transition_count(self) -> int:
        """Attempts refused here PLUS any synthetic origin the adapter holds."""
        observed = [event for event in self.adapter.transition_snapshot()
                    if str(event.get("origin")) != "REAL"]
        with self._lock:
            return self._synthetic_attempts + len(observed)

    # -- the real path ----------------------------------------------------
    def process_intent(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        context = (nullcontext() if self._admission is None
                   else self._admission("processing an intent in the Coordinator"))
        with context:
            return self._process_intent_admitted(arguments)

    def _process_intent_admitted(self, arguments: Mapping[str, Any]
                                 ) -> dict[str, Any]:
        from oran.rapp.headless import _context, _typed_intent

        before = int(self.adapter.harness_observations()["processIntentCalls"])
        now_input = str(arguments["now"])
        instant = datetime.fromisoformat(now_input.replace("Z", "+00:00"))
        records = json.loads(json.dumps(list(arguments["evidenceRecords"])))
        policy_id = arguments.get("policyId")
        for record in records:
            if policy_id is not None and isinstance(record.get("correlation"), dict):
                record["correlation"]["policyId"] = policy_id
        supplied = [jcs_sha256(record) for record in records]
        result = self.adapter.process_intent(
            _typed_intent(arguments["intent"]),
            context=_context(arguments["policyContext"]),
            evidence_records=records,
            now=instant,
            identifiers=dict(arguments["identifiers"]))
        observed = self.adapter.harness_observations()
        history = [
            {"from": str(event["from"]), "to": str(event["to"]),
             "outcome": event.get("outcome"), "origin": "REAL"}
            for event in observed["coordinatorFsmHistory"]
            if str(event.get("origin")) == "REAL"]
        terminal = [event for event in history if event["to"] == "S6"]
        after = int(observed["processIntentCalls"])
        self.recorder.set_coordinator({
            "processIntentCallsBefore": before,
            "processIntentCallsAfter": after,
            "processIntentCallsDelta": after - before,
            "executionMode": "REAL_PROCESS_INTENT",
            "fsmHistory": history,
            "terminalOutcome": result.get("terminal_outcome"),
            "terminalOutcomeCount": len(terminal),
            "terminalEvidenceRef": result.get("evidence_record_id"),
            "ledgerReferences": list(observed["coordinatorLedgerReferences"]),
            "requiresRealCoordinatorExecution": self.requires_real_execution,
            "syntheticTransitionCount": self.synthetic_transition_count,
            "evidenceRecordsSuppliedFromNormalization": supplied,
            "nowInput": now_input,
        })
        return {
            "processIntentCalls": after,
            "fsmHistory": copy.deepcopy(observed["coordinatorFsmHistory"]),
            "terminalOutcome": result.get("terminal_outcome"),
            "terminalEvidenceRef": result.get("evidence_record_id"),
            "ledgerReferences": list(observed["coordinatorLedgerReferences"]),
        }

    # -- capture ----------------------------------------------------------
    def publish_not_invoked(self) -> None:
        """Record an honest NOT_INVOKED coordinator block.

        A run that never reached the Coordinator must say so; leaving the
        member at its default would be indistinguishable from a run whose
        observations were never taken.
        """
        self.recorder.set_coordinator({
            "executionMode": "NOT_INVOKED",
            "requiresRealCoordinatorExecution": self.requires_real_execution,
            "syntheticTransitionCount": self.synthetic_transition_count,
        })
