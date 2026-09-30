"""Durable SQLite state for policy, status, DME and harness recovery."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class DurableStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _initialize(self) -> None:
        with self.transaction() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS policies (
                    policy_id TEXT PRIMARY KEY,
                    rapp_id TEXT NOT NULL,
                    near_rt_ric_id TEXT NOT NULL,
                    policy_type_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    policy_json TEXT NOT NULL,
                    information_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    location TEXT NOT NULL,
                    pending_operation TEXT,
                    pending_policy_json TEXT,
                    UNIQUE(rapp_id, near_rt_ric_id, policy_type_id, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS statuses (
                    policy_id TEXT PRIMARY KEY,
                    producer_epoch TEXT NOT NULL,
                    status_seq INTEGER NOT NULL,
                    status_json TEXT NOT NULL,
                    FOREIGN KEY(policy_id) REFERENCES policies(policy_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS status_audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    policy_id TEXT NOT NULL,
                    producer_epoch TEXT,
                    status_seq INTEGER,
                    disposition TEXT NOT NULL,
                    status_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS subscriptions (
                    subscription_id TEXT PRIMARY KEY,
                    body_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS services (
                    api_id TEXT PRIMARY KEY,
                    rapp_id TEXT NOT NULL,
                    body_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dme_registrations (
                    registration_id TEXT PRIMARY KEY,
                    dme_type_id TEXT NOT NULL,
                    body_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS data_jobs (
                    data_job_id TEXT PRIMARY KEY,
                    body_json TEXT NOT NULL,
                    status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS faults (
                    fault TEXT NOT NULL,
                    boundary_json TEXT NOT NULL,
                    remaining INTEGER NOT NULL,
                    PRIMARY KEY(fault, boundary_json)
                );
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def encode(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def decode(value: str) -> Any:
        return json.loads(value)

    def rows(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self.transaction() as db:
            return list(db.execute(sql, parameters).fetchall())

    def row(self, sql: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        values = self.rows(sql, parameters)
        return values[0] if values else None

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> None:
        with self.transaction() as db:
            db.execute(sql, parameters)

    def reserve_fault(self, fault: str, boundary: dict[str, Any]) -> None:
        encoded = self.encode(boundary)
        with self.transaction() as db:
            db.execute(
                """INSERT INTO faults(fault,boundary_json,remaining) VALUES(?,?,1)
                   ON CONFLICT(fault,boundary_json)
                   DO UPDATE SET remaining=remaining+1""",
                (fault, encoded),
            )

    def consume_fault(self, fault: str, boundary: dict[str, Any]) -> bool:
        encoded = self.encode(boundary)
        with self.transaction() as db:
            row = db.execute(
                "SELECT remaining FROM faults WHERE fault=? AND boundary_json=?",
                (fault, encoded),
            ).fetchone()
            if row is None or row["remaining"] <= 0:
                return False
            db.execute(
                "UPDATE faults SET remaining=remaining-1 WHERE fault=? AND boundary_json=?",
                (fault, encoded),
            )
            return True

    def snapshot(self) -> dict[str, Any]:
        policy_rows = self.rows(
            "SELECT policy_id,state,near_rt_ric_id,policy_type_id FROM policies ORDER BY policy_id"
        )
        job_rows = self.rows(
            "SELECT data_job_id,status FROM data_jobs ORDER BY data_job_id"
        )
        status_rows = self.rows(
            "SELECT policy_id,producer_epoch,status_seq FROM statuses ORDER BY policy_id"
        )
        subscription_count = self.row("SELECT COUNT(*) AS count FROM subscriptions")["count"]
        state: dict[str, Any] = {
            "component": "NON_RT_RIC_FRAMEWORK",
            "policies": [dict(row) for row in policy_rows],
            "policyStatuses": [dict(row) for row in status_rows],
            "dataJobs": [dict(row) for row in job_rows],
            "subscriptionCount": subscription_count,
            "subscriptionResource": "PRESENT" if subscription_count else "DELETED",
        }
        if len(status_rows) == 1:
            row = self.row("SELECT status_json FROM statuses WHERE policy_id=?",
                           (status_rows[0]["policy_id"],))
            status = self.decode(row["status_json"])
            aic = status["aicStatus"]
            state.update({
                "statusBody": status,
                "enforceStatus": status["enforceStatus"],
                "policyState": aic["policyState"],
                "policyTerminal": aic["policyTerminal"],
                "finalProducerEpoch": aic["producerEpoch"],
                "finalStatusSeq": aic["statusSeq"],
            })
            if "episodeState" in aic:
                state["episodeState"] = aic["episodeState"]
                state["episodeTerminal"] = aic["episodeTerminal"]
            error = aic.get("error")
            state["errorCode"] = error.get("code") if isinstance(error, dict) else None
        dispositions = {
            row["disposition"]: row["count"]
            for row in self.rows(
                "SELECT disposition,COUNT(*) AS count FROM status_audit GROUP BY disposition")
        }
        applied_statuses = self.rows(
            """SELECT producer_epoch,status_seq FROM status_audit
               WHERE disposition IN (
                   'APPLIED_QUERY','APPLIED_NOTIFICATION','APPLIED_INITIAL_STATE')
               ORDER BY audit_id""")
        last_sequence_by_epoch: dict[str, int] = {}
        regression_count = 0
        for row in applied_statuses:
            epoch, sequence = row["producer_epoch"], row["status_seq"]
            if (isinstance(epoch, str) and isinstance(sequence, int)
                    and epoch in last_sequence_by_epoch
                    and sequence < last_sequence_by_epoch[epoch]):
                regression_count += 1
            if isinstance(epoch, str) and isinstance(sequence, int):
                last_sequence_by_epoch[epoch] = sequence
        state.update({
            "ignoredLowerStatusSeqCount": dispositions.get("LATE_LOWER_SEQUENCE", 0),
            "ignoredOldProducerEpochCount": dispositions.get("OLD_PRODUCER_EPOCH", 0),
            "appliedStatusSnapshotCount": (
                dispositions.get("APPLIED_QUERY", 0)
                + dispositions.get("APPLIED_NOTIFICATION", 0)),
            "r1CallbackDroppedCount": dispositions.get("R1_CALLBACK_DROPPED", 0),
            "statusRegressionCount": regression_count,
        })
        accepted_digests: set[str] = set()
        for row in self.rows(
                "SELECT value_json FROM metadata WHERE key LIKE 'dme-push:%'"):
            values = self.decode(row["value_json"])
            if isinstance(values, list):
                accepted_digests.update(
                    value for value in values if isinstance(value, str))
        state["acceptedPushPayloadDigests"] = sorted(accepted_digests)
        return state

    def reset_scenario_state(self) -> None:
        """Destructively isolate one development conformance scenario."""
        with self.transaction() as db:
            for table in (
                    "statuses", "status_audit", "subscriptions", "services",
                    "data_jobs", "dme_registrations", "faults", "policies",
                    "metadata"):
                db.execute(f"DELETE FROM {table}")
