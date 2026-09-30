"""Readiness receipts are Lab Setup records, never candidates or OTA evidence."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from assurance.core.timebase import format_utc, parse_utc, utc_now
from .inventory import _reject_embedded_secrets

__all__ = ["Probe", "ReceiptView", "create_receipt", "read_receipt"]


@dataclass(frozen=True)
class Probe:
    component_id: str
    check: Callable[[], tuple[bool, str]]


@dataclass(frozen=True)
class ReceiptView:
    receipt: Mapping[str, Any]
    stale: bool
    age_ms: int
    counts_as_candidate: bool
    counts_as_ota_evidence: bool


def create_receipt(*, inventory: Mapping[str, Any], probes: Sequence[Probe],
                   configuration_identity: Mapping[str, str], started_at: str,
                   output_path: str | Path, now: str | None = None) -> Mapping[str, Any]:
    """Write a JSON receipt from injected probes only; no equipment path is implicit."""
    started = format_utc(parse_utc(started_at))
    checked = []
    for probe in probes:
        ok, detail = probe.check()
        checked.append({"componentId": probe.component_id, "state": "READY" if ok else "NOT_READY", "detail": str(detail)})
    readiness = "READY" if checked and all(item["state"] == "READY" for item in checked) else "NOT_READY"
    body = {
        "schemaVersion": "oran-aic-labctl-readiness-receipt/1.0.0",
        "kind": "LAB_SETUP_READINESS_RECEIPT",
        "startedAt": started, "generatedAt": now or format_utc(utc_now()),
        "inventory": dict(inventory), "configurationIdentity": dict(configuration_identity),
        "configurationState": {"staticConfig": "STAGED_NOT_RADIO_APPLIED"},
        "readiness": {"state": readiness, "components": checked},
        "provenance": {"producer": "labctl", "signed": False, "secretFree": True},
        "boundary": {"countsAsCandidate": False, "countsAsObjectiveEffect": False,
                     "countsAsOtaEvidence": False, "cockpitMayStartLabSetup": False},
    }
    _reject_embedded_secrets(body)
    target = Path(output_path)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.write_text(json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    return body


def read_receipt(path: str | Path, *, now: str, maximum_age_ms: int) -> ReceiptView:
    try:
        body = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("cannot read readiness receipt") from exc
    if not isinstance(body, dict) or body.get("schemaVersion") != "oran-aic-labctl-readiness-receipt/1.0.0":
        raise ValueError("unsupported readiness receipt")
    boundary = body.get("boundary")
    if not isinstance(boundary, dict) or any(boundary.get(key) is not False for key in ("countsAsCandidate", "countsAsObjectiveEffect", "countsAsOtaEvidence", "cockpitMayStartLabSetup")):
        raise ValueError("receipt boundary is invalid")
    _reject_embedded_secrets(body)
    age_ms = int((parse_utc(now) - parse_utc(body.get("generatedAt"))).total_seconds() * 1000)
    return ReceiptView(body, age_ms > maximum_age_ms, age_ms, False, False)
