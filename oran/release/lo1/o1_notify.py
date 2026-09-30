"""Durable, fail-closed acceptance for live O1 file-ready notifications."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


class NotificationRefused(ValueError):
    """The notification cannot be durably accepted under the O1 profile."""


def _utc(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise NotificationRefused("O1 timestamps must be RFC3339 UTC Z")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise NotificationRefused("O1 timestamp is malformed") from exc


def parse_file_ready(raw: bytes, *, expected_job_id: str) -> dict[str, Any]:
    """Parse and validate the notification without persisting partial state."""
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NotificationRefused("notification body is not UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise NotificationRefused("notification body must be one JSON object")
    if document.get("notificationType") != "notifyFileReady":
        raise NotificationRefused("only notifyFileReady is accepted on this boundary")
    event_time = _utc(document.get("eventTime"))
    href = document.get("href")
    files = document.get("fileInfoList")
    if not isinstance(href, str) or not href or not isinstance(files, list) or not files:
        raise NotificationRefused("file-ready requires href and a non-empty fileInfoList")
    for info in files:
        if not isinstance(info, Mapping):
            raise NotificationRefused("each fileInfoList item must be an object")
        ready = _utc(info.get("fileReadyTime"))
        expiration = _utc(info.get("fileExpirationTime"))
        if event_time != ready or ready >= expiration:
            raise NotificationRefused("notification/file temporal order is invalid")
        if info.get("fileDataType") != "PERFORMANCE":
            raise NotificationRefused("only PERFORMANCE file data is accepted")
        if info.get("fileFormat") != "32.435 V10.0 XML-schema":
            raise NotificationRefused("notification fileFormat differs from the frozen profile")
        if info.get("fileCompression") not in ("", "NONE"):
            raise NotificationRefused("compressed live PM input is not assigned to this release")
        if info.get("jobId") != expected_job_id:
            raise NotificationRefused("notification jobId does not match the deployment vector")
        if not isinstance(info.get("fileSize"), int) or info["fileSize"] < 1:
            raise NotificationRefused("notification fileSize must be a positive integer")
        if not isinstance(info.get("fileLocation"), str):
            raise NotificationRefused("notification fileLocation is absent")
    return document


class DurableNotificationJournal:
    """Atomic journal proving acceptance completed before the 204 is returned."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._lock = threading.RLock()

    @contextmanager
    def _serialized(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.lock_path.open("a+b") as handle:
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except ImportError as exc:
                raise NotificationRefused("process-safe journal locking is unavailable") from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"acceptedNotifications": []}
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NotificationRefused("notification journal is unreadable") from exc
        if not isinstance(state, dict) or not isinstance(state.get("acceptedNotifications"), list):
            raise NotificationRefused("notification journal has an invalid shape")
        return state

    def append(self, document: Mapping[str, Any], *, raw_sha256: str, raw_artifact_path: str) -> bool:
        with self._serialized():
            state = self._read()
            accepted = state["acceptedNotifications"]
            duplicate = any(item.get("rawSha256") == raw_sha256 for item in accepted)
            if not duplicate:
                accepted.append(
                    {
                        "rawSha256": raw_sha256,
                        "rawArtifactPath": raw_artifact_path,
                        "document": dict(document),
                    }
                )
            fd, temporary = tempfile.mkstemp(prefix=".o1-notify-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    payload = (json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n").encode()
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return duplicate

    def clear(self) -> bool:
        with self._serialized():
            if not self.path.exists():
                removed = False
            else:
                self.path.unlink()
                removed = True
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            pass
        return removed


def raw_name(raw: bytes) -> str:
    return "notification-%s.json" % hashlib.sha256(raw).hexdigest()
