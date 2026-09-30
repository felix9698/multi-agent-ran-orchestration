"""Durable ownership records and exclusive mutation lock."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional


class StateError(RuntimeError):
    """Raised when run state cannot be trusted or mutated safely."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(body: Dict[str, Any]) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


@dataclass
class RunRecord:
    run_id: str
    run_dir: Path
    record_path: Path
    body: Dict[str, Any]


class RunStore:
    def __init__(self, state_root: str | Path):
        self.state_root = Path(state_root)

    def _ensure_root(self) -> None:
        self.state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.state_root, 0o700)

    def _write(self, run: RunRecord) -> None:
        payload = _canonical(run.body)
        descriptor, temp_name = tempfile.mkstemp(prefix=".run-record.", dir=run.run_dir)
        temp_path = Path(temp_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, run.record_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    def begin(self, profile_id: str, action: str) -> RunRecord:
        self._ensure_root()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        run_id = f"{stamp}-{uuid.uuid4().hex[:12]}"
        run_dir = self.state_root / run_id
        run_dir.mkdir(mode=0o700, exist_ok=False)
        record = RunRecord(
            run_id=run_id,
            run_dir=run_dir,
            record_path=run_dir / "run.json",
            body={
                "schemaVersion": "oran-aic-labctl-run/1.0.0",
                "runId": run_id,
                "profileId": profile_id,
                "action": action,
                "startedAt": _utc_now(),
                "endedAt": None,
                "disposition": "IN_PROGRESS",
                "components": {},
            },
        )
        self._write(record)
        return record

    def mark_started(self, run: RunRecord, component_id: str, preexisting: bool) -> None:
        ownership = "PREEXISTING" if preexisting else "STARTED_BY_THIS_RUN"
        run.body["components"][component_id] = {
            "ownership": ownership,
            "recordedAt": _utc_now(),
        }
        self._write(run)

    def mark_stopped(self, run: RunRecord, component_id: str) -> None:
        entry = run.body.get("components", {}).get(component_id)
        if not isinstance(entry, dict) or entry.get("ownership") != "STARTED_BY_THIS_RUN":
            raise StateError(f"cannot mark unowned component stopped: {component_id}")
        entry["stoppedAt"] = _utc_now()
        self._write(run)

    def finish(self, run: RunRecord, disposition: str) -> None:
        run.body["disposition"] = disposition
        run.body["endedAt"] = _utc_now()
        self._write(run)

    def latest(self) -> Optional[RunRecord]:
        if not self.state_root.exists():
            return None
        candidates = sorted(
            path for path in self.state_root.iterdir() if path.is_dir() and not path.name.startswith(".")
        )
        if not candidates:
            return None
        run_dir = candidates[-1]
        record_path = run_dir / "run.json"
        try:
            body = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(f"malformed latest run state: {record_path}: {exc}") from exc
        if not isinstance(body, dict) or body.get("runId") != run_dir.name:
            raise StateError(f"malformed latest run state: {record_path}")
        return RunRecord(run_dir.name, run_dir, record_path, body)

    def latest_owning_run(self) -> Optional[RunRecord]:
        if not self.state_root.exists():
            return None
        candidates = sorted(
            (
                path
                for path in self.state_root.iterdir()
                if path.is_dir() and not path.name.startswith(".")
            ),
            reverse=True,
        )
        for run_dir in candidates:
            record_path = run_dir / "run.json"
            try:
                body = json.loads(record_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise StateError(f"malformed run state: {record_path}: {exc}") from exc
            if not isinstance(body, dict) or body.get("runId") != run_dir.name:
                raise StateError(f"malformed run state: {record_path}")
            components = body.get("components", {})
            if any(
                isinstance(entry, dict)
                and entry.get("ownership") == "STARTED_BY_THIS_RUN"
                and "stoppedAt" not in entry
                for entry in components.values()
            ):
                return RunRecord(run_dir.name, run_dir, record_path, body)
        return None


@contextmanager
def mutation_lock(state_root: str | Path) -> Iterator[None]:
    root = Path(state_root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    lock_path = root / ".mutation.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StateError("another mutation is already in progress") from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
