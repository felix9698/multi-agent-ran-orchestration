"""The R1 DME boundary: job creation, delivery binding, push receipt, status.

Four SC-084 rows are served here -- ``create-dme-job``, ``dme-push-cell-1``,
``dme-push-cell-2`` and ``dme-status-query`` -- and one frozen rule ranges over
them, ``RULE-R1-DME-PUSH-BINDING``.  The binding discipline it demands is the
existing upper behaviour, composed unchanged from ``oran.rapp.r1_client``:

* a fresh opaque delivery binding is persisted **before** the create request
  leaves the process, so a crash between the durable write and the POST can
  never produce a push destination nobody owns;
* the server-assigned ``dataJobId`` from the create response's ``Location`` is
  then committed onto that pre-created binding;
* a push is accepted only against an active, committed binding whose policy and
  revision match, and a repeated delivery identity with identical bytes is a
  *dedupe*, not a second acceptance.

The push payload must not carry the data job id and the request must not carry
an ``X-Data-Job-Id`` header: the binding, not the payload, is what maps a push to
its job.  Both are checked here rather than assumed.
"""

from __future__ import annotations

import threading
from contextlib import nullcontext
from typing import Any, Mapping

FORBIDDEN_PAYLOAD_MEMBERS = ("dataJobId",)
FORBIDDEN_HEADER_NAMES = ("x-data-job-id",)


class DmeBoundaryError(RuntimeError):
    """A DME push or binding operation violated the frozen binding discipline."""


class DmeBoundary:
    """Delivery-binding lifecycle and push acceptance for the evidence job."""

    def __init__(self, *, client: Any, push_base_path: str,
                 admission: Any = None) -> None:
        self.client = client
        self.push_base_path = str(push_base_path).rstrip("/")
        self._admission = admission
        self._lock = threading.RLock()
        self._duplicate_suppressions = 0
        self._accepted = 0

    # -- binding lifecycle ------------------------------------------------
    def precreate_binding(self, binding_id: str, job: Mapping[str, Any]) -> None:
        with self._admit("precreating an R1 DME delivery binding"):
            self.client.harness_precreate_binding(str(binding_id), dict(job))

    def commit_binding(self, binding_id: str, data_job_id: str) -> None:
        with self._admit("committing an R1 DME delivery binding"):
            self.client.harness_commit_binding(str(binding_id), str(data_job_id))

    # -- push receipt -----------------------------------------------------
    def binding_of(self, path: str) -> str | None:
        target = str(path).rstrip("/")
        if not target.startswith(self.push_base_path + "/"):
            return None
        remainder = target[len(self.push_base_path) + 1:]
        return remainder if remainder and "/" not in remainder else None

    def accept_push(self, *, binding_id: str, headers: Mapping[str, str],
                    record: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        with self._admit("accepting an R1 DME evidence push"):
            for name in FORBIDDEN_HEADER_NAMES:
                if any(str(key).lower() == name for key in headers):
                    raise DmeBoundaryError(
                        "a push request may not carry the %s header: the delivery "
                        "binding maps the push to its job" % name)
            for member in FORBIDDEN_PAYLOAD_MEMBERS:
                if member in record:
                    raise DmeBoundaryError(
                        "a push payload may not carry %s" % member)
            try:
                accepted = self.client.accept_evidence(str(binding_id), dict(record))
            except KeyError:
                return 404, {"code": "AIC_RESOURCE_NOT_FOUND"}
            with self._lock:
                if accepted:
                    self._accepted += 1
                else:
                    self._duplicate_suppressions += 1
            return 204, {}

    def _admit(self, what: str):
        return nullcontext() if self._admission is None else self._admission(what)

    # -- observations -----------------------------------------------------
    def observations(self) -> dict[str, Any]:
        state = self.client.state.snapshot()
        with self._lock:
            return {
                "deliveryBindingCount": len(state.get("bindings", {})),
                "evidenceCommitCount": len(state.get("evidence", {})),
                "acceptedPushes": self._accepted,
                "duplicateSuppressions": self._duplicate_suppressions,
            }

    def reset(self) -> None:
        self.client.harness_reset()
        with self._lock:
            self._duplicate_suppressions = 0
            self._accepted = 0
