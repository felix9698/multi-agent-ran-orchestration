"""Fail-closed S4 judgement from A1 execution status plus R1 DME evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable

from .contract_support import canonicalize, validate
from .ports import AssuranceDecision


ASSURANCE_FRESHNESS_LIMIT_MS = 120_000


def _parse_utc(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("timestamp is not UTC Z")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return parsed


def _canon(value: Any) -> bytes:
    return canonicalize(value)


class CombinedAssurance:
    """The authoritative O-RAN S4 gate.

    ``ENFORCED`` is necessary but not sufficient.  A successful judgement also
    requires a verified Near-RT action/readback status and fresh, unambiguous,
    exactly correlated O1-derived evidence.
    """

    freshness_limit_ms = ASSURANCE_FRESHNESS_LIMIT_MS

    def __init__(self, capability_manifest: Dict[str, Any]):
        validate(capability_manifest, "aic:ran-capability:1.0.0")
        self.profile_id = capability_manifest["o1PerformanceProfileId"]
        self._cell_dns = {
            _canon(cell["cellId"]): cell["managedObjectDn"]
            for cell in capability_manifest["topology"]["cells"]}

    def assess(self, policy_object: Dict[str, Any], policy_id: str,
               policy_status: Dict[str, Any],
               evidence_records: Iterable[Dict[str, Any]], *,
               now: datetime) -> AssuranceDecision:
        try:
            validate(policy_object, "AIC_UECellSteering_1.0.0.policy")
            validate(policy_status, "AIC_UECellSteering_1.0.0.status")
            if now.tzinfo is None or now.utcoffset() is None:
                return AssuranceDecision.UNKNOWN
            now = now.astimezone(timezone.utc)
            status = policy_status["aicStatus"]
            trace = policy_object["trace"]
            if (status["policyId"] != policy_id or
                    status["policyRevision"] != trace["policyRevision"] or
                    status["trace"]["intentId"] != trace["intentId"] or
                    status["trace"]["intentRevision"] != trace["intentRevision"] or
                    status["trace"]["correlationId"] != trace["correlationId"]):
                return AssuranceDecision.UNKNOWN
            if policy_status["enforceStatus"] != "ENFORCED":
                terminal_failure = status.get("episodeTerminal") is True and \
                    status.get("episodeState") in {
                        "APPLY_FAILED", "READBACK_MISMATCH", "QUARANTINED"}
                return (AssuranceDecision.VIOLATED if terminal_failure
                        else AssuranceDecision.UNKNOWN)
            if (status.get("episodeState") != "APPLIED_VERIFIED" or
                    status.get("episodeTerminal") is not True or
                    status.get("readback", {}).get("result") != "VERIFIED"):
                return AssuranceDecision.UNKNOWN

            records = list(evidence_records)
            if not records:
                return AssuranceDecision.UNKNOWN
            expected_scope = _canon(policy_object["scope"]["ueId"])
            allowed_cells = {
                _canon(cell) for cell in policy_object["steeringObjective"]
                ["actionEnvelope"]["allowedCells"]}
            selected = status.get("selectedCell")
            if selected is not None and _canon(selected) not in allowed_cells:
                return AssuranceDecision.UNKNOWN
            readback_cell = status.get("readback", {}).get("observedServingCell")
            if selected is None or _canon(readback_cell) != _canon(selected):
                return AssuranceDecision.UNKNOWN
            expected_window = None
            seen_cells = set()
            for record in records:
                validate(record, "aic:policy-evidence:1.0.0")
                if (record["dmeTypeId"] != "aic:policy-evidence:1.0.0" or
                        record["source"]["interface"] != "O1" or
                        record["quality"] != "OK" or
                        record["phase"] != "AFTER"):
                    return AssuranceDecision.UNKNOWN
                if _canon(record["policyScope"]["ueId"]) != expected_scope:
                    return AssuranceDecision.UNKNOWN
                cell = _canon(record["measurementScope"]["cellId"])
                if cell not in allowed_cells or cell in seen_cells:
                    return AssuranceDecision.UNKNOWN
                if (self._cell_dns.get(cell) !=
                        record["measurementScope"]["managedObjectDn"] or
                        record["source"]["profileId"] != self.profile_id):
                    return AssuranceDecision.UNKNOWN
                seen_cells.add(cell)
                corr = record["correlation"]
                if (corr["policyTypeId"] != "AIC_UECellSteering_1.0.0" or
                        corr["policyId"] != policy_id or
                        corr["policyRevision"] != trace["policyRevision"] or
                        corr.get("episodeId") != status.get("episodeId")):
                    return AssuranceDecision.UNKNOWN
                control = status.get("control") or {}
                if control:
                    if (corr.get("transactionId") != control.get("transactionId") or
                            corr.get("actionId") != control.get("actionId")):
                        return AssuranceDecision.UNKNOWN
                window = (_parse_utc(record["window"]["start"]),
                          _parse_utc(record["window"]["end"]))
                if window[0] >= window[1] or _parse_utc(record["observedAt"]) != window[1]:
                    return AssuranceDecision.UNKNOWN
                if expected_window is None:
                    expected_window = window
                elif window != expected_window:
                    return AssuranceDecision.UNKNOWN
                age_ms = (now - window[1]).total_seconds() * 1000.0
                if age_ms < 0 or age_ms > self.freshness_limit_ms:
                    return AssuranceDecision.UNKNOWN
                required = [sample for sample in record["samples"]
                            if sample["name"] == "RRU.PrbDl"]
                if (len(required) != 1 or required[0]["quality"] != "OK" or
                        required[0]["value"] is None or
                        required[0]["measurementAgeMs"] > self.freshness_limit_ms or
                        any(sample["quality"] != "OK" for sample in record["samples"])):
                    return AssuranceDecision.UNKNOWN
            if _parse_utc(status["occurredAt"]) > expected_window[0]:
                # The action falls inside/after the declared AFTER window.
                return AssuranceDecision.UNKNOWN
            return AssuranceDecision.SATISFIED
        except (KeyError, TypeError, ValueError):
            return AssuranceDecision.UNKNOWN
