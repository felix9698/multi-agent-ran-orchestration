"""Shared hermetic fixtures for the Campaign 5 official-path tests.

Not a test module.  It stands up the in-repo producer behind an
``R1PolicyPort``-shaped bridge and drives the Write Gateway with Kernel-shaped
tokens, so each test file reads as claims about one family rather than as a
setup script.  Everything here is hermetic: no socket, no radio, no LLM.
"""

from __future__ import annotations

import itertools
from typing import Any, Dict, Mapping

from assurance.gateway.token import KernelToken, TokenKind

from oran.campaign5.families import CAMPAIGN5_FAMILIES, Campaign5Family
from oran.campaign5.producer import Campaign5PolicyProducer
from oran.campaign5.readback import DictKpmConfigReader

CLOCK = "2026-09-02T09:00:00.000000Z"
LEASE = "2026-09-02T10:00:00.000000Z"
VALIDITY = ("2026-09-02T00:00:00Z", "2026-09-03T00:00:00Z")

#: One representative scope + baseline/target value pair per family.
FAMILY_CASES: Dict[str, Dict[str, Any]] = {
    "cap": {
        "scope": {"cellId": "cell-1", "ueId": "ue-1"},
        "baseline": {"maxDlPrbs": 24},
        "target": {"maxDlPrbs": 12},
    },
    "priority": {
        "scope": {"cellId": "cell-1", "ueId": "ue-1"},
        "baseline": {"pfWeight": 1.0},
        "target": {"pfWeight": 4.0},
    },
    "mcs": {
        "scope": {"cellId": "cell-1"},
        "baseline": {"minDlMcs": 0, "maxDlMcs": 28},
        "target": {"minDlMcs": 4, "maxDlMcs": 16},
    },
    "power": {
        "scope": {"cellId": "cell-1", "gnbId": "gnb-1"},
        "baseline": {"txAttenuationDb": 0},
        "target": {"txAttenuationDb": 6},
    },
}


def family(key: str) -> Campaign5Family:
    return CAMPAIGN5_FAMILIES[key]


def permit(transaction_id: str = "tx-1", trial_id: str = "trial-1", fence: int = 1):
    def make(kind: str, expected: str, sequence: int) -> KernelToken:
        return KernelToken(
            token_kind=TokenKind[kind], transaction_id=transaction_id,
            trial_id=trial_id, fencing_token=fence, command_sequence=sequence,
            lease_expiry=LEASE, expected_config_hash=expected,
            idempotency_key=f"{transaction_id}:{kind}:{fence}:{sequence}",
            issued_at=CLOCK,
        )
    return make


class ProducerPolicyPort:
    """Bridge the ``R1PolicyPort`` surface onto the in-repo Campaign 5 producer.

    On create/update it simulates the near-RT worker plus the gNB applying the
    change and, when the gNB advertises the configuration counter, publishing the
    observed value leaves to the KPM stream -- the second, independent
    observation the corroborated readback compares against the producer status.
    ``gnb_applies`` toggles a RIC Control Failure; ``publish_counter`` toggles
    whether the configuration counter exists in the deployed binary yet.
    """

    def __init__(self, producer: Campaign5PolicyProducer, fam: Campaign5Family,
                 kpm: DictKpmConfigReader, *, gnb_applies: bool = True,
                 publish_counter: bool = True) -> None:
        self.producer = producer
        self.family = fam
        self.kpm = kpm
        self.gnb_applies = gnb_applies
        self.publish_counter = publish_counter
        self._ids = (f"pol-{n}" for n in itertools.count(1))
        self._type_of: Dict[str, str] = {}
        self.created: list[str] = []

    def get_policy_type(self, policy_type_id: str) -> Mapping[str, Any]:
        return self.producer.get_policytype(policy_type_id)

    def _apply(self, pid: str, body: Mapping[str, Any]) -> None:
        config = body["config"]
        if not self.gnb_applies:
            self.producer.record_applied(self.family.policy_type_id, pid,
                                         control_ack=False)
            return
        self.producer.record_applied(self.family.policy_type_id, pid,
                                     control_ack=True, observed_config=config)
        if self.publish_counter:
            value_leaves = {f: config[f] for f in self.family.value_fields}
            scope = {f: config[f] for f in self.family.scope_fields}
            self.kpm.publish(self.family.readback_counter, scope, value_leaves)

    def create_policy(self, ric: str, policy_type_id: str,
                      body: Mapping[str, Any]) -> Mapping[str, Any]:
        pid = next(self._ids)
        self._type_of[pid] = policy_type_id
        self.producer.put_policy(policy_type_id, pid, body)
        self.created.append(pid)
        self._apply(pid, body)
        return {"policyId": pid}

    def update_policy(self, pid: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        self.producer.put_policy(self._type_of[pid], pid, body)
        self._apply(pid, body)
        return {}

    def delete_policy(self, pid: str) -> None:
        self._type_of.pop(pid, None)

    def get_policy_status(self, pid: str) -> Mapping[str, Any]:
        return self.producer.get_status(self._type_of[pid], pid)
