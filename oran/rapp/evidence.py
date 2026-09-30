"""O-RAN-only four-plane evidence records and identifier-chain persistence."""

from __future__ import annotations

import copy
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


STAGES = frozenset({
    "COORDINATOR_JUDGEMENT",
    "A1_POLICY_LIFECYCLE",
    "NEAR_RT_ACTION_EVIDENCE",
    "INTENT_SATISFACTION",
})


class EvidenceLedger:
    """Append-only JSONL ledger; existing experiment metrics remain untouched."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._lock = threading.RLock()

    def append(self, *, stage: str, identifiers: Dict[str, Any],
               outcome: str, evidence_ref: str | None,
               detail: Dict[str, Any] | None = None) -> Dict[str, Any]:
        if stage not in STAGES:
            raise ValueError(f"unknown O-RAN evidence stage: {stage}")
        required = {"run_id", "episode_id", "cycle_id", "proposal_id",
                    "trial_id", "intent_id", "intent_revision",
                    "r1_request_id", "policyTypeId", "policyId",
                    "policyRevision", "kpi_window", "scope"}
        missing = required - set(identifiers)
        if missing:
            raise ValueError(f"identifier chain missing: {sorted(missing)}")
        record = {
            "recordedAt": datetime.now(timezone.utc).isoformat(
                timespec="milliseconds").replace("+00:00", "Z"),
            "stage": stage,
            "identifiers": copy.deepcopy(identifiers),
            "outcome": outcome,
            "evidenceRef": evidence_ref,
            "detail": copy.deepcopy(detail or {}),
        }
        encoded = json.dumps(record, separators=(",", ":"), sort_keys=True)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(encoded + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        return record

    def read_all(self) -> list[Dict[str, Any]]:
        if not self.path.exists():
            return []
        with self._lock, self.path.open("r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
