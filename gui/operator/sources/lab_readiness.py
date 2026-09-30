"""Read-only projection of a Lab Setup readiness receipt for the Cockpit."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tools.labctl.receipt import read_receipt


@dataclass(frozen=True)
class LabReadinessView:
    state: str
    stale: bool
    age_ms: int
    secret_free: bool
    may_start_lab_setup: bool = False
    counts_as_verdict_evidence: bool = False


def read_lab_setup_receipt(path: str | Path, *, now: str,
                           maximum_age_ms: int) -> LabReadinessView:
    """Read a receipt without importing the Lab Setup CLI or any executor."""
    receipt = read_receipt(path, now=now, maximum_age_ms=maximum_age_ms)
    body = receipt.receipt
    return LabReadinessView(
        state=str((body.get("readiness") or {}).get("state", "NOT_READY")),
        stale=receipt.stale,
        age_ms=receipt.age_ms,
        secret_free=bool((body.get("provenance") or {}).get("secretFree")),
    )
