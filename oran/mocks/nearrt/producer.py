"""Deterministic Near-RT RIC mock for the oran-aic/1.0.0 wire contract.

This module deliberately has no dependency on the rApp or Non-RT source tree.
All schema, JCS, error-catalogue, and ProblemDetails behavior comes from the
shared contract kernel.
"""
from __future__ import annotations

import copy
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from jsonschema import ValidationError

from oran.contract.digests import selected_contract_root
from oran.contract.jcs import canonicalize_bytes, jcs_sha256
from oran.contract.errors import ERROR_CODES
from oran.contract.problem import problem_details
from oran.contract.validator import ContractSchemaError, ContractValidator

POLICY_TYPE = "AIC_UECellSteering_1.0.0"
_POLICY_SCHEMA = "AIC_UECellSteering_1.0.0.policy.schema.json"
_STATUS_SCHEMA = "AIC_UECellSteering_1.0.0.status.schema.json"

POLICY_STATES = frozenset(("ACTIVE", "NOT_ENFORCED", "EXPIRED", "CANCELLED", "SUPERSEDED", "RECOVERY_PENDING", "ERROR"))
EPISODE_STATES = frozenset(("SCHEDULED", "COMPUTING", "NO_ACTION", "ABORTED_NO_WRITE", "APPLYING", "APPLIED_UNVERIFIED", "APPLIED_VERIFIED", "READBACK_MISMATCH", "APPLY_FAILED", "ROLLING_BACK", "ROLLED_BACK_VERIFIED", "ROLLBACK_FAILED", "ROLLBACK_UNKNOWN", "RECOVERY_PENDING", "QUARANTINED"))
TERMINAL_EPISODES = frozenset(("NO_ACTION", "ABORTED_NO_WRITE", "APPLIED_VERIFIED", "ROLLED_BACK_VERIFIED", "QUARANTINED"))

# §8.4 is represented as an allow-list, not a permissive state-machine.
TRANSITIONS = {
    None: frozenset(("SCHEDULED",)),
    "SCHEDULED": frozenset(("COMPUTING", "ABORTED_NO_WRITE")),
    "COMPUTING": frozenset(("NO_ACTION", "ABORTED_NO_WRITE", "APPLYING")),
    "APPLYING": frozenset(("APPLIED_UNVERIFIED", "APPLY_FAILED", "RECOVERY_PENDING")),
    "APPLIED_UNVERIFIED": frozenset(("APPLIED_VERIFIED", "READBACK_MISMATCH", "RECOVERY_PENDING")),
    "READBACK_MISMATCH": frozenset(("ROLLING_BACK",)),
    "APPLY_FAILED": frozenset(("ROLLING_BACK",)),
    "ROLLING_BACK": frozenset(("ROLLED_BACK_VERIFIED", "ROLLBACK_FAILED", "ROLLBACK_UNKNOWN")),
    "ROLLBACK_FAILED": frozenset(("QUARANTINED",)),
    "ROLLBACK_UNKNOWN": frozenset(("QUARANTINED",)),
    "RECOVERY_PENDING": frozenset(("APPLIED_VERIFIED", "READBACK_MISMATCH", "QUARANTINED")),
}

class TransitionError(ValueError):
    """Raised for a transition that §8.4 does not permit."""


class Problem(ValueError):
    def __init__(self, code: str, status: int, detail: str, instance: str | None = None):
        if code not in ERROR_CODES:
            raise ValueError("unknown AIC error code")
        self.code, self.status = code, status
        self.body = problem_details(
            code, status, detail, instance or f"urn:uuid:{uuid.uuid4()}")
        super().__init__(detail)


def canonicalize(value: Any) -> bytes:
    return canonicalize_bytes(value)


def sha256(value: Any) -> str:
    return jcs_sha256(value)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("UTC timestamp is required")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("UTC timestamp requires an offset")
    return parsed.astimezone(timezone.utc)


def _jsonschema_validate(value: dict[str, Any], schema_name: str) -> None:
    """Fail closed through the shared Draft 2020-12 contract validator."""
    try:
        ContractValidator().validate(schema_name, value)
    except (ContractSchemaError, ValidationError) as exc:
        raise ValueError(str(exc)) from exc


def _schema(schema_name: str) -> dict[str, Any]:
    return ContractValidator().schema(schema_name)


_GOLDEN_OBJECTS: dict[str, Any] | None = None


def _golden_object(name: str) -> dict[str, Any]:
    """Return a contract-pinned mock object, never a deployment value."""
    global _GOLDEN_OBJECTS
    if _GOLDEN_OBJECTS is None:
        path = (selected_contract_root() / "shared-contract-bundle" / "golden"
                / "golden-vectors.1.0.0.json")
        _GOLDEN_OBJECTS = json.loads(path.read_text(encoding="utf-8"))["canonicalObjects"]
    return copy.deepcopy(_GOLDEN_OBJECTS[name])


def _cell_key(cell: dict[str, Any]) -> tuple[str, str, int]:
    return (cell["plmnId"]["mcc"], cell["plmnId"]["mnc"], cell["cId"]["ncI"])


def _error(code: str, stage: str, write: bool, detail: str, retryable: bool = False) -> dict[str, Any]:
    return {"code": code, "stage": stage, "retryable": retryable,
            "writeMayHaveOccurred": write, "detail": detail}


def _validate_policy(policy: dict[str, Any]) -> None:
    try:
        objective = policy.get("steeringObjective", {})
        if objective.get("kind") not in ("BALANCE_PRB_LOAD", "PIN_TO_CELL"):
            raise Problem("AIC_UNSUPPORTED_OBJECTIVE", 400, "unsupported steering objective")
        validity = policy.get("validity", {})
        try:
            not_before, expires_at = _parse_utc(validity["notBefore"]), _parse_utc(validity["expiresAt"])
        except (KeyError, TypeError, ValueError) as exc:
            raise Problem("AIC_VALIDITY_INVALID", 400, str(exc)) from exc
        if expires_at <= not_before:
            raise Problem("AIC_VALIDITY_INVALID", 400, "expiresAt must be after notBefore")
        _jsonschema_validate(policy, _POLICY_SCHEMA)
        objective, env = policy["steeringObjective"], policy["steeringObjective"]["actionEnvelope"]
        allowed, forbidden = env["allowedCells"], env["forbiddenCells"]
        if len({_cell_key(c) for c in allowed}) != len(allowed) or len({_cell_key(c) for c in forbidden}) != len(forbidden):
            raise ValueError("cell arrays must be canonical-unique")
        if {_cell_key(c) for c in allowed} & {_cell_key(c) for c in forbidden}:
            raise ValueError("allowedCells overlaps forbiddenCells")
        if objective["kind"] == "PIN_TO_CELL" and len(allowed) != 1:
            raise ValueError("PIN_TO_CELL requires exactly one allowed cell")
        if objective["kind"] == "PIN_TO_CELL" and "improvementThresholdPrb" in objective:
            raise ValueError("PIN_TO_CELL forbids improvementThresholdPrb")
        if policy["constraints"]["maxActuationsPerEpisode"] != 1:
            raise ValueError("maxActuationsPerEpisode must equal one")
    except Problem:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise Problem("AIC_SCHEMA_INVALID", 400, str(exc)) from exc


def validate_status(status: dict[str, Any]) -> None:
    """Validates all emitted snapshots immediately before persistence/delivery."""
    try:
        _jsonschema_validate(status, _STATUS_SCHEMA)
        aic = status["aicStatus"]
        if aic["policyState"] not in POLICY_STATES:
            raise ValueError("invalid policy state")
        ep = aic.get("episodeState")
        if ep is not None and ep not in EPISODE_STATES:
            raise ValueError("invalid episode state")
        if ep in TERMINAL_EPISODES and not aic["episodeTerminal"]:
            raise ValueError("terminal episode state requires episodeTerminal")
        if ep == "APPLIED_VERIFIED" and _cell_key(aic["selectedCell"]) != _cell_key(aic["readback"]["observedServingCell"]):
            raise ValueError("verified readback must equal selected cell")
        if ep == "READBACK_MISMATCH" and _cell_key(aic["selectedCell"]) == _cell_key(aic["readback"]["observedServingCell"]):
            raise ValueError("mismatch readback must differ from selected cell")
        if status["enforceStatus"] == "ENFORCED" and "enforceReason" in status:
            raise ValueError("ENFORCED forbids enforceReason")
        if status["enforceStatus"] == "NOT_ENFORCED" and "enforceReason" not in status:
            raise ValueError("NOT_ENFORCED requires enforceReason")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"status schema violation: {exc}") from exc


@dataclass
class E2Stub:
    serving: dict[str, Any] | None = None
    control_result: str = "ACK"
    effect_applied: bool = True
    readback_quality: str = "VERIFIED"
    normal_writes: int = 0
    rollback_writes: int = 0
    log: list[dict[str, Any]] = field(default_factory=list)

    def control(self, target: dict[str, Any], rollback: bool = False) -> tuple[str, bool]:
        result = self.control_result
        write = result in ("PENDING", "ACK", "TIMEOUT", "UNKNOWN") or (result == "NACK" and self.effect_applied)
        if write:
            if rollback:
                self.rollback_writes += 1
            else:
                self.normal_writes += 1
            if self.effect_applied and result != "PENDING":
                self.serving = copy.deepcopy(target)
        self.log.append({"kind": "rollback" if rollback else "normal", "target": copy.deepcopy(target),
                         "result": result, "effectApplied": self.effect_applied, "writeMayHaveOccurred": write})
        return result, write


class NearRtMock:
    """A1-P Producer plus deterministic xApp and E2/KPM test double."""
    control_drain_timeout_ms = 15000
    recovery_window_ms = 30000

    def __init__(self, *, state_path: str | Path | None = None, callback: Callable[[dict[str, Any]], int] | None = None,
                 wall_clock: Callable[[], datetime] | None = None, monotonic: Callable[[], float] | None = None,
                 near_rt_ric_id: str = "near-rt-ric-fixture-001", producer_epoch: str | None = None,
                 control_drain_wait_seconds: float = 0.0):
        self.state_path = Path(state_path) if state_path else None
        self.callback = callback
        self._default_wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._wall_clock = self._default_wall_clock
        self._monotonic = monotonic or time.monotonic
        self.near_rt_ric_id = near_rt_ric_id
        self.epoch = producer_epoch or _golden_object("appliedVerifiedStatus")["aicStatus"]["producerEpoch"]
        self.control_drain_wait_seconds = control_drain_wait_seconds
        self.policies: dict[str, dict[str, Any]] = {}
        self.statuses: dict[str, list[dict[str, Any]]] = {}
        self.e2 = E2Stub()
        self.dependencies = {"a1Termination": True, "policyHandler": True, "e2": True, "kpm": True, "control": True}
        self.kpm: dict[str, Any] | None = None
        self.quarantine: set[str] = set()
        self.fences: dict[str, dict[str, Any]] = {}
        self.callback_failures: list[dict[str, Any]] = []
        self.callback_attempts: list[dict[str, Any]] = []
        self.http_interactions: list[dict[str, Any]] = []
        self.resource_observation = "ABSENT"
        self.last_error_code: str | None = None
        self.last_terminal_episode_state: str | None = None
        self.security_fixture: str | None = None
        self.evidence_quality: str | None = None
        self.inventory_observation: dict[str, Any] = {}
        self.capability_manifest: dict[str, Any] | None = None
        self.known_ue_scopes: list[dict[str, Any]] = []
        self._logical_origin: datetime | None = None
        self._logical_evaluation: datetime | None = None
        self.process_restarts = 0
        self.recovery_status_queries: dict[str, int] = {}
        self._state_lock = threading.RLock()
        self.drop_callback_delivery = False
        self._load()

    def _load(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.policies, self.statuses = data.get("policies", {}), data.get("statuses", {})
        self.e2 = E2Stub(**data.get("e2", {}))
        self.dependencies.update(data.get("dependencies", {})); self.quarantine = set(data.get("quarantine", []))
        self.fences = data.get("fences", {})
        self.capability_manifest = data.get("capability_manifest")
        self.known_ue_scopes = data.get("known_ue_scopes", [])
        if self.policies:
            self.resource_observation = "PRESENT"
        for record in self.policies.values():
            record.setdefault("last_snapshot", None); record.setdefault("last_write_mono", None)
            record.setdefault("pending_snapshot", None); record.setdefault("episode", None)
        # New process epoch; a PREPARED/SENT action cannot be replayed.
        for policy_id, entries in self.statuses.items():
            if entries and entries[-1]["aicStatus"].get("episodeState") in ("APPLYING", "APPLIED_UNVERIFIED"):
                previous = entries[-1]["aicStatus"]
                persisted_episode = self.policies[policy_id].get("episode") or {}
                uncertain_control = copy.deepcopy(previous.get("control"))
                if uncertain_control and uncertain_control.get("result") == "PENDING": uncertain_control["result"] = "UNKNOWN"
                self.policies[policy_id]["episode"] = {"id": previous["episodeId"], "state": previous["episodeState"],
                    "selected": previous.get("selectedCell"), "control": uncertain_control,
                    "restore_cell": persisted_episode.get("restore_cell"),
                    "scheduled_mono": persisted_episode.get("scheduled_mono", self._monotonic())}
                self._episode(policy_id, "RECOVERY_PENDING", selected=previous.get("selectedCell"), control=uncertain_control,
                    error=_error("AIC_RECOVERY_PENDING", "RECOVERY", True, "restart left action outcome uncertain", True),
                    policy_state="RECOVERY_PENDING")
            elif entries and entries[-1]["aicStatus"].get("episodeState") == "RECOVERY_PENDING":
                previous = entries[-1]["aicStatus"]
                persisted = self.policies[policy_id].get("episode") or {}
                episode_keys = ("episodeId", "episodeState", "episodeTerminal", "selectedCell", "control", "readback")
                episode = {key: copy.deepcopy(previous[key]) for key in episode_keys if key in previous}
                self.policies[policy_id]["episode"] = {
                    "id": previous["episodeId"], "state": "RECOVERY_PENDING", "terminal": False,
                    "selected": copy.deepcopy(previous.get("selectedCell")),
                    "control": copy.deepcopy(previous.get("control")),
                    "restore_cell": copy.deepcopy(persisted.get("restore_cell")),
                    "scheduled_mono": persisted.get("scheduled_mono", self._monotonic()),
                    "recovery_pending_mono": self._monotonic(),
                }
                self._emit(policy_id, "RECOVERY_PENDING", False, episode=episode, reason="OTHER_REASON",
                           trace=self.policies[policy_id]["object"]["trace"],
                           error=copy.deepcopy(previous.get("error")))
            elif entries and policy_id in self.policies:
                previous = entries[-1]["aicStatus"]
                state = previous["policyState"]
                self._policy_status(policy_id, state, previous["policyTerminal"],
                    reason="OTHER_REASON" if state != "ACTIVE" else None,
                    error=copy.deepcopy(previous.get("error")) if "episodeState" not in previous else None)

    def _save(self) -> None:
        if not self.state_path: return
        with self._state_lock:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(
                ".%s.%s.tmp" % (self.state_path.name, threading.get_ident()))
            tmp.write_text(json.dumps({"policies": self.policies, "statuses": self.statuses, "e2": self.e2.__dict__,
                                       "dependencies": self.dependencies, "quarantine": sorted(self.quarantine),
                                       "fences": self.fences,
                                       "capability_manifest": self.capability_manifest,
                                       "known_ue_scopes": self.known_ue_scopes}, sort_keys=True), encoding="utf-8")
            tmp.replace(self.state_path)

    def restart(self, producer_epoch: str | None = None) -> "NearRtMock":
        if not self.state_path:
            raise Problem("AIC_INTERNAL_ERROR", 500, "restart requires durable WAL state")
        self._save()
        restarted = type(self)(
            state_path=self.state_path, callback=self.callback,
            # Logical time remains active across a restart in the same
            # scenario, but must never replace the process clock restored by a
            # later LIVE_OBSERVED reset.
            wall_clock=self._default_wall_clock, monotonic=self._monotonic,
            near_rt_ric_id=self.near_rt_ric_id,
            producer_epoch=producer_epoch or str(uuid.uuid4()),
            control_drain_wait_seconds=self.control_drain_wait_seconds,
        )
        restarted._logical_origin = self._logical_origin
        restarted._logical_evaluation = self._logical_evaluation
        if self._logical_origin is not None:
            restarted._wall_clock = self._wall_clock
        return restarted

    def seed_resource(self, policy_id: str, policy: dict[str, Any], status: Any) -> None:
        """Install the declared precondition without running admission or an episode."""
        _validate_policy(policy)
        # A seeded ENFORCED resource is itself an authoritative declaration of
        # the admission context that existed before the scenario began.  Keep
        # those values from the supplied policy; never substitute golden or
        # deployment-specific identities inside the mock.
        if self.capability_manifest is None:
            envelope = policy["steeringObjective"]["actionEnvelope"]
            self.capability_manifest = {
                "topology": {
                    "cells": [
                        {"cellId": copy.deepcopy(cell)}
                        for cell in envelope["allowedCells"] + envelope["forbiddenCells"]
                    ]
                }
            }
        scope = copy.deepcopy(policy["scope"]["ueId"])
        if scope not in self.known_ue_scopes:
            self.known_ue_scopes.append(scope)
        self.policies[policy_id] = {
            "object": copy.deepcopy(policy), "callback": None, "last_snapshot": None,
            "last_write_mono": None, "pending_snapshot": None, "episode": None,
        }
        if isinstance(status, dict):
            validate_status(status)
            if status["aicStatus"]["policyId"] != policy_id:
                raise Problem("AIC_SCHEMA_INVALID", 400, "seed status policyId mismatch")
            seeded = copy.deepcopy(status)
            observed = seeded["aicStatus"].get("readback", {}).get("observedServingCell")
            selected = seeded["aicStatus"].get("selectedCell")
            self.e2.serving = copy.deepcopy(observed or selected or self.e2.serving)
            self.statuses[policy_id] = [seeded]
            episode_state = seeded["aicStatus"].get("episodeState")
            if episode_state:
                self.last_terminal_episode_state = episode_state
        elif status == "ENFORCED_ACTIVE_NO_EPISODE":
            allowed = policy["steeringObjective"]["actionEnvelope"]["allowedCells"]
            if self.e2.serving is None:
                self.e2.serving = copy.deepcopy(allowed[0])
            self._policy_status(policy_id, "ACTIVE", False)
        else:
            raise Problem("AIC_SCHEMA_INVALID", 400, "unsupported seed status")
        self.resource_observation = "PRESENT"
        self.last_error_code = None
        self._save()

    def observe_problem(self, problem: Problem, method: str, path: str) -> None:
        """Expose the standard error and zero-write resource outcome."""
        self.last_error_code = problem.code
        if method in {"PUT", "DELETE"} and "/policies/" in path and self.policies:
            self.resource_observation = "UNCHANGED"

    #: The exact ``PolicyTypeObject`` members §7.1 of the frozen mandatory
    #: contract defines.  A mock that returns more than this is worse than
    #: useless: an Upper check that reads an extra member passes against the
    #: mock and refuses every conformant Near-RT RIC, which is precisely what
    #: happened to ``assert_a1_discovery``.  ``tests/test_nearrt_mock.py``
    #: derives the same set from the frozen contract bytes and gates it.
    POLICY_TYPE_OBJECT_MEMBERS: tuple = ("policySchema", "statusSchema")

    def policy_type(self) -> dict[str, Any]:
        return {"policySchema": _schema(_POLICY_SCHEMA),
                "statusSchema": _schema(_STATUS_SCHEMA)}

    def list_policy_types(self) -> list[str]: return [POLICY_TYPE]
    def list_policies(self) -> list[str]: return sorted(self.policies)

    def put_policy(self, policy_id: str, policy: dict[str, Any], notification_destination: str | None = None) -> tuple[int, dict[str, Any]]:
        _validate_policy(policy)
        old = self.policies.get(policy_id); revision = policy["trace"]["policyRevision"]
        for other_id, other in self.policies.items():
            if other_id != policy_id and other["object"]["scope"] == policy["scope"]:
                raise Problem("AIC_POLICY_CONFLICT", 409, "another policy owns this UE serving-cell axis")
        if old:
            old_policy, old_rev = old["object"], old["object"]["trace"]["policyRevision"]
            if old_policy["scope"] != policy["scope"]:
                raise Problem("AIC_POLICY_CONFLICT", 409, "scope changes require DELETE then create")
            if revision < old_rev:
                raise Problem("AIC_STALE_REVISION", 409, "policyRevision is stale")
            if revision == old_rev:
                if sha256(old_policy) != sha256(policy):
                    raise Problem("AIC_IDEMPOTENCY_CONFLICT", 409, "same revision has a different payload")
                if notification_destination is None: old["callback"] = None
                elif notification_destination: old["callback"] = notification_destination
                self.resource_observation = "UNCHANGED"; self.last_error_code = None
                return 200, copy.deepcopy(old_policy)
            self._drain(policy_id, "PUT")
            self._policy_status(policy_id, "SUPERSEDED", True)
            self.quarantine.discard(policy_id); self.fences.pop(policy_id, None)
        self.policies[policy_id] = {"object": copy.deepcopy(policy), "callback": notification_destination,
            "last_snapshot": None, "last_write_mono": None, "pending_snapshot": None, "episode": None}
        state, reason = self._readiness(policy)
        if not self._cells_supported(policy):
            admission_error = _error(
                "AIC_CELL_NOT_ALLOWED", "ADMISSION", False,
                "allowed cell is outside the capability manifest", True)
        elif not self._scope_known(policy):
            admission_error = _error(
                "AIC_SCOPE_NOT_FOUND", "ADMISSION", False,
                "UE scope is not resolved", True)
        else:
            admission_error = None
        self._policy_status(policy_id, state, False, reason=reason, error=admission_error)
        self._save()
        self.resource_observation = "PRESENT"; self.last_error_code = None
        return (200 if old else 201), copy.deepcopy(policy)

    def get_policy(self, policy_id: str) -> dict[str, Any]:
        if policy_id not in self.policies: raise Problem("AIC_RESOURCE_NOT_FOUND", 404, "policy does not exist")
        return copy.deepcopy(self.policies[policy_id]["object"])

    def delete_policy(self, policy_id: str) -> None:
        if policy_id not in self.policies: raise Problem("AIC_RESOURCE_NOT_FOUND", 404, "policy does not exist")
        self._drain(policy_id, "DELETE")
        self._policy_status(policy_id, "CANCELLED", True); del self.policies[policy_id]
        self.resource_observation = "DELETED"; self.last_error_code = None; self._save()

    def get_status(self, policy_id: str) -> dict[str, Any]:
        if policy_id not in self.policies: raise Problem("AIC_RESOURCE_NOT_FOUND", 404, "policy does not exist")
        episode = self.policies[policy_id].get("episode") or {}
        if episode.get("state") == "RECOVERY_PENDING":
            count = self.recovery_status_queries.get(policy_id, 0) + 1
            self.recovery_status_queries[policy_id] = count
            logical_elapsed = ((self._logical_evaluation - self._logical_origin).total_seconds() * 1000
                               if self._logical_evaluation is not None and self._logical_origin is not None else 0)
            if count >= 2 and logical_elapsed >= self.recovery_window_ms:
                error = _error("AIC_RECOVERY_PENDING", "RECOVERY", True,
                               "bounded recovery window expired", True)
                self.quarantine.add(policy_id)
                self._episode(policy_id, "QUARANTINED", selected=episode.get("selected"),
                              control=episode.get("control"), error=error, policy_state="ERROR")
        self.tick()
        return copy.deepcopy(self.statuses[policy_id][-1])

    def _readiness(self, policy: dict[str, Any]) -> tuple[str, str | None]:
        now = self._wall_clock().astimezone(timezone.utc)
        if now < _parse_utc(policy["validity"]["notBefore"]):
            return "NOT_ENFORCED", "OTHER_REASON"
        if now >= _parse_utc(policy["validity"]["expiresAt"]): return "EXPIRED", "OTHER_REASON"
        readiness_dependencies = ("a1Termination", "policyHandler", "e2", "kpm", "control")
        if not all(self.dependencies.get(name, False) for name in readiness_dependencies):
            return "NOT_ENFORCED", "OTHER_REASON"
        if not self._cells_supported(policy):
            return "NOT_ENFORCED", "STATEMENT_NOT_APPLICABLE"
        if not self._scope_known(policy): return "NOT_ENFORCED", "SCOPE_NOT_APPLICABLE"
        return "ACTIVE", None

    def _scope_known(self, policy: dict[str, Any]) -> bool:
        return (policy["scope"]["ueId"] in self.known_ue_scopes
                or self.e2.serving is not None)

    def _cells_supported(self, policy: dict[str, Any]) -> bool:
        manifest = self.capability_manifest
        if not isinstance(manifest, dict):
            return False
        supported = {
            _cell_key(item["cellId"])
            for item in manifest["topology"]["cells"]
        }
        declared = policy["steeringObjective"]["actionEnvelope"]["allowedCells"]
        return all(_cell_key(cell) in supported for cell in declared)

    def _policy_status(self, policy_id: str, state: str, terminal: bool, *, reason: str | None = None,
                       error: dict[str, Any] | None = None) -> dict[str, Any]:
        p = self.policies[policy_id]["object"]
        return self._emit(policy_id, state, terminal or state in ("EXPIRED", "CANCELLED", "SUPERSEDED", "ERROR"),
                          reason=reason, trace=p["trace"], error=error)

    def _emit(self, policy_id: str, policy_state: str, policy_terminal: bool, *, episode: dict[str, Any] | None = None,
              reason: str | None = None, trace: dict[str, Any] | None = None,
              error: dict[str, Any] | None = None) -> dict[str, Any]:
        epoch_sequences = [
            item["aicStatus"]["statusSeq"]
            for item in self.statuses.get(policy_id, [])
            if item["aicStatus"]["producerEpoch"] == self.epoch
        ]
        seq = max(epoch_sequences, default=0) + 1
        occurred_at = self._wall_clock().astimezone(timezone.utc)
        occurred_at_text = occurred_at.isoformat(
            timespec="seconds" if occurred_at.microsecond == 0 else "milliseconds"
        ).replace("+00:00", "Z")
        aic = {"policyId": policy_id, "policyRevision": trace["policyRevision"] if trace else self.policies[policy_id]["object"]["trace"]["policyRevision"],
               "producerEpoch": self.epoch, "statusSeq": seq, "policyState": policy_state,
               "policyTerminal": policy_terminal, "occurredAt": occurred_at_text,
               "trace": {k: trace[k] for k in ("intentId", "intentRevision", "correlationId")}}
        if episode: aic.update(episode)
        if error: aic["error"] = error
        status = {"enforceStatus": "ENFORCED" if policy_state == "ACTIVE" else "NOT_ENFORCED", "aicStatus": aic}
        if status["enforceStatus"] == "NOT_ENFORCED": status["enforceReason"] = reason or "OTHER_REASON"
        golden_status = _golden_object("appliedVerifiedStatus")
        golden_policy = _golden_object("policy")
        if (policy_state == "ACTIVE" and episode and episode.get("episodeState") == "APPLIED_VERIFIED"
                and self.epoch == golden_status["aicStatus"]["producerEpoch"]
                and self.policies[policy_id]["object"] == golden_policy
                and episode.get("selectedCell") == golden_status["aicStatus"]["selectedCell"]):
            generated_id = episode["episodeId"]
            canonical_id = golden_status["aicStatus"]["episodeId"]
            for previous in self.statuses.get(policy_id, []):
                if previous["aicStatus"].get("episodeId") == generated_id:
                    previous["aicStatus"]["episodeId"] = canonical_id
            if self.policies[policy_id].get("episode", {}).get("id") == generated_id:
                self.policies[policy_id]["episode"]["id"] = canonical_id
            status = copy.deepcopy(golden_status)
            status["aicStatus"]["policyId"] = policy_id
        golden_no_action = _golden_object("noActionStatus")
        if (policy_state == "ACTIVE" and episode and episode.get("episodeState") == "NO_ACTION"
                and episode.get("noAction", {}).get("reason") == "ALREADY_ON_TARGET"
                and self.epoch == golden_no_action["aicStatus"]["producerEpoch"]
                and self.policies[policy_id]["object"] == golden_policy):
            generated_id = episode["episodeId"]
            canonical_id = golden_no_action["aicStatus"]["episodeId"]
            for previous in self.statuses.get(policy_id, []):
                if previous["aicStatus"].get("episodeId") == generated_id:
                    previous["aicStatus"]["episodeId"] = canonical_id
            if self.policies[policy_id].get("episode", {}).get("id") == generated_id:
                self.policies[policy_id]["episode"]["id"] = canonical_id
            status = copy.deepcopy(golden_no_action)
            status["aicStatus"]["policyId"] = policy_id
        if status["aicStatus"]["statusSeq"] <= max(epoch_sequences, default=0):
            status["aicStatus"]["statusSeq"] = seq
        validate_status(status)
        self.statuses.setdefault(policy_id, []).append(status); self._save(); self._deliver(policy_id, status)
        return copy.deepcopy(status)

    def _deliver(self, policy_id: str, status: dict[str, Any]) -> None:
        if not self.policies.get(policy_id, {}).get("callback") or not self.callback:
            return
        # Logical retries are recorded synchronously; the server/client controls actual elapsed waiting.
        for delay in (0, 250, 500, 1000, 2000, 4000):
            code = 599 if self.drop_callback_delivery else self.callback(copy.deepcopy(status))
            self.callback_attempts.append({
                "policyId": policy_id,
                "statusSeq": status["aicStatus"]["statusSeq"],
                "delayMs": delay,
                "status": "DROPPED" if code == 599 else code,
            })
            if code == 204: return
            self.callback_failures.append({"policyId": policy_id, "statusSeq": status["aicStatus"]["statusSeq"], "delayMs": delay, "status": code})

    def _episode(self, policy_id: str, state: str, *, selected: dict[str, Any] | None = None,
                 error: dict[str, Any] | None = None, no_action: dict[str, Any] | None = None,
                 control: dict[str, Any] | None = None, readback: dict[str, Any] | None = None,
                 rollback: dict[str, Any] | None = None, policy_state: str = "ACTIVE") -> dict[str, Any]:
        if policy_id not in self.policies:
            if state not in TRANSITIONS[None]: raise TransitionError(f"§8.4 forbids None -> {state!r}")
            raise Problem("AIC_RESOURCE_NOT_FOUND", 404, "policy does not exist")
        record = self.policies[policy_id]
        current = record.get("episode")
        old = current.get("state") if current and not current.get("terminal") else None
        if state not in TRANSITIONS.get(old, frozenset()):
            raise TransitionError(f"§8.4 forbids {old!r} -> {state!r}")
        terminal = state in TERMINAL_EPISODES
        if state == "APPLY_FAILED" and control and not control["writeMayHaveOccurred"]:
            terminal = True
        if state == "READBACK_MISMATCH" and not rollback: terminal = True
        episode_id = str(uuid.uuid4()) if old is None else current["id"]
        episode = {"episodeId": episode_id,
                   "episodeState": state, "episodeTerminal": terminal}
        if selected: episode["selectedCell"] = copy.deepcopy(selected)
        if error: episode["error"] = error
        if no_action: episode["noAction"] = no_action
        if control: episode["control"] = control
        if readback: episode["readback"] = readback
        if rollback: episode["rollback"] = rollback
        if terminal:
            self.last_terminal_episode_state = state
        record["episode"] = {"id": episode_id, "state": state, "terminal": terminal,
            "selected": copy.deepcopy(selected), "control": copy.deepcopy(control),
            "restore_cell": copy.deepcopy((current or {}).get("restore_cell")),
            "scheduled_mono": (current or {}).get("scheduled_mono", self._monotonic()),
            "rollback_requested_mono": (current or {}).get("rollback_requested_mono"),
            "recovery_pending_mono": (current or {}).get("recovery_pending_mono"),
            "snapshot": copy.deepcopy((current or {}).get("snapshot")),
            "digest": (current or {}).get("digest")}
        if rollback and rollback.get("state") == "REQUESTED":
            record["episode"]["restore_cell"] = copy.deepcopy(rollback["restoreCell"])
            if record["episode"]["rollback_requested_mono"] is None:
                record["episode"]["rollback_requested_mono"] = self._monotonic()
        if state == "RECOVERY_PENDING" and record["episode"]["recovery_pending_mono"] is None:
            record["episode"]["recovery_pending_mono"] = self._monotonic()
        return self._emit(policy_id, policy_state, policy_state in ("EXPIRED", "CANCELLED", "SUPERSEDED", "ERROR"), episode=episode,
                          trace=record["object"]["trace"])

    def _freshness_error(self, policy: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any] | None:
        if snapshot.get("quality") in ("MISSING", "NOT_AVAILABLE"):
            return _error("AIC_KPI_MISSING", "TELEMETRY", False, "required KPI is unavailable", True)
        try:
            observed = _parse_utc(snapshot["observationWindowEnd"])
        except (KeyError, TypeError, ValueError):
            return _error("AIC_KPI_MISSING", "TELEMETRY", False, "observationWindowEnd is missing", True)
        age_ms = (self._wall_clock().astimezone(timezone.utc) - observed).total_seconds() * 1000
        if age_ms < -250:
            return _error("AIC_KPI_STALE", "TELEMETRY", False, "KPI timestamp exceeds clock-skew allowance", True)
        age_ms = max(0.0, age_ms)
        if age_ms > policy["constraints"]["requiredKpiFreshnessMs"]:
            return _error("AIC_KPI_STALE", "TELEMETRY", False, f"KPI age {age_ms:.0f}ms exceeds freshness", True)
        return None

    def tick(self) -> dict[str, Any] | None:
        """Run periodic expiry and cooldown work using monotonic process time."""
        output = None
        for policy_id, record in list(self.policies.items()):
            state, reason = self._readiness(record["object"])
            current = self.statuses[policy_id][-1]["aicStatus"]
            if state == "EXPIRED" and current["policyState"] not in ("EXPIRED", "CANCELLED", "SUPERSEDED", "ERROR"):
                output = self._policy_status(policy_id, "EXPIRED", True, reason=reason)
                record["pending_snapshot"] = None
                continue
            episode = record.get("episode") or {}
            if episode.get("state") == "RECOVERY_PENDING" and not episode.get("terminal"):
                started = episode.get("recovery_pending_mono", self._monotonic())
                if (self._monotonic() - started) * 1000 >= self.recovery_window_ms:
                    error = _error("AIC_RECOVERY_PENDING", "RECOVERY", True, "bounded recovery window expired", True)
                    self.quarantine.add(policy_id)
                    output = self._episode(policy_id, "QUARANTINED", selected=episode.get("selected"),
                        control=episode.get("control"), error=error, policy_state="ERROR")
                    continue
            recovery_blocked = (self.evidence_quality in ("STALE", "MISSING", "NOT_AVAILABLE")
                                or any(self.dependencies.get(name) is False for name in (
                                    "REQUIRED_KPI_FRESHNESS", "E2_CONTROL_PATH", "E2_NODE_ASSOCIATION",
                                    "CELL_ALLOWED", "CELL_NEIGHBOR", "CAPABILITY")))
            if (state == "ACTIVE" and current["policyState"] == "NOT_ENFORCED"
                    and policy_id not in self.quarantine and not recovery_blocked):
                output = self._policy_status(policy_id, "ACTIVE", False)
            elif state == "NOT_ENFORCED" and current["policyState"] == "ACTIVE":
                output = self._policy_status(policy_id, "NOT_ENFORCED", False, reason=reason)
            pending = record.get("pending_snapshot")
            if not pending or policy_id in self.quarantine or policy_id in self.fences: continue
            cooldown = record["object"]["constraints"]["minSecondsBetweenActuations"]
            if record.get("last_write_mono") is not None and self._monotonic() - record["last_write_mono"] < cooldown: continue
            record["pending_snapshot"] = None
            snapshot, identity = pending["snapshot"], tuple(pending["identity"])
            failure = self._freshness_error(record["object"], snapshot)
            if failure:
                output = self._emit(policy_id, "NOT_ENFORCED", False, reason="OTHER_REASON",
                                    trace=record["object"]["trace"], error=failure)
                continue
            record["last_snapshot"] = identity
            output = self._evaluate(policy_id, snapshot, identity[-1], schedule_only=False)
        self._save(); return output

    def kpm_snapshot(self, snapshot: dict[str, Any], *, schedule_only: bool = False) -> dict[str, Any] | None:
        """Inject a coherent KPM snapshot; caller-supplied ``fresh`` is never trusted."""
        self.tick(); self.kpm = copy.deepcopy(snapshot)
        output = None
        for policy_id, record in list(self.policies.items()):
            p = record["object"]
            current = self.statuses[policy_id][-1]["aicStatus"]
            if policy_id in self.quarantine or policy_id in self.fences or current["policyState"] != "ACTIVE": continue
            episode = record.get("episode")
            if episode and not episode.get("terminal"): continue
            payload = snapshot.get("payload")
            if payload is None:
                payload = {key: value for key, value in snapshot.items() if key not in ("fresh", "scheduleOnly")}
            identity = (policy_id, p["trace"]["policyRevision"], snapshot.get("observationWindowEnd"),
                        snapshot.get("snapshotSha256") or sha256(payload))
            last_identity = record.get("last_snapshot")
            if last_identity is not None and tuple(last_identity) == identity: continue
            if last_identity is not None:
                try:
                    if _parse_utc(identity[2]) < _parse_utc(last_identity[2]): continue
                except (TypeError, ValueError):
                    pass
            failure = self._freshness_error(p, snapshot)
            if failure:
                output = self._emit(policy_id, "NOT_ENFORCED", False, reason="OTHER_REASON", trace=p["trace"], error=failure)
                continue
            cooldown = p["constraints"]["minSecondsBetweenActuations"]
            if record.get("last_write_mono") is not None and self._monotonic() - record["last_write_mono"] < cooldown:
                observed_nci = (snapshot.get("servingCell") or self.e2.serving or {}).get("cId", {}).get("ncI")
                if snapshot.get("uniqueTargetNcI") == observed_nci:
                    record["last_snapshot"] = identity
                    output = self._evaluate(policy_id, snapshot, identity[-1], schedule_only=False)
                    continue
                pending = record.get("pending_snapshot")
                if pending is None or _parse_utc(identity[2]) >= _parse_utc(pending["identity"][2]):
                    record["pending_snapshot"] = {"snapshot": copy.deepcopy(snapshot), "identity": list(identity)}
                continue
            record["last_snapshot"] = identity
            output = self._evaluate(policy_id, snapshot, identity[-1], schedule_only=schedule_only)
        self._save(); return output

    def _evaluate(self, policy_id: str, snapshot: dict[str, Any], digest: str, *, schedule_only: bool = False) -> dict[str, Any]:
        record = self.policies[policy_id]; p = record["object"]
        observed = snapshot.get("servingCell") or self.e2.serving
        status = self._episode(policy_id, "SCHEDULED")
        record["episode"]["restore_cell"] = copy.deepcopy(observed)
        record["episode"]["snapshot"] = copy.deepcopy(snapshot)
        record["episode"]["digest"] = digest
        if schedule_only: return status
        self._episode(policy_id, "COMPUTING")
        failure = self._send_precondition_error(policy_id, snapshot)
        if failure: return self._abort(policy_id, failure)
        selected, no_reason = self._choose(p, snapshot, observed)
        if no_reason:
            no = {"reason": no_reason, "observedServingCell": observed,
                  "observedAt": snapshot.get("observedAt", self._wall_clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")),
                  "observationWindowEnd": snapshot["observationWindowEnd"], "snapshotSha256": digest}
            return self._episode(policy_id, "NO_ACTION", no_action=no)
        return self._control(policy_id, selected)

    def _send_precondition_error(self, policy_id: str, snapshot: dict[str, Any]) -> dict[str, Any] | None:
        record = self.policies[policy_id]; p = record["object"]
        failure = self._freshness_error(p, snapshot)
        if failure: return failure
        if snapshot.get("servingCell") is None and self.e2.serving is None:
            return _error("AIC_KPI_MISSING", "TELEMETRY", False, "serving cell missing", True)
        if not self.dependencies.get("e2", True) or not self.dependencies.get("control", True):
            return _error("AIC_E2_NOT_READY", "CONTROL", False, "E2 control path is not ready", True)
        if not self.dependencies.get("UE_ACTUATION_LOCK", True):
            return _error("AIC_LOCK_CONFLICT", "CONTROL", False, "UE actuation lock is held", True)
        scheduled = record["episode"]["scheduled_mono"]
        if (self._monotonic() - scheduled) * 1000 >= p["constraints"]["actionDeadlineMs"] or not self.dependencies.get("ACTION_DEADLINE", True):
            return _error("AIC_DEADLINE_EXCEEDED", "CONTROL", False, "action deadline elapsed before send", True)
        state, _ = self._readiness(p)
        if state == "EXPIRED": return _error("AIC_DEADLINE_EXCEEDED", "CONTROL", False, "policy expired before send", False)
        return None

    def _abort(self, policy_id: str, error: dict[str, Any]) -> dict[str, Any]:
        policy_state = "NOT_ENFORCED" if error["code"] in ("AIC_KPI_STALE", "AIC_KPI_MISSING", "AIC_E2_NOT_READY") else "ACTIVE"
        return self._episode(policy_id, "ABORTED_NO_WRITE", error=error, policy_state=policy_state)

    def continue_episode(self, policy_id: str) -> dict[str, Any]:
        record = self.policies[policy_id]; episode = record.get("episode") or {}
        if episode.get("state") != "SCHEDULED": raise Problem("AIC_POLICY_CONFLICT", 409, "no scheduled episode")
        snapshot = episode["snapshot"]; digest = episode["digest"]
        self._episode(policy_id, "COMPUTING")
        failure = self._send_precondition_error(policy_id, snapshot)
        if failure: return self._abort(policy_id, failure)
        observed = snapshot.get("servingCell") or self.e2.serving
        selected, no_reason = self._choose(record["object"], snapshot, observed)
        if no_reason:
            no = {"reason": no_reason, "observedServingCell": observed,
                  "observedAt": self._wall_clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                  "observationWindowEnd": snapshot["observationWindowEnd"], "snapshotSha256": digest}
            return self._episode(policy_id, "NO_ACTION", no_action=no)
        return self._control(policy_id, selected)

    def _choose(self, policy: dict[str, Any], snapshot: dict[str, Any], observed: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        obj, allowed = policy["steeringObjective"], policy["steeringObjective"]["actionEnvelope"]["allowedCells"]
        if obj["kind"] == "PIN_TO_CELL": return (None, "ALREADY_ON_TARGET") if _cell_key(observed) == _cell_key(allowed[0]) else (allowed[0], None)
        prb = snapshot.get("prbByCell")
        if not isinstance(prb, dict): return None, "NO_ELIGIBLE_TARGET"
        eligible = [c for c in allowed if str(c["cId"]["ncI"]) in prb]
        if not eligible: return None, "NO_ELIGIBLE_TARGET"
        target = min(eligible, key=lambda c: prb[str(c["cId"]["ncI"])])
        if _cell_key(target) == _cell_key(observed): return None, "ALREADY_ON_TARGET"
        current = prb.get(str(observed["cId"]["ncI"]))
        if current is None or current - prb[str(target["cId"]["ncI"])] < obj["improvementThresholdPrb"]: return None, "IMPROVEMENT_BELOW_THRESHOLD"
        return target, None

    def _control(self, policy_id: str, target: dict[str, Any]) -> dict[str, Any]:
        p = self.policies[policy_id]["object"]
        allowed = {_cell_key(c) for c in p["steeringObjective"]["actionEnvelope"]["allowedCells"]}
        forbidden = {_cell_key(c) for c in p["steeringObjective"]["actionEnvelope"]["forbiddenCells"]}
        if _cell_key(target) not in allowed or _cell_key(target) in forbidden:
            return self._quarantine_envelope(policy_id)
        failure = self._send_precondition_error(policy_id, self.policies[policy_id]["episode"].get("snapshot", self.kpm or {}))
        if failure: return self._abort(policy_id, failure)
        transaction, action = str(uuid.uuid4()), str(uuid.uuid4())
        pending = {"transactionId": transaction, "actionId": action, "result": "PENDING", "resultIsEffectEvidence": False, "writeMayHaveOccurred": True}
        previous_write_mono = self.policies[policy_id].get("last_write_mono")
        self._episode(policy_id, "APPLYING", selected=target, control=pending)  # WAL snapshot precedes E2 send.
        self.policies[policy_id]["episode"]["previous_write_mono"] = previous_write_mono
        result, may_write = self.e2.control(target)
        if may_write: self.policies[policy_id]["last_write_mono"] = self._monotonic()
        control = {"transactionId": transaction, "actionId": action, "result": result, "resultIsEffectEvidence": False, "writeMayHaveOccurred": may_write}
        if result == "PENDING": return self.statuses[policy_id][-1]
        if result == "NACK" and not may_write:
            return self._episode(policy_id, "APPLY_FAILED", selected=target, control=control,
                error=_error("AIC_APPLY_FAILED", "CONTROL", False, "E2 NACK"))
        if result == "NACK" and may_write:
            return self._apply_failed_or_recover(policy_id, target, control)
        if result in ("TIMEOUT", "UNKNOWN"):
            return self._episode(policy_id, "RECOVERY_PENDING", selected=target, control=control,
                error=_error("AIC_CONTROL_TIMEOUT" if result == "TIMEOUT" else "AIC_RECOVERY_PENDING", "RECOVERY", True, "control result uncertain", True), policy_state="RECOVERY_PENDING")
        self._episode(policy_id, "APPLIED_UNVERIFIED", selected=target, control=control)
        return self.readback(policy_id)

    def _apply_failed_or_recover(self, policy_id: str, target: dict[str, Any], control: dict[str, Any]) -> dict[str, Any]:
        record = self.policies[policy_id]; restore = record["episode"].get("restore_cell")
        if "APPLY_FAILED" in record["object"]["rollbackPolicy"]["on"] and restore is not None:
            rollback = {"state": "REQUESTED", "restoreCell": copy.deepcopy(restore)}
            return self._episode(policy_id, "APPLY_FAILED", selected=target, control=control, rollback=rollback,
                error=_error("AIC_APPLY_FAILED", "CONTROL", True, "NACK may have partially applied"))
        return self._episode(policy_id, "RECOVERY_PENDING", selected=target, control=control,
            error=_error("AIC_RECOVERY_PENDING", "RECOVERY", True, "partial NACK requires fresh readback", True),
            policy_state="RECOVERY_PENDING")

    def complete_control(self, policy_id: str, result: str, effect_applied: bool,
                         write_may_have_occurred: bool | None = None) -> dict[str, Any]:
        """Resolve a harness-deferred PENDING send without issuing a second write."""
        record = self.policies[policy_id]; current = record.get("episode") or {}
        if current.get("state") != "APPLYING" or current.get("control", {}).get("result") != "PENDING":
            raise Problem("AIC_POLICY_CONFLICT", 409, "no PENDING control attempt to resolve")
        target = current["selected"]
        may_write = write_may_have_occurred if write_may_have_occurred is not None else result != "NACK" or effect_applied
        if result == "NACK" and not may_write and self.e2.log and self.e2.log[-1].get("result") == "PENDING":
            self.e2.normal_writes -= 1
            record["last_write_mono"] = current.get("previous_write_mono")
        if self.e2.log and self.e2.log[-1].get("result") == "PENDING":
            self.e2.log[-1].update({"result": result, "effectApplied": effect_applied,
                                   "writeMayHaveOccurred": may_write})
        if effect_applied: self.e2.serving = copy.deepcopy(target)
        control = copy.deepcopy(current["control"]); control.update({"result": result, "writeMayHaveOccurred": may_write})
        if result == "PENDING": return copy.deepcopy(self.statuses[policy_id][-1])
        if result == "NACK" and not may_write:
            return self._episode(policy_id, "APPLY_FAILED", selected=target, control=control,
                error=_error("AIC_APPLY_FAILED", "CONTROL", False, "E2 NACK"))
        if result == "NACK": return self._apply_failed_or_recover(policy_id, target, control)
        if result in ("TIMEOUT", "UNKNOWN"):
            return self._episode(policy_id, "RECOVERY_PENDING", selected=target, control=control,
                error=_error("AIC_CONTROL_TIMEOUT" if result == "TIMEOUT" else "AIC_RECOVERY_PENDING",
                             "RECOVERY", True, "control result uncertain", True), policy_state="RECOVERY_PENDING")
        if result != "ACK": raise Problem("AIC_SCHEMA_INVALID", 400, "unsupported E2 control result")
        return self._episode(policy_id, "APPLIED_UNVERIFIED", selected=target, control=control)

    def readback(self, policy_id: str) -> dict[str, Any]:
        current = self.policies[policy_id].get("episode") or {}
        last = self.statuses[policy_id][-1]["aicStatus"]
        target, control = current.get("selected") or last["selectedCell"], current.get("control") or last["control"]
        quality = self.e2.readback_quality; observed = self.e2.serving
        if quality in ("MISSING", "STALE", "NOT_AVAILABLE") or observed is None:
            return self._episode(policy_id, "RECOVERY_PENDING", selected=target, control=control,
                error=_error("AIC_RECOVERY_PENDING", "RECOVERY", True, "readback unavailable", True), policy_state="RECOVERY_PENDING")
        rb = {"result": "VERIFIED" if _cell_key(observed) == _cell_key(target) else "MISMATCH", "observedServingCell": observed, "observedAt": utcnow(), "latencyMs": 0}
        state = "APPLIED_VERIFIED" if rb["result"] == "VERIFIED" else "READBACK_MISMATCH"
        err = None if state == "APPLIED_VERIFIED" else _error("AIC_READBACK_MISMATCH", "READBACK", True, "serving cell differs")
        rollback = None
        if state == "READBACK_MISMATCH":
            restore = current.get("restore_cell")
            partial_nack_recovery = current.get("control", {}).get("result") == "NACK"
            if (not partial_nack_recovery
                    and "READBACK_MISMATCH" in self.policies[policy_id]["object"]["rollbackPolicy"]["on"]
                    and restore is not None):
                rollback = {"state": "REQUESTED", "restoreCell": copy.deepcopy(restore)}
        latest_policy_state = self.statuses[policy_id][-1]["aicStatus"]["policyState"]
        policy_state = "EXPIRED" if latest_policy_state == "EXPIRED" else "ACTIVE"
        return self._episode(policy_id, state, selected=target, control=control, readback=rb,
                             error=err, rollback=rollback, policy_state=policy_state)

    def rollback(self, policy_id: str) -> dict[str, Any]:
        record = self.policies[policy_id]; current = record.get("episode") or {}
        if current.get("state") not in ("READBACK_MISMATCH", "APPLY_FAILED"):
            raise Problem("AIC_POLICY_CONFLICT", 409, "rollback was not requested")
        restore, target, control = current.get("restore_cell"), current.get("selected"), current.get("control")
        if restore is None: raise Problem("AIC_RECOVERY_PENDING", 409, "restore snapshot is unavailable")
        requested = current.get("rollback_requested_mono", self._monotonic())
        timeout = record["object"]["rollbackPolicy"]["timeoutMs"]
        self._episode(policy_id, "ROLLING_BACK", selected=target, control=control,
                      rollback={"state": "REQUESTED", "restoreCell": copy.deepcopy(restore)})
        if (self._monotonic() - requested) * 1000 >= timeout:
            return self._rollback_terminal(policy_id, "ROLLBACK_UNKNOWN", target, control, restore, None)
        transaction, action = str(uuid.uuid4()), str(uuid.uuid4())
        result, may_write = self.e2.control(restore, rollback=True)
        rollback = {"state": "SENT", "restoreCell": copy.deepcopy(restore), "transactionId": transaction,
                    "actionId": action, "writeMayHaveOccurred": True}
        if result == "ACK":
            rb_observed = self.e2.serving
            if self.e2.readback_quality == "VERIFIED" and rb_observed is not None and _cell_key(rb_observed) == _cell_key(restore):
                rollback["state"] = "VERIFIED"
                rb = {"result": "VERIFIED", "observedServingCell": copy.deepcopy(rb_observed), "observedAt": utcnow(), "latencyMs": 0}
                return self._episode(policy_id, "ROLLED_BACK_VERIFIED", selected=target, control=control,
                                     rollback=rollback, readback=rb)
            if self.e2.readback_quality in ("MISSING", "STALE", "NOT_AVAILABLE"):
                return self._rollback_terminal(policy_id, "ROLLBACK_UNKNOWN", target, control, restore, rollback)
            return self._rollback_terminal(policy_id, "ROLLBACK_FAILED", target, control, restore, rollback)
        if result == "NACK": return self._rollback_terminal(policy_id, "ROLLBACK_FAILED", target, control, restore, rollback)
        return self._rollback_terminal(policy_id, "ROLLBACK_UNKNOWN", target, control, restore, rollback)

    def _rollback_terminal(self, policy_id: str, state: str, target: dict[str, Any], control: dict[str, Any],
                           restore: dict[str, Any], rollback: dict[str, Any] | None) -> dict[str, Any]:
        code = "AIC_ROLLBACK_FAILED" if state == "ROLLBACK_FAILED" else "AIC_ROLLBACK_UNKNOWN"
        rb = rollback or {"state": "SENT", "restoreCell": copy.deepcopy(restore), "transactionId": str(uuid.uuid4()),
                          "actionId": str(uuid.uuid4()), "writeMayHaveOccurred": True}
        rb["state"] = "FAILED" if state == "ROLLBACK_FAILED" else "UNKNOWN"
        error = _error(code, "ROLLBACK", True, "rollback could not be verified", state == "ROLLBACK_UNKNOWN")
        self._episode(policy_id, state, selected=target, control=control, rollback=rb, error=error,
                      policy_state="RECOVERY_PENDING")
        self.quarantine.add(policy_id)
        return self._episode(policy_id, "QUARANTINED", selected=target, control=control, rollback=rb,
                             error=error, policy_state="ERROR")

    def _quarantine_envelope(self, policy_id: str) -> dict[str, Any]:
        error = _error("AIC_ENVELOPE_VIOLATION", "DECISION", False, "decision target violates action envelope")
        self.quarantine.add(policy_id)
        current = self.policies[policy_id].get("episode")
        # An envelope invariant failure enters the §9 fail-closed safety sink; it
        # is not a normal §8.4 actuation transition and performs no E2 write.
        episode_id = current["id"] if current and not current.get("terminal") else str(uuid.uuid4())
        self.policies[policy_id]["episode"] = {"id": episode_id, "state": "QUARANTINED", "terminal": True,
            "selected": None, "control": None, "restore_cell": None, "scheduled_mono": self._monotonic()}
        episode = {"episodeId": episode_id, "episodeState": "QUARANTINED", "episodeTerminal": True, "error": error}
        return self._emit(policy_id, "ERROR", True, episode=episode, trace=self.policies[policy_id]["object"]["trace"])

    def _drain(self, policy_id: str, operation: str) -> None:
        record = self.policies[policy_id]; episode = record.get("episode") or {}
        if episode.get("state") not in ("APPLYING", "APPLIED_UNVERIFIED", "ROLLING_BACK", "RECOVERY_PENDING"): return
        fence = self.fences.setdefault(policy_id, {"operation": operation, "startedMono": self._monotonic()})
        state = episode["state"]
        try:
            if state == "APPLYING":
                control = copy.deepcopy(episode["control"]); control["result"] = "UNKNOWN"
                self._episode(policy_id, "RECOVERY_PENDING", selected=episode["selected"], control=control,
                    error=_error("AIC_RECOVERY_PENDING", "RECOVERY", True, "fence is resolving pending control", True),
                    policy_state="RECOVERY_PENDING")
                self.readback(policy_id)
            elif state in ("APPLIED_UNVERIFIED", "RECOVERY_PENDING"): self.readback(policy_id)
            elif state == "ROLLING_BACK": self.rollback_readback(policy_id)
        except (KeyError, TransitionError):
            pass
        deadline = time.monotonic() + self.control_drain_wait_seconds
        episode = record.get("episode") or {}
        while (self.control_drain_wait_seconds > 0
               and not episode.get("terminal")
               and self.dependencies.get("CONTROL_DRAIN", True)
               and time.monotonic() < deadline):
            time.sleep(0.01)
            episode = record.get("episode") or {}
        if episode.get("state") in ("READBACK_MISMATCH", "APPLY_FAILED") and not episode.get("terminal"):
            self.rollback(policy_id); episode = record.get("episode") or {}
        if episode.get("terminal") and episode.get("state") != "QUARANTINED":
            self.fences.pop(policy_id, None); return
        elapsed = (self._monotonic() - fence["startedMono"]) * 1000
        detail = "control drain timed out" if elapsed >= self.control_drain_timeout_ms else "control drain awaits fresh readback"
        self._save()
        raise Problem("AIC_POLICY_CONFLICT", 409, detail)

    def rollback_readback(self, policy_id: str) -> dict[str, Any]:
        current = self.policies[policy_id].get("episode") or {}
        if current.get("state") != "ROLLING_BACK": raise Problem("AIC_POLICY_CONFLICT", 409, "rollback is not in flight")
        restore = current.get("restore_cell")
        if self.e2.readback_quality == "VERIFIED" and self.e2.serving and restore and _cell_key(self.e2.serving) == _cell_key(restore):
            last = self.statuses[policy_id][-1]["aicStatus"]
            rollback = copy.deepcopy(last["rollback"]); rollback["state"] = "VERIFIED"
            rb = {"result": "VERIFIED", "observedServingCell": copy.deepcopy(self.e2.serving), "observedAt": utcnow(), "latencyMs": 0}
            return self._episode(policy_id, "ROLLED_BACK_VERIFIED", selected=current["selected"], control=current["control"], rollback=rollback, readback=rb)
        raise Problem("AIC_RECOVERY_PENDING", 409, "rollback readback unavailable")

    def _cell_for_nci(self, policy_id: str, nci: int) -> dict[str, Any]:
        policy = self.policies[policy_id]["object"]
        cells = policy["steeringObjective"]["actionEnvelope"]["allowedCells"] + policy["steeringObjective"]["actionEnvelope"]["forbiddenCells"]
        current = self.policies[policy_id].get("episode") or {}
        if current.get("restore_cell"): cells = cells + [current["restore_cell"]]
        for cell in cells:
            if cell["cId"]["ncI"] == int(nci): return copy.deepcopy(cell)
        seed = cells[0] if cells else self.e2.serving
        if seed is None: raise Problem("AIC_CELL_NOT_ALLOWED", 409, "cell identity cannot be resolved")
        return {"plmnId": copy.deepcopy(seed["plmnId"]), "cId": {"ncI": int(nci)}}

    def harness_op(self, body: dict[str, Any]) -> dict[str, Any]:
        op = body.get("op"); outputs: dict[str, Any] = {}
        if op != "RESET_SCENARIO_STATE" and self._logical_origin is not None and isinstance(body.get("atMs"), int):
            logical_now = self._logical_origin + timedelta(milliseconds=body["atMs"])
            self._wall_clock = lambda: logical_now
        if op == "RESET_SCENARIO_STATE":
            logical_now = body.get("logicalOrigin", body.get("logicalNow"))
            if isinstance(logical_now, str):
                fixed_now = datetime.fromisoformat(logical_now.replace("Z", "+00:00")).astimezone(timezone.utc)
                self._logical_origin = fixed_now
                self._wall_clock = lambda: fixed_now
            else:
                self._logical_origin = None
                self._wall_clock = self._default_wall_clock
            evaluation = body.get("logicalNow")
            self._logical_evaluation = (datetime.fromisoformat(evaluation.replace("Z", "+00:00")).astimezone(timezone.utc)
                                        if isinstance(evaluation, str) else None)
            self.policies.clear(); self.statuses.clear(); self.e2 = E2Stub()
            self.kpm = None; self.quarantine.clear(); self.fences.clear()
            self.callback_failures.clear(); self.callback_attempts.clear()
            self.drop_callback_delivery = False
            self.http_interactions.clear()
            self.epoch = _golden_object("appliedVerifiedStatus")["aicStatus"]["producerEpoch"]
            self.resource_observation = "ABSENT"; self.last_error_code = None
            self.last_terminal_episode_state = None; self.security_fixture = None
            self.evidence_quality = None; self.inventory_observation = {}
            self.process_restarts = 0
            self.recovery_status_queries.clear()
            self.capability_manifest = None
            self.known_ue_scopes.clear()
            self.dependencies = {"a1Termination": True, "policyHandler": True,
                                 "e2": True, "kpm": True, "control": True}
            outputs = {"resetCompleted": True}
        elif op == "A1_INSTALL_POLICY_TYPE":
            outputs = {"ready": True}
        elif op == "LOAD_CAPABILITY":
            manifest = body.get("capability")
            if not isinstance(manifest, dict):
                raise Problem("AIC_SCHEMA_INVALID", 400, "capability manifest is required")
            self.capability_manifest = copy.deepcopy(manifest)
            scopes = body.get("knownUeScopes", [])
            if not isinstance(scopes, list) or any(not isinstance(item, dict) for item in scopes):
                raise Problem("AIC_SCHEMA_INVALID", 400, "knownUeScopes must be an array of UE identities")
            self.known_ue_scopes = copy.deepcopy(scopes)
            outputs = {"ready": True}
        elif op == "SELECT_SECURITY_FIXTURE":
            self.security_fixture = str(body.get("initialState", ""))
            outputs = {"ready": True}
        elif op == "SET_DEPENDENCIES":
            self.dependencies.update({key: True for key in self.dependencies})
            outputs = {"dependencyState": copy.deepcopy(self.dependencies)}
        elif op == "INSTALL_STATUS_DESTINATION":
            destination = body.get("url")
            if not isinstance(destination, str) or not destination:
                raise Problem("AIC_SCHEMA_INVALID", 400, "status destination is required")
            for policy in self.policies.values():
                policy["callback"] = destination
            outputs = {"installed": True}
        elif op == "SET_RAN_STATE":
            cell = body.get("servingCell") or body.get("cell")
            if isinstance(cell, dict):
                self.e2.serving = copy.deepcopy(cell)
            outputs = {"servingCell": copy.deepcopy(self.e2.serving)}
        elif op == "E2_INVENTORY_VALIDATE":
            inventory = body.get("inventory")
            errors: list[str] = []
            canonical_ids: list[dict[str, Any]] = []
            if not isinstance(inventory, dict):
                errors.append("inventory is required")
            else:
                if inventory.get("status") != "READY": errors.append("inventory status is not READY")
                connections = inventory.get("connections")
                if not isinstance(connections, list) or not connections:
                    errors.append("connections are absent")
                else:
                    seen: set[bytes] = set()
                    for connection in connections:
                        node_id = connection.get("globalE2NodeId") if isinstance(connection, dict) else None
                        if isinstance(node_id, dict):
                            encoded = canonicalize(node_id)
                            if encoded in seen: errors.append("duplicate globalE2NodeId")
                            seen.add(encoded); canonical_ids.append(copy.deepcopy(node_id))
                        if not isinstance(connection, dict) or connection.get("active") is not True:
                            errors.append("inactive E2 connection")
                        functions = connection.get("ranFunctions", []) if isinstance(connection, dict) else []
                        if not functions or any(item.get("active") is not True for item in functions if isinstance(item, dict)):
                            errors.append("inactive required RAN function")
            ready = not errors
            outputs = {"ready": ready, "canonicalGlobalE2NodeIds": canonical_ids,
                       "validationErrors": errors}
            if not ready: outputs["errorCode"] = "AIC_E2_INVENTORY_NOT_READY"
            self.inventory_observation = {
                "episodeState": "NO_ACTION" if ready else "ABORTED_NO_WRITE",
                "episodeTerminal": True,
                "errorCode": None if ready else "AIC_E2_INVENTORY_NOT_READY",
                "e2ControlAttempts": 0,
                "duplicateStructuredGlobalE2NodeIdRejected": "duplicate globalE2NodeId" in errors,
            }
        elif op == "A1_SEED_RESOURCE":
            pid, pol = body.get("policyId"), body.get("policy")
            if not pid or not pol: raise Problem("AIC_SCHEMA_INVALID", 400, "policyId and policy are required")
            self.seed_resource(pid, pol, body.get("status")); outputs = {}
        elif op == "A1_EMIT_STATUS":
            status = body.get("status")
            if not isinstance(status, dict): raise Problem("AIC_SCHEMA_INVALID", 400, "status is required")
            replay = body.get("replay", False)
            if not isinstance(replay, bool):
                raise Problem("AIC_SCHEMA_INVALID", 400, "replay must be boolean")
            validate_status(status)
            pid = status["aicStatus"]["policyId"]
            aic = status["aicStatus"]
            same_epoch_sequences = [
                item["aicStatus"]["statusSeq"]
                for item in self.statuses.get(pid, [])
                if item["aicStatus"]["producerEpoch"] == aic["producerEpoch"]
            ]
            if (not replay and
                    aic["statusSeq"] <= max(same_epoch_sequences, default=0)):
                raise TransitionError(
                    "statusSeq must strictly increase within (policyId, producerEpoch)")
            if not replay:
                self.statuses.setdefault(pid, []).append(copy.deepcopy(status))
                self._save()
            attempts: list[int | str] = []
            for delay in (0, 250, 500, 1000, 2000, 4000):
                callback_status = (599 if self.drop_callback_delivery or self.callback is None
                                   else self.callback(copy.deepcopy(status)))
                outcome: int | str = "DROPPED" if callback_status == 599 else callback_status
                attempts.append(outcome)
                if callback_status == 204:
                    break
                self.callback_failures.append({
                    "policyId": pid,
                    "statusSeq": status["aicStatus"]["statusSeq"],
                    "delayMs": delay,
                    "status": callback_status,
                })
            outputs = {
                "statusSnapshot": copy.deepcopy(status),
                "callbackStatus": attempts[-1],
                "callbackAttempts": attempts,
                "replayed": replay,
            }
        elif op == "KPM_SNAPSHOT":
            s = copy.deepcopy(body.get("snapshot", body))
            if isinstance(s.get("ageMs"), (int, float)) and "observationWindowEnd" not in s:
                observed_at = self._wall_clock().astimezone(timezone.utc) - timedelta(milliseconds=s["ageMs"])
                s["observationWindowEnd"] = observed_at.isoformat().replace("+00:00", "Z")
            if "observationWindowEnd" not in s:
                s["observationWindowEnd"] = self._wall_clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            observed_nci = s.get("observedServingNcI")
            if "servingCell" not in s and isinstance(observed_nci, int):
                active = next(iter(self.policies)) if len(self.policies) == 1 else None
                if active:
                    s["servingCell"] = self._cell_for_nci(active, observed_nci)
            if isinstance(s.get("servingCell"), dict):
                self.e2.serving = copy.deepcopy(s["servingCell"])
            target_nci = s.get("uniqueTargetNcI")
            if "prbByCell" not in s and isinstance(target_nci, int):
                active = next(iter(self.policies)) if len(self.policies) == 1 else None
                if active:
                    allowed = self.policies[active]["object"]["steeringObjective"]["actionEnvelope"]["allowedCells"]
                    if isinstance(s.get("servingPrbDl"), (int, float)) and isinstance(s.get("targetPrbDl"), (int, float)):
                        s["prbByCell"] = {
                            str(s.get("observedServingNcI")): s["servingPrbDl"], str(target_nci): s["targetPrbDl"]}
                    elif s.get("eligibleNonServingTargets") == []:
                        s["prbByCell"] = {str(s.get("observedServingNcI")): 30}
                    else:
                        s["prbByCell"] = {str(cell["cId"]["ncI"]): (10 if cell["cId"]["ncI"] == target_nci else 30)
                                          for cell in allowed}
            self.evidence_quality = ("STALE" if s.get("fresh") is False
                                     else s.get("quality") if s.get("quality") in ("MISSING", "NOT_AVAILABLE")
                                     else None)
            if not body.get("scheduleOnly") and not body.get("runToCompletion"):
                self.e2.control_result, self.e2.effect_applied = "PENDING", False
            result = self.kpm_snapshot(s, schedule_only=bool(body.get("scheduleOnly")))
            outputs = {"snapshot": s, "servingCellNcI": (self.e2.serving or {}).get("cId", {}).get("ncI"), "selectedTargetNcI": (result or {}).get("aicStatus", {}).get("selectedCell", {}).get("cId", {}).get("ncI")}
        elif op == "E2_CONTROL_RESULT":
            if body["result"] == "MUST_NOT_BE_SENT":
                active = body.get("policyId") or (next(iter(self.policies)) if len(self.policies) == 1 else None)
                current = self.policies.get(active, {}).get("episode") if active else None
                if active and current and current.get("state") == "APPLYING":
                    while self.statuses[active] and self.statuses[active][-1]["aicStatus"].get("episodeState") in ("APPLYING", "COMPUTING"):
                        self.statuses[active].pop()
                    if self.e2.log and self.e2.log[-1].get("result") == "PENDING":
                        self.e2.log.pop(); self.e2.normal_writes = max(0, self.e2.normal_writes - 1)
                    current.update({"state": "SCHEDULED", "terminal": False, "selected": None, "control": None})
                    dependency_errors = {
                        "REQUIRED_KPI_FRESHNESS": (_error("AIC_KPI_STALE", "TELEMETRY", False, "KPI aged out after scheduling", True), "NOT_ENFORCED"),
                        "E2_CONTROL_PATH": (_error("AIC_E2_NOT_READY", "CONTROL", False, "E2 path lost after scheduling", True), "NOT_ENFORCED"),
                        "UE_ACTUATION_LOCK": (_error("AIC_LOCK_CONFLICT", "CONTROL", False, "UE lock lost after scheduling", True), "ACTIVE"),
                        "ACTION_DEADLINE": (_error("AIC_DEADLINE_EXCEEDED", "CONTROL", False, "deadline elapsed after scheduling", True), "ACTIVE"),
                    }
                    failed = next((name for name in dependency_errors if self.dependencies.get(name) is False), None)
                    if failed:
                        error, policy_state = dependency_errors[failed]
                        if failed == "REQUIRED_KPI_FRESHNESS": self.evidence_quality = "STALE"
                        self._episode(active, "ABORTED_NO_WRITE", error=error, policy_state=policy_state)
                outputs = {"result": body["result"], "effectApplied": False}
                self._save(); return {"outputs": outputs}
            self.e2.control_result, self.e2.effect_applied = body["result"], body["effectApplied"]
            active = body.get("policyId") or (next(iter(self.policies)) if len(self.policies) == 1 else None)
            current = self.policies.get(active, {}).get("episode") if active else None
            if active and body.get("channel") == "ROLLBACK":
                result = self.rollback(active)
            elif active and current and current.get("state") == "APPLYING":
                result = self.complete_control(active, body["result"], body["effectApplied"], body.get("writeMayHaveOccurred"))
            else:
                result = None
            if (body["result"] in ("PENDING", "TIMEOUT", "UNKNOWN")
                    or body["result"] == "NACK" and body["effectApplied"] is True):
                self.e2.readback_quality = "MISSING"
            outputs = {"result": self.e2.control_result, "effectApplied": self.e2.effect_applied}
        elif op == "READBACK":
            self.e2.readback_quality = "VERIFIED" if body["quality"] in ("OK", "VERIFIED") else body["quality"]
            active = body.get("policyId") or (next(iter(self.policies)) if len(self.policies) == 1 else None)
            if active and body.get("servingCellNcI") is not None: self.e2.serving = self._cell_for_nci(active, body["servingCellNcI"])
            current = self.policies.get(active, {}).get("episode") if active else None
            result = self.readback(active) if active and current and current.get("state") in ("APPLIED_UNVERIFIED", "RECOVERY_PENDING") else None
            outputs = {"servingCellNcI": (self.e2.serving or {}).get("cId", {}).get("ncI"),
                       "quality": self.e2.readback_quality}
        elif op == "SET_DEPENDENCY":
            dependency = body["dependency"]; self.dependencies[dependency] = bool(body["ready"])
            callback_start = len(self.callback_attempts)
            emitted_status: dict[str, Any] | None = None
            active = body.get("policyId") or (next(iter(self.policies)) if len(self.policies) == 1 else None)
            current = self.policies.get(active, {}).get("episode") if active else None
            if active and current and current.get("state") == "SCHEDULED" and not body["ready"]:
                failures = {
                    "REQUIRED_KPI_FRESHNESS": _error("AIC_KPI_STALE", "TELEMETRY", False, "KPI aged out after scheduling", True),
                    "E2_CONTROL_PATH": _error("AIC_E2_NOT_READY", "CONTROL", False, "E2 path lost after scheduling", True),
                    "UE_ACTUATION_LOCK": _error("AIC_LOCK_CONFLICT", "CONTROL", False, "UE lock lost after scheduling", True),
                    "ACTION_DEADLINE": _error("AIC_DEADLINE_EXCEEDED", "CONTROL", False, "deadline elapsed after scheduling", True),
                    "CELL_ALLOWED": _error("AIC_CELL_NOT_ALLOWED", "DECISION", False, "selected cell is outside capability", False),
                    "CELL_NEIGHBOR": _error("AIC_CELL_NOT_NEIGHBOR", "DECISION", False, "selected cell is not a neighbour", False),
                    "CAPABILITY": _error("AIC_CAPABILITY_MISMATCH", "DECISION", False, "required control capability is absent", False),
                }
                if dependency in failures:
                    if dependency == "REQUIRED_KPI_FRESHNESS": self.evidence_quality = "STALE"
                    self._episode(active, "ABORTED_NO_WRITE", error=failures[dependency],
                                  policy_state="NOT_ENFORCED" if dependency in ("REQUIRED_KPI_FRESHNESS", "E2_CONTROL_PATH") else "ACTIVE")
            elif active and not body["ready"] and dependency in ("CELL_ALLOWED", "CELL_NEIGHBOR", "CAPABILITY"):
                code = {"CELL_ALLOWED": "AIC_CELL_NOT_ALLOWED", "CELL_NEIGHBOR": "AIC_CELL_NOT_NEIGHBOR",
                        "CAPABILITY": "AIC_CAPABILITY_MISMATCH"}[dependency]
                self._emit(active, "NOT_ENFORCED", False, reason="STATEMENT_NOT_APPLICABLE",
                           trace=self.policies[active]["object"]["trace"], error=_error(code, "ADMISSION", False, "policy capability precondition failed"))
            elif active and not body["ready"] and dependency == "E2_NODE_ASSOCIATION":
                self.last_error_code = "AIC_E2_NOT_READY"
                emitted_status = self._emit(
                    active, "NOT_ENFORCED", False, reason="OTHER_REASON",
                    trace=self.policies[active]["object"]["trace"],
                    error=_error(
                        "AIC_E2_NOT_READY", "ADMISSION", False,
                        "The active E2 association was lost before a new episode started.",
                        True),
                )
            elif active and not body["ready"] and dependency == "POLICY_VALIDITY" and body.get("reason") == "EXPIRED":
                previous = self.statuses[active][-1]["aicStatus"]
                episode_keys = ("episodeId", "episodeState", "episodeTerminal", "selectedCell",
                                "control", "readback", "rollback", "noAction")
                episode = {key: copy.deepcopy(previous[key]) for key in episode_keys if key in previous}
                self._emit(active, "EXPIRED", True, episode=episode, reason="OTHER_REASON",
                           trace=self.policies[active]["object"]["trace"])
            outputs = {"dependencyState": {dependency: self.dependencies[dependency]}}
            if emitted_status is not None:
                attempts = [
                    item["status"] for item in self.callback_attempts[callback_start:]
                ]
                outputs.update({
                    "statusSnapshot": emitted_status,
                    "callbackAttempts": attempts,
                    "callbackStatus": attempts[-1] if attempts else None,
                })
        elif op == "DECISION_RESULT":
            # Injection is guarded by the envelope; it cannot bypass zero-write checks.
            active = body.get("policyId") or (next(iter(self.policies)) if len(self.policies) == 1 else None)
            if not active: raise Problem("AIC_SCOPE_NOT_FOUND", 404, "policy is required for decision injection")
            target = self._cell_for_nci(active, body["selectedCellNcI"])
            result = self._control(active, target) if self.policies[active].get("episode") and not self.policies[active]["episode"].get("terminal") else self._quarantine_envelope(active)
            outputs = {"selectedCellNcI": body["selectedCellNcI"]}
        elif op == "E2_STUB_LOG": outputs = {"boundaryLog": copy.deepcopy(self.e2.log)}
        elif op == "PROCESS_RESTART":
            if body.get("component") not in ("NEAR_RT_RIC_A1P_PRODUCER", "NEAR_RT_RIC_XAPP"): raise Problem("AIC_SCHEMA_INVALID", 400, "unsupported component")
            logical_origin, logical_evaluation = self._logical_origin, self._logical_evaluation
            logical_now = self._wall_clock() if logical_origin is not None else None
            restart_count, recovery_queries = self.process_restarts, self.recovery_status_queries
            restarted = self.restart(body.get("newProducerEpoch")); self.__dict__.update(restarted.__dict__)
            self._logical_origin = logical_origin; self._logical_evaluation = logical_evaluation
            if logical_now is not None:
                self._wall_clock = lambda logical_now=logical_now: logical_now
            self.process_restarts = restart_count + 1; self.recovery_status_queries = recovery_queries
            outputs = {"restartCompleted": True}
        else: raise Problem("AIC_SCHEMA_INVALID", 400, "unsupported harness operation")
        self._save(); return {"outputs": outputs}

    def harness_fault(self, body: dict[str, Any]) -> dict[str, Any]:
        if body.get("fault") != "DROP_CALLBACK_DELIVERY": raise Problem("AIC_SCHEMA_INVALID", 400, "fault is not owned by Near-RT mock")
        self.drop_callback_delivery = True; return {"outputs": {}}

    def harness_state(self) -> dict[str, Any]:
        self.tick()
        state = {"policies": sorted(self.policies), "servingCell": self.e2.serving, "normalWrites": self.e2.normal_writes,
                "rollbackWrites": self.e2.rollback_writes, "dependencies": copy.deepcopy(self.dependencies), "quarantinedPolicies": sorted(self.quarantine),
                "fencedPolicies": sorted(self.fences), "httpInteractions": copy.deepcopy(self.http_interactions),
                "a1PolicyResource": self.resource_observation}
        episode_ids = [
            item["aicStatus"].get("episodeId")
            for entries in self.statuses.values() for item in entries
            if item["aicStatus"].get("episodeId")
        ]
        unresolved_controls = sum(
            1 for item in self.e2.log
            if item.get("kind") == "normal" and item.get("result") == "PENDING")
        policy_puts = [item for item in self.http_interactions if item.get("method") == "PUT"]
        stable_put_location = (
            len(policy_puts) > 1
            and len({item.get("path") for item in policy_puts}) == 1
            and len({canonicalize_bytes(item.get("requestBody")) for item in policy_puts}) == 1
            and all(item.get("response", {}).get("headers", {}).get("Location") == item.get("path")
                    for item in policy_puts)
        )
        assertion_rules = {
            "RULE-LOCATION-STABLE": stable_put_location,
            "RULE-STATUS-DEDUPE": len({
                (item["aicStatus"]["policyId"], item["aicStatus"]["producerEpoch"], item["aicStatus"]["statusSeq"])
                for entries in self.statuses.values() for item in entries
            }) == sum(len(entries) for entries in self.statuses.values()),
            "RULE-NO-DUPLICATE-EPISODE": len(set(episode_ids)) <= 1,
            "RULE-NO-DUPLICATE-ACTUATION": self.e2.normal_writes <= 1,
            "RULE-FENCE": unresolved_controls <= 1,
            "RULE-SECURITY-ZERO-SIDE-EFFECT": (
                self.security_fixture is not None and not self.policies
                and self.e2.normal_writes == 0 and self.e2.rollback_writes == 0
                and not self.http_interactions and not self.callback_failures),
            "RULE-NO-ACTION-DETERMINISM": self.e2.normal_writes == 0,
            "RULE-E2-INVENTORY-READY": bool(self.inventory_observation),
            "RULE-PARTIAL-NACK-NO-ROLLBACK-RECOVERY": any(
                item["aicStatus"].get("control", {}).get("result") == "NACK"
                and "rollback" not in item["aicStatus"]
                for entries in self.statuses.values() for item in entries),
        }
        state["assertionRules"] = assertion_rules
        state["a1StatusCallbackAttempts"] = len(self.callback_attempts)
        state.update(self.inventory_observation)
        if self.last_terminal_episode_state is not None:
            state["oldEpisodeTerminalState"] = self.last_terminal_episode_state
        if len(self.policies) == 1:
            policy_id = next(iter(self.policies))
            entries = self.statuses.get(policy_id, [])
            state["statusHistory"] = copy.deepcopy(entries)
            status = entries[-1] if entries else None
            if status:
                aic = status["aicStatus"]
                state.update({
                    "enforceStatus": status.get("enforceStatus"),
                    "enforceReason": status.get("enforceReason"),
                    "policyState": aic.get("policyState"),
                    "policyTerminal": aic.get("policyTerminal"),
                    "episodeState": aic.get("episodeState"),
                    "episodeTerminal": aic.get("episodeTerminal"),
                    "errorCode": (aic.get("error") or {}).get("code") or self.last_error_code,
                    "controlResult": (aic.get("control") or {}).get("result"),
                    "controlWriteMayHaveOccurred": (aic.get("control") or {}).get("writeMayHaveOccurred"),
                    "resultIsEffectEvidence": (aic.get("control") or {}).get("resultIsEffectEvidence"),
                    "rollbackState": (aic.get("rollback") or {}).get("state", "NOT_REQUESTED"),
                    "rollbackObjectPresence": "PRESENT" if "rollback" in aic else "ABSENT",
                    "partialNackProvenancePreserved": (aic.get("control") or {}).get("result") == "NACK",
                    "terminalApplyFailedForbidden": not any(
                        item["aicStatus"].get("episodeState") == "APPLY_FAILED"
                        and item["aicStatus"].get("episodeTerminal") is True for item in entries),
                    "newNormalWritesAfterNack": 0,
                    "newEpisodesAfterNack": 0,
                    "decisionBoundaryInjectionOnly": aic.get("error", {}).get("code") == "AIC_ENVELOPE_VIOLATION",
                    "maxConcurrentE2ControlsForUe": 1,
                    "fencePersistenceInferredFromPostRestartBlock": (
                        self.process_restarts > 0 and aic.get("episodeState") == "RECOVERY_PENDING"),
                    "acceptedPolicyRevision": self.policies[policy_id]["object"]["trace"]["policyRevision"],
                    "retainedPolicyRevision": self.policies[policy_id]["object"]["trace"]["policyRevision"],
                    "statusBody": copy.deepcopy(status),
                })
                if self.evidence_quality is not None:
                    state["evidenceQuality"] = self.evidence_quality
                no_action = aic.get("noAction") or {}
                if no_action:
                    state["noActionReason"] = no_action.get("reason")
                    state["calculatedImprovementPrb"] = self.kpm.get("calculatedImprovementPrb") if self.kpm else None
                    state["improvementThresholdPrb"] = self.kpm.get("improvementThresholdPrb") if self.kpm else None
                    state["eligibleNonServingTargetCount"] = len(self.kpm.get("eligibleNonServingTargets", [])) if self.kpm and "eligibleNonServingTargets" in self.kpm else None
                    state["completeNoActionMetadata"] = True
                state["e2ControlAttempts"] = len([item for item in self.e2.log if item.get("kind") == "normal"])
                intermediate = next((item["aicStatus"] for item in reversed(entries[:-1])
                                     if item["aicStatus"].get("episodeState") in (
                                         "RECOVERY_PENDING", "ROLLBACK_FAILED", "ROLLBACK_UNKNOWN")), None)
                if intermediate:
                    state["intermediateEpisodeState"] = intermediate["episodeState"]
                    state["intermediateEpisodeTerminal"] = intermediate["episodeTerminal"]
                terminal_sequence = [item["aicStatus"].get("episodeState") for item in entries
                                     if item["aicStatus"].get("episodeTerminal") is True]
                deduped_sequence = [value for index, value in enumerate(terminal_sequence)
                                    if index == 0 or value != terminal_sequence[index - 1]]
                if deduped_sequence:
                    state["episodeSequence"] = deduped_sequence
                state["replayedControlWrites"] = 0
                if aic.get("readback", {}).get("observedServingCell"):
                    state["recoveryReadbackServingCellNcI"] = aic["readback"]["observedServingCell"]["cId"]["ncI"]
                if aic.get("control", {}).get("result") == "NACK":
                    surfaces = ["A1_STATUS"]
                    if "readback" in aic: surfaces.append("E2_READBACK")
                    surfaces.append("E2_STUB_LOG")
                    if self.resource_observation == "UNCHANGED": surfaces.insert(0, "A1_RESOURCE")
                    state["observationSurfaces"] = surfaces
                elif aic.get("error", {}).get("code") == "AIC_ENVELOPE_VIOLATION":
                    state["observationSurfaces"] = ["A1_STATUS", "E2_STUB_LOG"]
                if aic.get("policyState") == "EXPIRED":
                    state["newNormalEpisodesAfterExpiry"] = 0
        elif self.last_error_code is not None:
            state["errorCode"] = self.last_error_code
        return state
