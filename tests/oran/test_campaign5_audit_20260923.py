"""Campaign-5 producer/worker defects found by the 2026-09-23 audit; hardware-free."""

from __future__ import annotations

import contextlib
import os
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from oran.campaign5 import producer as producer_module
from oran.campaign5.live_worker import Campaign5LiveWorker
from oran.campaign5.producer import A1Conflict, Campaign5PolicyProducer
from tests.oran.test_campaign5_live_worker import CAP, NOW, TEST_ENV, Harness, policy


class TheExpiryTimerSurvivesAFailedPass(unittest.TestCase):
    """One exception used to end the timer thread, and every later expiry with it."""

    def test_the_loop_logs_and_keeps_polling(self):
        stop = threading.Event()
        calls = []

        class Worker:
            def expire_due(self):
                calls.append(1)
                if len(calls) == 1:
                    raise RuntimeError("ledger is unreadable")
                if len(calls) >= 3:
                    stop.set()
                return ()

        with self.assertLogs("oran.campaign5.producer", level="ERROR"):
            thread = threading.Thread(
                target=producer_module._expiry_loop, args=(Worker(), stop, 0.001))
            thread.start()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertGreaterEqual(len(calls), 3)


class AVerifiedRollbackOwesNothing(unittest.TestCase):
    """NACK, then a rollback read back at the baseline: nothing is left on the radio.

    The worker's ledger keeps ``owners`` after a verified rollback, so the debt
    probe answered "maybe written" and the status became
    ``writeMayHaveOccurred: true`` + ``rollback: REQUESTED`` (UNKNOWN →
    PARTIAL_APPLY upstream).
    """

    def setUp(self):
        environment = patch.dict(os.environ, TEST_ENV, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_nack_with_verified_rollback_reports_no_write(self):
        harness = Harness(self.root, [None, 0])   # apply: no effect; rollback: baseline 0
        worker = harness.worker()

        def runner(argv, **kwargs):
            result = harness.runner(argv, **kwargs)
            if len(harness.calls) == 1:
                result.stdout = "RC control action=102 success=0\nRIC Control NACK\n"
            return result

        worker._runner = runner
        producer = Campaign5PolicyProducer()
        worker.bind(producer)
        producer.put_policy(CAP, "p1", policy())

        self.assertEqual(2, len(harness.calls))
        aic = producer.get_status(CAP, "p1")["aicStatus"]
        self.assertEqual("NACK", aic["control"]["result"])
        self.assertFalse(aic["control"]["writeMayHaveOccurred"])
        self.assertNotIn("rollback", aic)
        self.assertTrue(aic["episodeTerminal"])
        record = producer.control_records(CAP, "p1")[-1]
        self.assertFalse(record["writeMayHaveOccurred"])


class ARefusedUpdateKeepsThePolicyThatWasThere(unittest.TestCase):
    """The update used to overwrite digest/revision before the worker ran, so a
    refused update followed by the same body matched the new digest and answered
    200 without calling the worker at all."""

    def setUp(self):
        self.producer = Campaign5PolicyProducer()
        self.calls, self.refuse = [], False

        def apply(policy_type_id, policy_id, body):
            self.calls.append(body["trace"]["revision"])
            if self.refuse:
                raise A1Conflict("nothing was applied; the write was refused before it began")
            self.producer.record_applied(policy_type_id, policy_id, control_ack=True,
                                         observed_config=body["config"])

        self.producer.bind_live_worker(apply, lambda policy_id: None)
        self.producer.put_policy(CAP, "p", policy(12))

    @staticmethod
    def update(value, revision):
        body = policy(value)
        body["trace"].update(revision=revision, fencingToken=revision)
        return body

    def test_a_resend_after_a_refusal_reaches_the_worker(self):
        before = self.producer.get_status(CAP, "p")
        self.refuse = True
        answer = self.producer.handle(
            "PUT", f"/A1-P/v2/policytypes/{CAP}/policies/p", self.update(6, 2))
        self.assertEqual(409, answer.status)
        self.assertEqual(1, self.producer.get_policy(CAP, "p")["trace"]["revision"])
        self.assertEqual(before, self.producer.get_status(CAP, "p"))

        self.refuse = False
        answer = self.producer.handle(
            "PUT", f"/A1-P/v2/policytypes/{CAP}/policies/p", self.update(6, 2))
        self.assertEqual(200, answer.status)
        self.assertEqual([1, 2, 2], self.calls)
        self.assertEqual(2, self.producer.get_policy(CAP, "p")["trace"]["revision"])
        aic = self.producer.get_status(CAP, "p")["aicStatus"]
        self.assertEqual(2, aic["policyRevision"])
        self.assertEqual(6, aic["selectedDlPrbCap"]["maxDlPrbs"])
        self.assertGreater(aic["statusSeq"], before["aicStatus"]["statusSeq"])


class ExpiryBackOffIsCountedFromEachTransaction(unittest.TestCase):
    """One ``now`` for the whole pass: a restore that took 40 s set the next
    candidate's back-off in the past, so it retried on the very next tick."""

    class _Ledger:
        def __init__(self, state):
            self.state = state

        def edit(self, **_):
            return contextlib.nullcontext(self.state)

    def test_each_failure_backs_off_from_its_own_clock(self):
        clock = [NOW]
        worker = Campaign5LiveWorker.__new__(Campaign5LiveWorker)
        worker._clock = lambda: clock[0]
        expired = (NOW - timedelta(seconds=1)).isoformat()
        worker._ledger = self._Ledger({
            "entries": {pid: {"notAfter": expired, "scopeKey": f"s-{pid}"} for pid in "ab"},
            "owners": {"s-a": "a", "s-b": "b"},
        })
        worker._producer = SimpleNamespace(policy_record=lambda pid: {"policyTypeId": CAP})
        worker._lifecycle_lock = threading.RLock()

        def slow_failure(policy_id):
            clock[0] += timedelta(seconds=40)
            raise RuntimeError("restore not verified")

        worker._delete_from_a1 = slow_failure
        worker._rollback_target_is_gone = lambda *args: False
        worker._identity_superseded = lambda policy_id: False
        with self.assertLogs("oran.campaign5.live_worker", level="ERROR"):
            self.assertEqual((), worker.expire_due())
        retry_at = worker._expiry_retry_at
        self.assertEqual((NOW + timedelta(seconds=40)).timestamp() + 2.0, retry_at["a"])
        self.assertEqual((NOW + timedelta(seconds=80)).timestamp() + 2.0, retry_at["b"])
        self.assertGreater(retry_at["b"], clock[0].timestamp())


if __name__ == "__main__":
    unittest.main()
