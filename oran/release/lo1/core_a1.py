"""The Non-RT A1 consumer and the inbound A1 status surface.

Two SC-084 rows live here.  ``observe-a1-put`` is the upper's only outbound
HTTPS obligation: the A1-P protocol layer stays the frozen
``oran.nonrt.a1_client.A1PClient`` and only its transport is replaced, so the
recorded bytes are the contract's bytes.  ``a1-status-to-framework`` is inbound:
the producer delivers a status to the framework's callback root and the upper
answers 204 only after the framework has accepted it.

The correlation problem is real and is solved from contract-declared material
only: a producer that emits the catalog fixture verbatim names a policy this
framework never assigned.  The status carries ``aicStatus.trace``, the stored
policy carries the same trace, and the correlation is used **only** when it
identifies exactly one stored policy.  Otherwise the status is passed through
untouched and the framework answers for itself.
"""

from __future__ import annotations

import json
import threading
from contextlib import nullcontext
from typing import Any, Mapping

from oran.nonrt.a1_client import A1PClient

from .clients import CapturingA1Transport, CapturingHttpsClient, OutboundRecorderBinding


class A1BoundaryError(RuntimeError):
    """The A1 boundary was driven outside its declared envelope."""


def build_a1_client(*, api_root: str, client: CapturingHttpsClient,
                    binding: OutboundRecorderBinding) -> A1PClient:
    """The frozen A1-P client over the capturing transport."""
    return A1PClient(CapturingA1Transport(
        api_root=api_root, client=client, binding=binding))


class A1StatusInbound:
    """Accept a producer-originated A1 status on the framework's callback root."""

    def __init__(self, *, service: Any, admission: Any = None) -> None:
        self.service = service
        self._admission = admission
        self._lock = threading.RLock()
        self.received = 0
        self.last_status: int | None = None

    def accept(self, body: Mapping[str, Any]) -> int:
        context = (nullcontext() if self._admission is None
                   else self._admission("inbound A1 policy status"))
        with context:
            status = self.service.handle(
                "POST", self.service.a1_notification_path,
                body=self._correlate(body)).status
            with self._lock:
                self.received += 1
                self.last_status = int(status)
            return int(status)

    def observations(self) -> dict[str, Any]:
        with self._lock:
            return {"a1StatusCallbacksReceived": self.received,
                    "lastA1StatusCallbackStatus": self.last_status}

    def reset(self) -> None:
        with self._lock:
            self.received = 0
            self.last_status = None

    # -- correlation ------------------------------------------------------
    def _correlate(self, body: Mapping[str, Any]) -> dict[str, Any]:
        document = json.loads(json.dumps(dict(body)))
        aic = document.get("aicStatus")
        if not isinstance(aic, Mapping):
            return document
        notified = aic.get("policyId")
        store = self.service.store
        if isinstance(notified, str) and store.row(
                "SELECT 1 FROM policies WHERE policy_id=?", (notified,)) is not None:
            return document
        trace = aic.get("trace")
        if not isinstance(trace, Mapping):
            return document
        matches = [
            str(row["policy_id"])
            for row in store.rows("SELECT policy_id,policy_json FROM policies")
            if _trace_matches(store.decode(row["policy_json"]), trace)
        ]
        if len(matches) == 1:
            document["aicStatus"]["policyId"] = matches[0]
        return document


def _trace_matches(policy: Any, trace: Mapping[str, Any]) -> bool:
    if not isinstance(policy, Mapping):
        return False
    candidate = policy.get("policyObject", policy)
    if not isinstance(candidate, Mapping):
        return False
    stored = candidate.get("trace")
    if not isinstance(stored, Mapping):
        return False
    keys = ("intentId", "correlationId", "idempotencyKey", "producerId")
    present = [key for key in keys if key in trace and key in stored]
    if not present:
        return False
    return all(str(stored[key]) == str(trace[key]) for key in present)
