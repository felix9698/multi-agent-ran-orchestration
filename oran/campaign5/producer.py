"""In-repo R1 facade and A1-P v2 producer for four Campaign 5 policy types.

Design section 4.6 Option B: a second, in-repo producer stands up beside the
external steering producer.  It advertises **only** the new types, one active
owner per target scope (cell for mcs/power, UE for cap/priority), and is
capability-gated on the *discovered RAN-function definition* -- a style/action
number without the nested definition is a failed capability gate, so a family
whose definition is not advertised is simply not offered.

The process exposes two views of one policy store.  The rApp-facing ``/r1``
surface implements the policy-management calls used by :class:`R1Client`; the
Near-RT-facing ``/A1-P/v2`` surface remains available unchanged.  R1 operations
translate onto the same methods and records as A1-P, so there is no facade-side
desired-state copy to diverge from the producer.

The service surface is transport-neutral (the same shape as
``oran/slice_actuator/a1.py``) so it can be embedded in the native xApp backend
or exercised hermetically. It emits the frozen steering-shaped status object,
where a control ACK is structurally *not* effect evidence
(``control.resultIsEffectEvidence == false``): a freshly applied policy is
``APPLIED_UNVERIFIED`` with a ``NOT_AVAILABLE`` readback until a corroborated
configuration readback promotes it to ``APPLIED_VERIFIED``.
"""

from __future__ import annotations

import argparse
import copy
import hmac
import json
import logging
import ssl
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

from jsonschema import Draft202012Validator, FormatChecker

from oran.contract.jcs import jcs_sha256

from ._locking import serialized
from .families import (
    CAMPAIGN5_FAMILIES,
    Campaign5Family,
    Campaign5Error,
    campaign5_capability_manifest,
    family_by_policy_type,
    load_campaign5_schema,
)

__all__ = [
    "A1Conflict",
    "A1NotFound",
    "A1ValidationError",
    "Campaign5PolicyProducer",
    "HttpResponse",
    "PutResult",
]

_LOG = logging.getLogger(__name__)


# 거절 종류는 전송 없는 `errors` 에 산다 (2026-09-23, codex 감사 Q14): 이것 하나
# 때문에 composition root 가 이 파일을 -- 그리고 그 너머의 `live_worker` 와
# `subprocess` 를 -- import 하고 있었다.  여기서 다시 내보내므로 기존 import 는 그대로다.
from .errors import (          # noqa: F401  (re-export)
    A1CapabilityGate, A1Conflict, A1Error, A1NotFound, A1ValidationError,
)


@dataclass(frozen=True)
class PutResult:
    http_status: int
    policy_digest: str


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: Any = None
    headers: Mapping[str, str] = field(default_factory=dict)


def _utc(value: Callable[[], datetime] | None) -> datetime:
    return (value or (lambda: datetime.now(timezone.utc)))()


def _z(instant: datetime) -> str:
    return instant.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class _PolicyRecord:
    __slots__ = ("policy", "digest", "scope_key", "epoch", "seq",
                 "intent_id", "correlation_id", "applied", "near_rt_ric_id",
                 "staged")

    def __init__(self, policy: Dict[str, Any], digest: str, scope_key: str,
                 near_rt_ric_id: Optional[str] = None) -> None:
        self.policy = policy
        self.digest = digest
        self.scope_key = scope_key
        self.epoch = str(uuid.uuid4())
        self.seq = 1
        self.intent_id = str(uuid.uuid4())
        self.correlation_id = str(uuid.uuid4())
        self.applied = False
        self.near_rt_ric_id = near_rt_ric_id
        #: An update's ``(policy, digest)`` while the worker applies it.  It
        #: becomes ``policy``/``digest`` only when the worker records an
        #: outcome or returns; a refused update leaves the record untouched.
        self.staged: Optional[Tuple[Dict[str, Any], str]] = None


class Campaign5PolicyProducer:
    """Discover, validate, store, status and delete the four new policy types."""

    def __init__(
        self,
        *,
        discovered_definitions: Optional[Mapping[str, Mapping[str, Any]]] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        manifest = campaign5_capability_manifest()
        self._required = manifest["ranFunctionDefinitions"]
        discovered = (
            discovered_definitions
            if discovered_definitions is not None
            else self._required
        )
        # Capability gate on the definition, not the number: only advertise a
        # type whose nested RAN-function definition is discovered and matches.
        self._advertised: Dict[str, Campaign5Family] = {}
        for family in CAMPAIGN5_FAMILIES.values():
            got = discovered.get(family.policy_type_id)
            want = self._required[family.policy_type_id]
            if isinstance(got, Mapping) and all(
                got.get(k) == want[k]
                for k in ("ricStyleType", "ricControlActionId", "ranParameterIds")
            ):
                self._advertised[family.policy_type_id] = family
        self._clock = clock
        self._policy_schema: Dict[str, Dict[str, Any]] = {}
        self._status_schema: Dict[str, Dict[str, Any]] = {}
        self._policy_validator: Dict[str, Draft202012Validator] = {}
        self._status_validator: Dict[str, Draft202012Validator] = {}
        for type_id in self._advertised:
            ps = load_campaign5_schema(f"{type_id}.policy")
            ss = load_campaign5_schema(f"{type_id}.status")
            self._policy_schema[type_id] = ps
            self._status_schema[type_id] = ss
            self._policy_validator[type_id] = Draft202012Validator(ps, format_checker=FormatChecker())
            self._status_validator[type_id] = Draft202012Validator(ss)
        self._records: Dict[str, _PolicyRecord] = {}
        self._statuses: Dict[str, Dict[str, Any]] = {}
        #: Append-only history of every control outcome the worker reported.
        #: ``_statuses`` keeps only the latest transition per policy, and a
        #: question like "how many controls reached the RAN" cannot be asked
        #: of a value that was overwritten.
        self._controls: List[Dict[str, Any]] = []
        self._scope_owner: Dict[Tuple[str, str], str] = {}
        self._apply_handler: Optional[
            Callable[[str, str, Mapping[str, Any]], None]
        ] = None
        self._delete_handler: Optional[Callable[[str], None]] = None
        self._route_validator: Optional[Callable[[str, str, Mapping[str, Any]], None]] = None
        self._delete_capability = object()
        self._lifecycle_lock = threading.RLock()
        self._cell_locks: Dict[str, Any] = {}

    @property
    def lifecycle_lock(self):
        """Guards this producer's in-memory registry only; it is a **leaf**.

        Nothing that holds it calls a worker.  Writes are ordered by
        :meth:`cell_lock` instead (cell lock, then worker ledger, then this).
        """
        return self._lifecycle_lock

    def cell_lock(self, cell_id: Any):
        """The one writer lock for ``cell_id``, shared with that cell's worker.

        2026-09-23 audit: ``handle`` used to hold ``lifecycle_lock`` across the
        worker's synchronous control (up to 18 s apply + 18 s readback, and
        18+18 s more for a restore), so one cell's write stalled every other
        cell's PUT and even status GETs past the R1 client's 20 s timeout.
        Serializing per cell keeps same-cell (hence same-scope) order and the
        ledger's single writer, and lets other cells proceed.
        """
        with self._lifecycle_lock:
            return self._cell_locks.setdefault(str(cell_id), threading.RLock())

    @staticmethod
    def _cell_of(policy: Mapping[str, Any]) -> str:
        config = policy.get("config")
        return str(config.get("cellId")) if isinstance(config, Mapping) else ""

    # -- discovery ---------------------------------------------------------

    def get_policytypes(self) -> list[str]:
        return [f.policy_type_id for f in CAMPAIGN5_FAMILIES.values()
                if f.policy_type_id in self._advertised]

    def _family(self, policy_type_id: str) -> Campaign5Family:
        if policy_type_id not in self._advertised:
            raise A1CapabilityGate(
                f"{policy_type_id} is not advertised: its RAN-function definition "
                "was not discovered (capability gate on the definition, not the number)"
            )
        return family_by_policy_type(policy_type_id)

    def get_policytype(self, policy_type_id: str) -> Dict[str, Any]:
        self._family(policy_type_id)
        return {
            "policySchema": copy.deepcopy(self._policy_schema[policy_type_id]),
            "statusSchema": copy.deepcopy(self._status_schema[policy_type_id]),
            "ranFunctionDefinition": copy.deepcopy(
                self._required[policy_type_id]
            ),
        }

    def schema_digests(self, policy_type_id: str) -> Dict[str, str]:
        self._family(policy_type_id)
        return {
            "policySchemaJcsSha256": jcs_sha256(self._policy_schema[policy_type_id]),
            "statusSchemaJcsSha256": jcs_sha256(self._status_schema[policy_type_id]),
        }

    def capability_manifest(self) -> Dict[str, Any]:
        """The manifest restricted to the types this producer actually serves."""
        full = campaign5_capability_manifest()
        advertised = set(self._advertised)
        return {
            "manifestId": full["manifestId"],
            "policyTypes": [t for t in full["policyTypes"] if t in advertised],
            "schemaDigests": {t: d for t, d in full["schemaDigests"].items() if t in advertised},
            "ranFunctionDefinitions": {
                t: d for t, d in full["ranFunctionDefinitions"].items() if t in advertised
            },
        }

    # -- validation --------------------------------------------------------

    def _validate_policy(self, policy_type_id: str,
                         policy: Mapping[str, Any]) -> Tuple[Dict[str, Any], str]:
        family = self._family(policy_type_id)
        value = copy.deepcopy(dict(policy))
        errors = sorted(
            self._policy_validator[policy_type_id].iter_errors(value),
            key=lambda error: list(error.absolute_path),
        )
        if errors:
            first = errors[0]
            path = ".".join(str(item) for item in first.absolute_path) or "$"
            raise A1ValidationError(f"{path}: {first.message}")
        config = value["config"]
        if family.key == "mcs" and config["minDlMcs"] > config["maxDlMcs"]:
            raise A1ValidationError("minDlMcs must not exceed maxDlMcs")
        not_before = datetime.fromisoformat(value["validity"]["notBefore"].replace("Z", "+00:00"))
        not_after = datetime.fromisoformat(value["validity"]["notAfter"].replace("Z", "+00:00"))
        if not_before >= not_after:
            raise A1ValidationError("validity.notBefore must precede validity.notAfter")
        return value, self._scope_key(family, config)

    @staticmethod
    def _scope_key(family: Campaign5Family, config: Mapping[str, Any]) -> str:
        return "/".join(f"{field}={config[field]}" for field in family.scope_fields)

    # -- lifecycle ---------------------------------------------------------

    def put_policy(self, policy_type_id: str, policy_id: str,
                   policy: Mapping[str, Any], *,
                   near_rt_ric_id: Optional[str] = None,
                   create_only: bool = False) -> PutResult:
        self._family(policy_type_id)
        if not policy_id or len(policy_id) > 255:
            raise A1ValidationError("policy id must contain 1..255 characters")
        value, scope_key = self._validate_policy(policy_type_id, policy)
        with self.cell_lock(self._cell_of(value)):
            return self._put_in_cell(policy_type_id, policy_id, value, scope_key,
                                     near_rt_ric_id, create_only)

    def _put_in_cell(self, policy_type_id: str, policy_id: str,
                     value: Dict[str, Any], scope_key: str,
                     near_rt_ric_id: Optional[str], create_only: bool) -> PutResult:
        if self._route_validator is not None:
            # Admission is side-effect free: an unbound cell must not claim a
            # policy id, scope owner, revision or pending status.
            self._route_validator(policy_type_id, policy_id, value)
        digest = jcs_sha256(value)
        stored = dict(value, _typeId=policy_type_id)
        with self._lifecycle_lock:
            current = self._records.get(policy_id)
            if current is not None:
                if current.policy.get("_typeId") != policy_type_id:
                    raise A1Conflict("policy id cannot move to a different policy type")
                if (near_rt_ric_id is not None
                        and current.near_rt_ric_id not in (None, near_rt_ric_id)):
                    raise A1Conflict("policy id cannot move to a different Near-RT RIC")
                if current.digest == digest:
                    if current.near_rt_ric_id is None and near_rt_ric_id is not None:
                        current.near_rt_ric_id = near_rt_ric_id
                    return PutResult(200, digest)
                if create_only:
                    raise A1Conflict(
                        "R1 POST create identity already exists with a different "
                        "policy object; update it explicitly with PUT"
                    )
                if current.near_rt_ric_id is None and near_rt_ric_id is not None:
                    current.near_rt_ric_id = near_rt_ric_id
                if current.scope_key != scope_key:
                    raise A1Conflict("policy id cannot move to a different target scope")
                new_trace, cur_trace = value["trace"], current.policy["trace"]
                if (new_trace["revision"] <= cur_trace["revision"]
                        or new_trace["fencingToken"] <= cur_trace["fencingToken"]):
                    raise A1Conflict("policy update requires a newer revision and fencingToken")
                if self._apply_handler is None:
                    self._commit_update(policy_type_id, policy_id, current, stored, digest)
                    return PutResult(200, digest)
                # 2026-09-23 audit: the update used to overwrite policy/digest/seq
                # *before* the worker ran.  A refused update then left the new
                # digest in place, and resending the same body matched it and
                # answered 200 without calling the worker -- a silent no-op.
                # Stage it; it is committed only once the worker accepts it.
                current.staged = (stored, digest)
            else:
                owner_key = (policy_type_id, scope_key)
                owner = self._scope_owner.get(owner_key)
                if owner is not None and owner != policy_id:
                    raise A1Conflict(f"target scope already owned by policy {owner}")
                record = _PolicyRecord(stored, digest, scope_key,
                                       near_rt_ric_id=near_rt_ric_id)
                self._records[policy_id] = record
                self._scope_owner[owner_key] = policy_id
                self._statuses[policy_id] = self._pending_status(
                    policy_type_id, policy_id, record)
        if current is not None:
            try:
                self._apply_live(policy_type_id, policy_id, stored)
            except Exception:
                # A failed update keeps the policy that was already there --
                # that one is real, and the worker may owe it a rollback.
                with self._lifecycle_lock:
                    current.staged = None
                raise
            with self._lifecycle_lock:
                if current.staged is not None:   # the worker recorded nothing
                    self._commit_update(policy_type_id, policy_id, current, stored, digest)
            return PutResult(200, digest)
        try:
            self._apply_live(policy_type_id, policy_id, stored)
        except Exception:
            # A create whose apply handler refused never became a policy: the
            # client is told the write failed, so the registry must not keep it.
            #
            # Keeping it stranded the scope **permanently**.  The worker raises
            # before writing its own ledger (identity refusal, unavailable
            # baseline, scope conflict), so ``expire_due`` -- which walks that
            # ledger -- never sees the record and never expires it, while this
            # ``_scope_owner`` claim refuses every later write to the same axis
            # with ``target scope already owned by policy <id>``.
            #
            # Measured on 2026-09-17: nine policies were live here and **not one**
            # was in either worker ledger; one of them (``78c1aa67``, pfWeight on
            # ue 3) refused four consecutive sittings and three of them ended
            # ``RECOVERY_FAILURE``.
            with self._lifecycle_lock:
                self._records.pop(policy_id, None)
                self._scope_owner.pop((policy_type_id, scope_key), None)
                self._statuses.pop(policy_id, None)
            raise
        return PutResult(201, digest)

    def _commit_update(self, policy_type_id: str, policy_id: str,
                       record: _PolicyRecord, stored: Dict[str, Any],
                       digest: str) -> None:
        record.staged = None
        record.policy = stored
        record.digest = digest
        record.seq += 1
        record.applied = False
        self._statuses[policy_id] = self._pending_status(policy_type_id, policy_id, record)

    def _apply_live(self, policy_type_id: str, policy_id: str,
                    stored: Mapping[str, Any]) -> None:
        if self._apply_handler is None:
            return
        body = copy.deepcopy(dict(stored))
        body.pop("_typeId", None)
        self._apply_handler(policy_type_id, policy_id, body)

    @serialized
    def get_policy(self, policy_type_id: str, policy_id: str) -> Dict[str, Any]:
        self._family(policy_type_id)
        record = self._records.get(policy_id)
        if record is None or record.policy.get("_typeId") != policy_type_id:
            raise A1NotFound(f"unknown policy {policy_id}")
        body = copy.deepcopy(record.policy)
        body.pop("_typeId", None)
        return body

    @serialized
    def list_policies(self, policy_type_id: str) -> list[str]:
        self._family(policy_type_id)
        return sorted(
            pid for pid, rec in self._records.items()
            if rec.policy.get("_typeId") == policy_type_id
        )

    @serialized
    def get_status(self, policy_type_id: str, policy_id: str) -> Dict[str, Any]:
        self._family(policy_type_id)
        record = self._records.get(policy_id)
        if record is None or record.policy.get("_typeId") != policy_type_id:
            raise A1NotFound(f"unknown policy {policy_id}")
        return copy.deepcopy(self._statuses[policy_id])

    # -- status construction ----------------------------------------------

    def _trace(self, record: _PolicyRecord) -> Dict[str, Any]:
        return {
            "intentId": record.intent_id,
            "intentRevision": record.policy["trace"]["revision"],
            "correlationId": record.correlation_id,
        }

    def _base_aic(self, policy_id: str, record: _PolicyRecord) -> Dict[str, Any]:
        return {
            "policyId": policy_id,
            "policyRevision": record.policy["trace"]["revision"],
            "producerEpoch": record.epoch,
            "statusSeq": record.seq,
            "occurredAt": _z(_utc(self._clock)),
            "trace": self._trace(record),
        }

    def _pending_status(self, policy_type_id: str, policy_id: str,
                        record: _PolicyRecord) -> Dict[str, Any]:
        aic = self._base_aic(policy_id, record)
        aic.update({"policyState": "NOT_ENFORCED", "policyTerminal": False})
        status = {"enforceStatus": "NOT_ENFORCED", "enforceReason": "OTHER_REASON",
                  "aicStatus": aic}
        self._status_validator[policy_type_id].validate(status)
        return status

    @serialized
    def record_applied(self, policy_type_id: str, policy_id: str, *,
                       control_ack: bool,
                       observed_config: Optional[Mapping[str, Any]] = None,
                       write_may_have_occurred: bool = False,
                       restore_config: Optional[Mapping[str, Any]] = None) -> None:
        """Record one apply attempt's outcome as a status transition.

        ``write_may_have_occurred`` is **not** the negation of ``control_ack``.
        A control can fail to acknowledge after the radio already took the value,
        and only the worker knows which happened -- it holds the ledger that says
        whether it still owes a rollback.  Reporting an applied write as
        "nothing was written" is not a cosmetic audit slip: the adapter's
        ``_refused_without_writing()`` believes this field and then skips the
        policy readback in favour of an independent counter path
        (``assurance/gateway/r1_adapter.py``).  2026-09-21: the radio held
        ``maxDlPrbs = 6`` while the status said no write had occurred.

        ``observed_config`` is a *corroborated* configuration readback (both the
        producer and an independent counter agree); when it is ``None`` -- the
        honest state while the configuration counter is unbuilt -- the episode is
        ``APPLIED_UNVERIFIED`` with a ``NOT_AVAILABLE`` readback, never enforced.
        """
        family = self._family(policy_type_id)
        record = self._records.get(policy_id)
        if record is None or record.policy.get("_typeId") != policy_type_id:
            raise A1NotFound(f"unknown policy {policy_id}")
        if record.staged is not None:
            # The worker accepted the staged update far enough to report on it.
            record.policy, record.digest = record.staged
            record.staged = None
        record.applied = True
        record.seq += 1
        selected_key = "selected" + family.observed_key[len("observed"):]
        config = record.policy["config"]
        control_result = "ACK" if control_ack else "NACK"
        if not control_ack:
            # 계약은 `APPLY_FAILED` 를 두 모양으로 나눠 둔다(status 스키마 allOf 4·21):
            # 쓰기가 **없었으면** 종결이고 rollback 칸이 없다; 쓰기가 **있었을 수 있으면**
            # `rollback.state = REQUESTED` 를 달고 종결이 아니다.  둘을 가르는 것은 ACK 이
            # 아니라 **쓰기 가능성**이고, 그것은 워커의 원장만 안다.  전에는 ACK 부재를
            # 그대로 쓰기 부재로 번역해 첫 모양만 냈고, adapter 의
            # `_refused_without_writing()` 이 그 말을 믿어 정책 되읽기를 건너뛰었다
            # (2026-09-21 codex 재현: 라디오가 cap 6 을 들고 있는데 "안 썼다" 로 기록).
            wrote = bool(write_may_have_occurred)
            aic = self._base_aic(policy_id, record)
            aic.update({
                "policyState": "NOT_ENFORCED", "policyTerminal": False,
                "episodeId": str(uuid.uuid4()), "episodeState": "APPLY_FAILED",
                "episodeTerminal": not wrote,
                selected_key: config,
                "control": {"transactionId": str(uuid.uuid4()), "actionId": str(uuid.uuid4()),
                            "result": "NACK", "resultIsEffectEvidence": False,
                            "writeMayHaveOccurred": wrote},
                "error": {"code": "AIC_APPLY_FAILED", "stage": "CONTROL", "retryable": True,
                          "writeMayHaveOccurred": wrote, "detail": "RIC Control Failure"},
            })
            if wrote:
                # 요청된 롤백은 **무엇으로 되돌릴지**를 함께 말해야 한다(스키마 요구).
                # 그 값은 프로듀서가 아니라 워커의 원장에 있다 -- 넘어오지 않았다면
                # 롤백을 요청했다고 적을 수 없으므로 거절한다: 복구 대상 없는 롤백
                # 요청은 읽는 쪽에 거짓 안심을 준다.
                if restore_config is None:
                    raise A1ValidationError(
                        "a requested rollback must name the configuration it restores")
                # 원장의 기준선은 **값만** 담는다(`{"maxDlPrbs": 0}`).  스키마가 요구하는
                # 것은 `selected...` 와 같은 완전한 구성이므로, 이 정책의 scope 위에
                # 기준선 값을 얹는다.  밑줄로 시작하는 내부 칸은 싣지 않는다.
                aic["rollback"] = {
                    "state": "REQUESTED",
                    "restore" + family.observed_key[len("observed"):]: dict(
                        config,
                        **{k: v for k, v in dict(restore_config).items()
                           if not str(k).startswith("_")}),
                }
            status = {"enforceStatus": "NOT_ENFORCED", "enforceReason": "OTHER_REASON",
                      "aicStatus": aic}
        elif observed_config is not None and dict(observed_config) == dict(config):
            aic = self._base_aic(policy_id, record)
            aic.update({
                "policyState": "ACTIVE", "policyTerminal": False,
                "episodeId": str(uuid.uuid4()), "episodeState": "APPLIED_VERIFIED",
                "episodeTerminal": True,
                selected_key: config,
                "control": {"transactionId": str(uuid.uuid4()), "actionId": str(uuid.uuid4()),
                            "result": "ACK", "resultIsEffectEvidence": False,
                            "writeMayHaveOccurred": True},
                "readback": {"result": "VERIFIED", family.observed_key: config,
                             "observedAt": _z(_utc(self._clock)), "latencyMs": 5},
                "rollback": {"state": "NOT_REQUESTED"},
            })
            status = {"enforceStatus": "ENFORCED", "aicStatus": aic}
        else:
            aic = self._base_aic(policy_id, record)
            aic.update({
                "policyState": "NOT_ENFORCED", "policyTerminal": False,
                "episodeId": str(uuid.uuid4()), "episodeState": "APPLIED_UNVERIFIED",
                "episodeTerminal": False,
                selected_key: config,
                "control": {"transactionId": str(uuid.uuid4()), "actionId": str(uuid.uuid4()),
                            "result": control_result, "resultIsEffectEvidence": False,
                            "writeMayHaveOccurred": True},
                "readback": {"result": "NOT_AVAILABLE",
                             "observedAt": _z(_utc(self._clock)), "latencyMs": 0},
            })
            status = {"enforceStatus": "NOT_ENFORCED", "enforceReason": "OTHER_REASON",
                      "aicStatus": aic}
        self._status_validator[policy_type_id].validate(status)
        self._statuses[policy_id] = status
        control = aic["control"]
        self._controls.append({
            "policyTypeId": policy_type_id,
            "policyId": policy_id,
            "statusSeq": record.seq,
            "result": control["result"],
            "writeMayHaveOccurred": bool(control["writeMayHaveOccurred"]),
            "episodeState": aic["episodeState"],
            "readback": (aic.get("readback") or {}).get("result", "NONE"),
            "observedConfig": (dict(observed_config)
                               if observed_config is not None else None),
            "occurredAt": aic["occurredAt"],
        })

    @serialized
    def control_records(self, policy_type_id: Optional[str] = None,
                        policy_id: Optional[str] = None) -> list[Dict[str, Any]]:
        """Every control outcome this producer recorded, oldest first.

        The producer's own account of what the worker reported: one entry per
        ``record_applied`` call, kept because *how many* controls reached the
        RAN is a different question from *where the policy stands now*, and
        only the second one survives in ``get_status``.
        """
        return [dict(entry) for entry in self._controls
                if (policy_type_id is None
                    or entry["policyTypeId"] == policy_type_id)
                and (policy_id is None or entry["policyId"] == policy_id)]

    # -- delete ------------------------------------------------------------

    @serialized
    def bind_live_worker(
        self,
        apply_handler: Callable[[str, str, Mapping[str, Any]], None],
        delete_handler: Callable[[str], None],
        *,
        route_validator: Optional[Callable[[str, str, Mapping[str, Any]], None]] = None,
    ) -> object:
        """Bind the sole live writer and rollback owner.

        Hardware-free construction never calls this method.  The explicit
        live composition binds both directions together so a policy cannot be
        applied by one worker and deleted through another rollback authority.
        """
        if self._apply_handler is not None and self._apply_handler != apply_handler:
            raise A1Conflict("A1 PUT already has a live actuation worker")
        if self._apply_handler is not None and self._route_validator != route_validator:
            raise A1Conflict("live route validation is already bound")
        capability = self.bind_delete_handler(delete_handler)
        self._apply_handler = apply_handler
        self._route_validator = route_validator
        return capability

    @serialized
    def bind_delete_handler(self, handler: Callable[[str], None]) -> object:
        if self._delete_handler is not None and self._delete_handler != handler:
            raise A1Conflict("A1 DELETE already has a rollback worker")
        self._delete_handler = handler
        return self._delete_capability

    @serialized
    def policy_record(self, policy_id: str) -> Dict[str, Any]:
        """Return a worker-only snapshot without exposing internal mutability."""
        record = self._records.get(policy_id)
        if record is None:
            raise A1NotFound(f"unknown policy {policy_id}")
        body = copy.deepcopy(record.policy)
        policy_type_id = body.pop("_typeId")
        return {"policyTypeId": policy_type_id, "policy": body,
                "policyDigest": record.digest,
                "nearRtRicId": record.near_rt_ric_id}

    @serialized
    def delete_after_rollback(self, policy_type_id: str, policy_id: str,
                              capability: object) -> int:
        self._family(policy_type_id)
        if capability is not self._delete_capability or self._delete_handler is None:
            raise A1Conflict("policy deletion requires the bound rollback worker")
        record = self._records.get(policy_id)
        if record is None or record.policy.get("_typeId") != policy_type_id:
            raise A1NotFound(f"unknown policy {policy_id}")
        self._records.pop(policy_id)
        self._statuses.pop(policy_id, None)
        self._scope_owner.pop((policy_type_id, record.scope_key), None)
        return 204

    def _delete_live(self, policy_id: str) -> None:
        """Run the bound rollback worker under the policy's cell lock only."""
        with self._lifecycle_lock:
            record = self._records.get(policy_id)
            if record is None:
                raise A1NotFound(f"unknown policy {policy_id}")
            cell = self._cell_of(record.policy)
        with self.cell_lock(cell):
            if self._delete_handler is None:
                raise A1Conflict("DELETE requires a bound rollback worker")
            self._delete_handler(policy_id)

    # -- transport-neutral R1 + A1-P routing -------------------------------

    def handle(self, method: str, path: str,
               body: Optional[Mapping[str, Any]] = None, *,
               headers: Optional[Mapping[str, str]] = None) -> HttpResponse:
        verb = method.upper()
        parsed = urlsplit(path)
        route_path = unquote(parsed.path.rstrip("/")) or "/"
        query = {
            key: values[-1]
            for key, values in parse_qs(parsed.query, keep_blank_values=True).items()
        }
        normalized_headers = {
            str(key).lower(): str(value) for key, value in (headers or {}).items()
        }
        if route_path == "/r1" or route_path.startswith("/r1/"):
            return self._handle_r1(
                verb, route_path, query, normalized_headers, body
            )
        parts = [part for part in route_path.split("/") if part]
        try:
            if parts == ["A1-P", "v2", "policytypes"] and verb == "GET":
                return HttpResponse(200, self.get_policytypes())
            if len(parts) == 4 and parts[:3] == ["A1-P", "v2", "policytypes"] and verb == "GET":
                return HttpResponse(200, self.get_policytype(parts[3]))
            if (len(parts) == 5 and parts[:3] == ["A1-P", "v2", "policytypes"]
                    and parts[4] == "policies" and verb == "GET"):
                return HttpResponse(200, self.list_policies(parts[3]))
            if len(parts) >= 6 and parts[:3] == ["A1-P", "v2", "policytypes"] and parts[4] == "policies":
                policy_type_id, policy_id = parts[3], parts[5]
                if len(parts) == 7 and parts[6] == "status" and verb == "GET":
                    return HttpResponse(200, self.get_status(policy_type_id, policy_id))
                if len(parts) != 6:
                    return HttpResponse(404, {"error": "unknown A1-P resource"})
                if verb == "PUT":
                    if body is None:
                        raise A1ValidationError("PUT requires a JSON policy body")
                    result = self.put_policy(policy_type_id, policy_id, body)
                    return HttpResponse(result.http_status, {"policyDigest": result.policy_digest})
                if verb == "GET":
                    return HttpResponse(200, self.get_policy(policy_type_id, policy_id))
                if verb == "DELETE":
                    self.get_policy(policy_type_id, policy_id)
                    self._delete_live(policy_id)
                    return HttpResponse(204)
            return HttpResponse(404, {"error": "unknown A1-P resource"})
        except A1CapabilityGate as exc:
            return HttpResponse(404, {"error": str(exc)})
        except A1Conflict as exc:
            return HttpResponse(409, {"error": str(exc)})
        except A1ValidationError as exc:
            return HttpResponse(400, {"error": str(exc)})
        except A1NotFound as exc:
            return HttpResponse(404, {"error": str(exc)})
        except Campaign5Error as exc:
            return HttpResponse(404, {"error": str(exc)})

    @staticmethod
    def _versioned(status: int, body: Any = None, **headers: str) -> HttpResponse:
        return HttpResponse(
            status, body, {"Version": "1.0.0", **headers}
        )

    @staticmethod
    def _require_version(headers: Mapping[str, str], expected: str) -> None:
        if headers.get("version") != expected:
            raise A1ValidationError(f"Version header must be {expected}")

    @serialized
    def _r1_policy_type(self, policy_id: str) -> str:
        record = self._records.get(policy_id)
        if record is None:
            raise A1NotFound(f"unknown policy {policy_id}")
        return str(record.policy["_typeId"])

    def _r1_information(self, policy_id: str) -> Dict[str, Any]:
        record = self.policy_record(policy_id)
        return {
            "nearRtRicId": record["nearRtRicId"],
            "policyTypeId": record["policyTypeId"],
            "policyObject": record["policy"],
        }

    @staticmethod
    def _r1_policy_id(near_rt_ric_id: str, policy_type_id: str,
                      policy: Mapping[str, Any]) -> str:
        """Stable A1 resource id for one retryable R1 create identity."""
        trace = policy.get("trace")
        trace_id = trace.get("traceId") if isinstance(trace, Mapping) else None
        if not isinstance(trace_id, str) or not trace_id:
            raise A1ValidationError("policyObject.trace.traceId is required")
        identity = jcs_sha256({
            "nearRtRicId": near_rt_ric_id,
            "policyTypeId": policy_type_id,
            "traceId": trace_id,
        })
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"oran-aic:campaign5:r1:{identity}"))

    def _r1_delete(self, policy_id: str) -> None:
        self._r1_policy_type(policy_id)
        self._delete_live(policy_id)

    def _handle_r1(self, method: str, path: str, query: Mapping[str, str],
                   headers: Mapping[str, str],
                   body: Optional[Mapping[str, Any]]) -> HttpResponse:
        """Map the Cockpit's R1 profile onto this producer's one policy store."""
        relative = path[len("/r1"):] or "/"
        try:
            if method == "GET" and relative == "/bootstrap/v1/bootstrap-info":
                self._require_version(headers, "1.0.0")
                return self._versioned(200, {
                    "bootstrap": "ready",
                    "policyTypes": self.get_policytypes(),
                })

            if method == "GET" and relative == "/service-apis/v1/allServiceAPIs":
                self._require_version(headers, "1.2.0")
                if not query.get("api-invoker-id"):
                    raise A1ValidationError("api-invoker-id is required")
                service = {
                    "apiName": "a1-policy-management",
                    "apiVersion": "v1",
                    "vendorSpecific-o-ran.org": {"fullApiVersions": ["1.0.0"]},
                }
                services = [service]
                if query.get("api-name") not in (None, service["apiName"]):
                    services = []
                if query.get("api-version") not in (None, service["apiVersion"]):
                    services = []
                return HttpResponse(200, services, {"Version": "1.2.0"})

            root = "/a1-policy-management/v1"
            if not relative.startswith(root):
                return HttpResponse(404, {"error": "unknown R1 resource"})
            self._require_version(headers, "1.0.0")
            suffix = relative[len(root):]

            if suffix == "/policy-types" and method == "GET":
                return self._versioned(200, self.get_policytypes())
            if suffix.startswith("/policy-types/") and method == "GET":
                policy_type_id = suffix[len("/policy-types/"):]
                detail = self.get_policytype(policy_type_id)
                return self._versioned(
                    200, {"policyTypeId": policy_type_id, **detail}
                )

            if suffix == "/policies" and method == "POST":
                if not isinstance(body, Mapping) or set(body) != {
                    "nearRtRicId", "policyTypeId", "policyObject"
                }:
                    raise A1ValidationError(
                        "PolicyObjectInformation has invalid members"
                    )
                near_rt_ric_id = body["nearRtRicId"]
                policy_type_id = body["policyTypeId"]
                policy = body["policyObject"]
                if not isinstance(near_rt_ric_id, str) or not near_rt_ric_id:
                    raise A1ValidationError("nearRtRicId must be non-empty")
                if not isinstance(policy_type_id, str) or not isinstance(policy, Mapping):
                    raise A1ValidationError("policy type and policy object are required")
                policy_id = self._r1_policy_id(
                    near_rt_ric_id, policy_type_id, policy
                )
                self.put_policy(
                    policy_type_id, policy_id, policy,
                    near_rt_ric_id=near_rt_ric_id,
                    create_only=True,
                )
                location = f"/r1{root}/policies/{policy_id}"
                return self._versioned(
                    201, self._r1_information(policy_id), Location=location
                )

            if suffix == "/policies" and method == "GET":
                results = []
                with self._lifecycle_lock:
                    for policy_id in sorted(self._records):
                        record = self._records[policy_id]
                        if (query.get("policyTypeId") is not None
                                and record.policy.get("_typeId") != query["policyTypeId"]):
                            continue
                        if (query.get("nearRtRicId") is not None
                                and record.near_rt_ric_id not in
                                (None, query["nearRtRicId"])):
                            continue
                        results.append(self._r1_information(policy_id))
                return self._versioned(200, results)

            if suffix.startswith("/policies/"):
                item = suffix[len("/policies/"):]
                if item.endswith("/status"):
                    if method != "GET":
                        return HttpResponse(405, {"error": "unsupported R1 operation"})
                    policy_id = item[:-len("/status")]
                    policy_type_id = self._r1_policy_type(policy_id)
                    return self._versioned(
                        200, self.get_status(policy_type_id, policy_id)
                    )
                policy_id = item
                policy_type_id = self._r1_policy_type(policy_id)
                if method == "GET":
                    return self._versioned(200, self._r1_information(policy_id))
                if method == "PUT":
                    if not isinstance(body, Mapping):
                        raise A1ValidationError("PUT requires a JSON policy body")
                    self.put_policy(
                        policy_type_id, policy_id, body,
                        near_rt_ric_id=self.policy_record(policy_id)["nearRtRicId"],
                    )
                    return self._versioned(200, self._r1_information(policy_id))
                if method == "DELETE":
                    self._r1_delete(policy_id)
                    return self._versioned(204)

            return HttpResponse(404, {"error": "unknown R1 policy resource"})
        except A1CapabilityGate as exc:
            return self._versioned(404, {"error": str(exc)})
        except A1NotFound as exc:
            return self._versioned(404, {"error": str(exc)})
        except A1Conflict as exc:
            return self._versioned(409, {"error": str(exc)})
        except A1ValidationError as exc:
            return self._versioned(400, {"error": str(exc)})
        except Campaign5Error as exc:
            return self._versioned(404, {"error": str(exc)})


class _Campaign5HttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], producer: Campaign5PolicyProducer,
                 bearer_token: str, *, secret_path: Optional[Path] = None,
                 rotation_grace_seconds: float = 60.0) -> None:
        self.producer = producer
        self.bearer_token = bearer_token
        # The R1 access token is rotated on disk by the token refresher (every few minutes);
        # a producer that pinned the token at start-up answers 401 to every rApp call after the
        # first rotation. Re-read the secret file per request and honour the previous token for
        # a short grace window so a request in flight across a rotation is not refused.
        self._secret_path = secret_path
        self._secret_mtime_ns: Optional[int] = None
        self._previous_token: Optional[str] = None
        self._previous_until = 0.0
        self._rotation_grace_seconds = float(rotation_grace_seconds)
        self._token_lock = threading.Lock()
        super().__init__(address, _Campaign5Handler)

    def _refresh_token(self) -> None:
        if self._secret_path is None:
            return
        try:
            mtime_ns = self._secret_path.stat().st_mtime_ns
            if mtime_ns == self._secret_mtime_ns:
                return
            token = self._secret_path.read_text(encoding="utf-8").strip()
        except OSError:
            return  # keep serving with the last good token
        if not token or "\n" in token or "\r" in token:
            return
        self._secret_mtime_ns = mtime_ns
        if token != self.bearer_token:
            self._previous_token = self.bearer_token
            self._previous_until = time.monotonic() + self._rotation_grace_seconds
            self.bearer_token = token

    def accepts_authorization(self, supplied: str) -> bool:
        with self._token_lock:
            self._refresh_token()
            candidates = ["Bearer " + self.bearer_token]
            if self._previous_token is not None and time.monotonic() < self._previous_until:
                candidates.append("Bearer " + self._previous_token)
        return any(hmac.compare_digest(supplied, expected) for expected in candidates)


class _Campaign5Handler(BaseHTTPRequestHandler):
    server: _Campaign5HttpServer
    server_version = "oran-aic-campaign5-a1p/1.0"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler protocol
        self._dispatch()

    def do_PUT(self) -> None:  # noqa: N802 - stdlib handler protocol
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler protocol
        self._dispatch()

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler protocol
        self._dispatch()

    def _dispatch(self) -> None:
        supplied = self.headers.get("Authorization", "")
        if not self.server.accepts_authorization(supplied):
            self._reply(HttpResponse(401, {"error": "unauthorized"}))
            return
        length_text = self.headers.get("Content-Length", "0")
        try:
            length = int(length_text)
        except ValueError:
            self._reply(HttpResponse(400, {"error": "invalid Content-Length"}))
            return
        if length < 0 or length > 1024 * 1024:
            self._reply(HttpResponse(413, {"error": "request body too large"}))
            return
        body: Optional[Mapping[str, Any]] = None
        if length:
            try:
                parsed = json.loads(self.rfile.read(length))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._reply(HttpResponse(400, {"error": "invalid JSON"}))
                return
            if not isinstance(parsed, Mapping):
                self._reply(HttpResponse(400, {"error": "JSON body must be an object"}))
                return
            body = parsed
        try:
            response = self.server.producer.handle(
                self.command, self.path, body, headers=dict(self.headers.items())
            )
        except Exception:
            # Do not reflect paths, subprocess output or secret-adjacent detail.
            response = HttpResponse(500, {"error": "live worker failure"})
        self._reply(response)

    def _reply(self, response: HttpResponse) -> None:
        payload = b"" if response.body is None else json.dumps(
            response.body, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self.send_response(response.status)
        for name, value in response.headers.items():
            self.send_header(name, value)
        if payload:
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        del format, args


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Serve the in-repo Campaign-5 A1-P producer"
    )
    parser.add_argument("--listen-host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--tls-cert")
    parser.add_argument("--tls-key")
    parser.add_argument("--client-ca")
    parser.add_argument("--secret-file", required=True)
    parser.add_argument("--insecure-loopback", action="store_true",
                        help="tests/development only; still requires bearer auth")
    parser.add_argument("--live-xapp", action="store_true",
                        help="enable the our_rc_xapp writer (default is hardware-free)")
    parser.add_argument("--kpm-jsonl")
    parser.add_argument("--header-dir")
    parser.add_argument("--fire-xapp")
    parser.add_argument("--ledger")
    parser.add_argument("--cell-id")
    parser.add_argument("--nb-id", type=int)
    parser.add_argument(
        "--cell-bindings", metavar="JSON_FILE",
        help="explicit [{cellId, nbId, ledgerPath}, ...]; relative ledger paths "
             "are relative to this file; mutually exclusive with --cell-id/--nb-id/--ledger",
    )
    parser.add_argument("--freshness-seconds", type=float, default=5.0)
    parser.add_argument("--control-deadline-seconds", type=float, default=18.0)
    parser.add_argument("--expiry-poll-seconds", type=float, default=1.0)
    return parser


def _configured_live_worker(args: argparse.Namespace):
    """Build only operator-declared bindings; construction never sends controls."""
    if not args.live_xapp:
        return None
    from .live_worker import Campaign5LiveWorker, Campaign5WorkerDispatcher
    required = {
        "--kpm-jsonl": args.kpm_jsonl,
        "--header-dir": args.header_dir,
        "--fire-xapp": args.fire_xapp,
    }
    if args.cell_bindings is None:
        required.update({"--ledger": args.ledger, "--cell-id": args.cell_id, "--nb-id": args.nb_id})
    elif any(value is not None for value in (args.ledger, args.cell_id, args.nb_id)):
        raise SystemExit("--cell-bindings cannot be mixed with --ledger/--cell-id/--nb-id")
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise SystemExit("--live-xapp requires " + ", ".join(missing))
    common = {
        "kpm_jsonl": args.kpm_jsonl, "header_dir": args.header_dir,
        "fire_xapp": args.fire_xapp, "freshness_s": args.freshness_seconds,
        "deadline_s": args.control_deadline_seconds,
        # A joint trial's hand-back is ACKed before the UE re-attaches; a cell-power restore
        # right after it must wait for a UE on the cell (2026-09-26, Codex review of 507e23efc).
        "cell_ue_wait_s": 25.0,
    }
    if args.cell_bindings is None:
        return Campaign5LiveWorker(
            **common, ledger_path=args.ledger, cell_id=args.cell_id, nb_id=args.nb_id
        )
    path = Path(args.cell_bindings).resolve()
    try:
        bindings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit("--cell-bindings must be a readable JSON deployment map") from exc
    if not isinstance(bindings, list) or not bindings:
        raise SystemExit("--cell-bindings must be a non-empty list of explicit bindings")
    workers = []
    for entry in bindings:
        if not isinstance(entry, dict) or set(entry) != {"cellId", "nbId", "ledgerPath"}:
            raise SystemExit("each cell binding requires exactly cellId, nbId, ledgerPath")
        cell, node, ledger = entry["cellId"], entry["nbId"], entry["ledgerPath"]
        if not isinstance(cell, str) or not cell.strip() or cell.strip() != cell or len(cell) > 128:
            raise SystemExit("cell binding cellId must be a nonblank deployment identity")
        if type(node) is not int or not 0 <= node <= 0xffffffff:
            raise SystemExit("cell binding nbId must be an unsigned 32-bit integer")
        if not isinstance(ledger, str) or not ledger.strip():
            raise SystemExit("cell binding ledgerPath must be nonblank")
        workers.append(Campaign5LiveWorker(
            **common, cell_id=cell, nb_id=node, ledger_path=path.parent / ledger
        ))
    return Campaign5WorkerDispatcher(workers)


def _expiry_loop(worker: Any, stop: threading.Event, interval_s: float) -> None:
    """Run expiry passes until ``stop``; one failed pass never ends the timer.

    2026-09-23 audit: the loop had no ``try``, so a single exception (an
    unreadable ledger, a malformed ``notAfter``) killed the thread and every
    later expiry and rollback silently stopped for every cell.
    """
    while not stop.wait(interval_s):
        try:
            worker.expire_due()
        except Exception:      # noqa: BLE001 - logged; the next pass retries
            _LOG.exception("Campaign-5 expiry pass failed; retrying next poll")


def _main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be in 1..65535")
    if args.insecure_loopback:
        if args.listen_host not in {"127.0.0.1", "::1", "localhost"}:
            raise SystemExit("--insecure-loopback may bind only a loopback host")
    elif not all((args.tls_cert, args.tls_key, args.client_ca)):
        raise SystemExit("secure serving requires --tls-cert, --tls-key and --client-ca")
    try:
        token = Path(args.secret_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit("cannot read --secret-file") from exc
    if not token or "\n" in token or "\r" in token:
        raise SystemExit("--secret-file must contain exactly one non-empty token")

    producer = Campaign5PolicyProducer()
    from .live_worker import LiveWorkerError
    try:
        worker = _configured_live_worker(args)
        if worker is not None:
            worker.bind(producer)
    except LiveWorkerError as exc:
        raise SystemExit(f"live worker startup refused: {exc}") from exc

    server = _Campaign5HttpServer((args.listen_host, args.port), producer, token,
                                  secret_path=Path(args.secret_file))
    if not args.insecure_loopback:
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.tls_cert, args.tls_key)
        context.load_verify_locations(cafile=args.client_ca)
        context.verify_mode = ssl.CERT_REQUIRED
        server.socket = context.wrap_socket(server.socket, server_side=True)

    stop = threading.Event()
    expiry_thread = None
    if worker is not None:
        expiry_thread = threading.Thread(
            target=_expiry_loop, args=(worker, stop, args.expiry_poll_seconds),
            name="campaign5-expiry", daemon=True)
        expiry_thread.start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        if expiry_thread is not None:
            expiry_thread.join(timeout=2.0)
    return 0


if __name__ == "__main__":
    # ``python3 -m oran.campaign5.producer`` runs this file as ``__main__``, but
    # live_worker raises ``from .producer import A1Conflict`` -- a second copy of
    # the module whose classes ``handle`` does not catch, so a deterministic 409
    # refusal left as a 500 the R1 client retries (2026-09-15 attempt 74).  One
    # module, one set of exception classes.
    import sys
    sys.modules.setdefault("oran.campaign5.producer", sys.modules[__name__])
    raise SystemExit(_main())
