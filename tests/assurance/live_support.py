"""A testbed-shaped fake for the Gate 3 live path.  Not a test module.

The live driver's whole risk is *timing*: an A1 policy is accepted in
milliseconds and the handover it asks for lands seconds later, the KPM stream
reports on its own phase, the Kernel's evaluator wants windows that span their
full width, and the permit that authorised the write has a lease that all of
that has to fit inside.  None of that is exercised by a fixture that answers
every readback instantly.

So this module is a *clock-driven* fake: virtual time advances only when the
code under test sleeps or makes a call that would cost time, indications appear
on the cadence the real deployment reports at, and the policy episode reaches
``APPLIED_VERIFIED`` after the latency the real one took.  A run against it is a
dry run of the real sequence, and a change that would have missed a window
misses it here first.

The policy type it serves is the frozen one, loaded from this tree, so the
policy bodies the composition root builds are validated against the same schema
the deployment validates them against.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from oran.rapp.contract_support import load_schema
from oran.rapp.policy_translator import POLICY_TYPE_ID

#: The identities the live lab actually uses, so a dry run and an OTA run are
#: the same run with a different transport.
HOME_NB_ID = 0x00000E00
TARGET_NB_ID = 0x00000B00
HOME_NCI = 12345678
TARGET_NCI = 87654321
HOME_NODE = "ngran=02;plmn=208-095-2;nb=0000003584/00;cudu=none:00000000000000000000"
TARGET_NODE = "ngran=02;plmn=208-095-2;nb=0000002816/00;cudu=none:00000000000000000000"
EXPECTED_EPOCHS: Mapping[str, int] = {HOME_NODE: 173, TARGET_NODE: 174}

#: The identity the AMF handed out for this run, and the AMF that handed it out.
AMF_UE_NGAP_ID = 131
GUAMI: Mapping[str, Any] = {
    "mcc": 208,
    "mnc": 95,
    "mnc_digit_len": 2,
    "amf_region_id": 1,
    "amf_set_id": 64,
    "amf_pointer": 4,
}

WALL_BASE = datetime(2026, 8, 24, 9, 0, 0, tzinfo=timezone.utc)

#: Measured on the live producer: policy created 06:47:27.155, episode
#: ``APPLIED_VERIFIED`` 06:47:34.110.
APPLY_LATENCY_MS = 7_000


@dataclass
class FakeTestbed:
    """One clock-driven O-RAN deployment: R1 policy port plus KPM stream."""

    apply_latency_ms: int = APPLY_LATENCY_MS
    call_cost_ms: int = 120
    kpm_cadence_ms: int = 1000
    amf_ue_ngap_id: int = AMF_UE_NGAP_ID
    #: Whether withdrawing the policy puts the UE back on the home cell.  The
    #: real deployment does not: an A1 delete removes the policy, and a UE that
    #: has already been handed over stays where it was handed to.  Kept as a
    #: switch so the honest terminal for that case can be asserted rather than
    #: assumed.
    withdraw_restores_home: bool = False
    #: Episode outcome.  ``QUARANTINED`` is the producer's own terminal for an
    #: episode that never produced a verified readback.
    episode_outcome: str = "APPLIED_VERIFIED"
    #: When set, the KPM stream keeps reporting the home cell however the
    #: episode ends -- an accepted policy whose effect never reached the radio.
    stream_follows_policy: bool = True

    monotonic: int = 0
    serving_nb_id: int = HOME_NB_ID
    lines: List[str] = field(default_factory=list)
    policies: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    requests: List[Tuple[str, str]] = field(default_factory=list)
    _next_kpm_ms: int = 0
    _applied_at_ms: Optional[int] = None
    _sequence: int = 0

    def __post_init__(self) -> None:
        self._emit_due()

    # -- clock ports -------------------------------------------------------

    def now(self) -> str:
        moment = WALL_BASE + timedelta(milliseconds=self.monotonic)
        return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")

    def monotonic_ms(self) -> int:
        return self.monotonic

    def sleep_ms(self, milliseconds: int) -> None:
        self.advance(max(0, int(milliseconds)))

    def advance(self, milliseconds: int) -> None:
        """Run the deployment forward, emitting what it would have emitted."""
        target = self.monotonic + int(milliseconds)
        while self.monotonic < target:
            events = [target]
            if self._next_kpm_ms > self.monotonic:
                events.append(self._next_kpm_ms)
            if self._applied_at_ms is not None and self._applied_at_ms > self.monotonic:
                events.append(self._applied_at_ms)
            self.monotonic = min(events)
            if self._applied_at_ms is not None and self.monotonic >= self._applied_at_ms:
                self._apply_episode()
            self._emit_due()

    # -- the KPM indication stream ----------------------------------------

    def read_new_lines(self) -> Sequence[str]:
        drained, self.lines = tuple(self.lines), []
        return drained

    def _emit_due(self) -> None:
        while self._next_kpm_ms <= self.monotonic:
            self.lines.append(self._indication(self._next_kpm_ms))
            self._next_kpm_ms += self.kpm_cadence_ms

    def _indication(self, at_ms: int) -> str:
        moment = WALL_BASE + timedelta(milliseconds=at_ms)
        node = HOME_NODE if self.serving_nb_id == HOME_NB_ID else TARGET_NODE
        self._sequence += 1
        return json.dumps(
            {
                "event": "kpm_indication",
                "recv_unix_us": int(moment.timestamp() * 1_000_000),
                "slot": 0 if self.serving_nb_id == HOME_NB_ID else 1,
                "e2_node": node,
                "nb_id": self.serving_nb_id,
                "connection_epoch": EXPECTED_EPOCHS[node],
                "kpm_msg_format": 3,
                "slot_sequence": self._sequence,
                "ues": [
                    {
                        "ue_id_type": "gNB",
                        "amf_ue_ngap_id": self.amf_ue_ngap_id,
                        "guami": dict(GUAMI),
                        "has_ran_ue_id": True,
                        "ran_ue_id": 1,
                        "measurements": [
                            {"name": "DRB.UEThpDl", "type": "real", "value": 512.0}
                        ],
                    }
                ],
            },
            separators=(",", ":"),
        )

    # -- the A1 policy episode --------------------------------------------

    def _apply_episode(self) -> None:
        self._applied_at_ms = None
        for policy in self.policies.values():
            if policy["episodeState"] != "PENDING":
                continue
            policy["episodeState"] = self.episode_outcome
            if self.episode_outcome == "APPLIED_VERIFIED":
                policy["readback"] = "VERIFIED"
                if self.stream_follows_policy:
                    self.serving_nb_id = TARGET_NB_ID
            else:
                policy["readback"] = None

    # -- the R1 policy port ------------------------------------------------

    def _cost(self) -> None:
        self.advance(self.call_cost_ms)

    def get_policy_type(self, policy_type_id: str) -> Mapping[str, Any]:
        self.requests.append(("GET", "policy-type"))
        self._cost()
        if policy_type_id != POLICY_TYPE_ID:
            raise ValueError("policy type is outside the frozen profile")
        return {
            "policyTypeId": POLICY_TYPE_ID,
            "policySchema": load_schema("AIC_UECellSteering_1.0.0.policy"),
            "statusSchema": load_schema("AIC_UECellSteering_1.0.0.status"),
        }

    def create_policy(
        self, near_rt_ric_id: str, policy_type_id: str, policy_object: Dict[str, Any]
    ) -> Mapping[str, Any]:
        self.requests.append(("POST", "policies"))
        self._cost()
        policy_id = f"policy-live-{len(self.policies) + 1}"
        self.policies[policy_id] = {
            "policyObject": json.loads(json.dumps(policy_object)),
            "episodeState": "PENDING",
            "readback": None,
            "createdAt": self.monotonic,
        }
        self._applied_at_ms = self.monotonic + self.apply_latency_ms
        return {"policyId": policy_id}

    def update_policy(self, policy_id: str, policy_object: Dict[str, Any]) -> Mapping[str, Any]:
        self.requests.append(("PUT", policy_id))
        self._cost()
        self.policies[policy_id]["policyObject"] = json.loads(json.dumps(policy_object))
        return {}

    def delete_policy(self, policy_id: str) -> None:
        self.requests.append(("DELETE", policy_id))
        self._cost()
        self.policies.pop(policy_id, None)
        self._applied_at_ms = None
        if self.withdraw_restores_home:
            self.serving_nb_id = HOME_NB_ID

    def get_policy_status(self, policy_id: str) -> Mapping[str, Any]:
        self.requests.append(("GET", f"{policy_id}/status"))
        self._cost()
        policy = self.policies[policy_id]
        state = policy["episodeState"]
        pending = state == "PENDING"
        status: Dict[str, Any] = {
            "enforceStatus": "ENFORCED",
            "aicStatus": {
                "policyId": policy_id,
                "policyRevision": 1,
                "producerEpoch": "3a0d5f4e-0000-4000-8000-000000000001",
                "statusSeq": len(self.requests),
                "policyState": "ACTIVE",
                "policyTerminal": False,
                "episodeId": "9c1d5f4e-0000-4000-8000-000000000002",
                "episodeState": "APPLYING" if pending else state,
                "episodeTerminal": not pending,
                "occurredAt": self.now(),
                "control": {
                    "result": "ACK",
                    "resultIsEffectEvidence": False,
                    "writeMayHaveOccurred": True,
                },
                "rollback": {"state": "NOT_REQUESTED"},
                "trace": {"correlationId": "c-1"},
            },
        }
        if policy["readback"] == "VERIFIED":
            status["aicStatus"]["readback"] = {
                "result": "VERIFIED",
                "observedAt": self.now(),
                "latencyMs": 0,
                "observedServingCell": {
                    "plmnId": {"mcc": "208", "mnc": "95"},
                    "cId": {"ncI": TARGET_NCI},
                },
            }
            status["aicStatus"]["selectedCell"] = status["aicStatus"]["readback"][
                "observedServingCell"
            ]
        else:
            status["aicStatus"]["readback"] = {"result": "PENDING"}
        return status
