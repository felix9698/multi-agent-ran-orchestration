"""The adapter's own record of the writes it issued.

A write count is the number every refusal claim in
``docs/traceability/FAULT-MATRIX.md`` rests on, so the record it is read from
has to be the adapter's, has to survive the process, and has to classify a
refusal exactly the way the returned :class:`GatewayResult` classifies it.
Those three are what this module asserts.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from assurance.gateway.commands import GatewayOperation, build_command
from assurance.gateway.plan import config_hash
from assurance.gateway.r1_operation_journal import (
    InMemoryR1OperationJournal, JsonlR1OperationJournal, R1Operation,
    R1OperationOutcome, R1PolicyOperation, write_counts,
)
from assurance.gateway.write_gateway import GatewayOutcome

from tests.assurance.action102_support import (
    APPLIED_CAP, CAP, CapFaults, CountingCapPolicyPort, baseline_config,
    build_cap_harness, permit, plan_scope,
)

from oran.campaign5.producer import A1Conflict


def _entry(sequence: int, operation: R1PolicyOperation,
           outcome: R1OperationOutcome = R1OperationOutcome.ISSUED) -> R1Operation:
    return R1Operation(sequence=sequence, transaction_id="tx-1",
                       operation=operation, outcome=outcome)


class TheRecordShape(unittest.TestCase):

    def test_only_the_three_write_calls_count_as_writes(self):
        writes = {R1PolicyOperation.CREATE, R1PolicyOperation.UPDATE,
                  R1PolicyOperation.DELETE}
        for operation in R1PolicyOperation:
            with self.subTest(operation=operation.value):
                self.assertEqual(
                    _entry(1, operation).is_write, operation in writes)

    def test_only_a_refusal_proves_nothing_was_sent(self):
        for outcome, expected in ((R1OperationOutcome.ISSUED, True),
                                  (R1OperationOutcome.UNKNOWN, True),
                                  (R1OperationOutcome.REFUSED, False)):
            with self.subTest(outcome=outcome.value):
                entry = _entry(1, R1PolicyOperation.CREATE, outcome)
                self.assertIs(entry.write_may_have_occurred, expected)
        # A read is never a write, whatever it answered.
        self.assertFalse(_entry(1, R1PolicyOperation.STATUS).write_may_have_occurred)

    def test_a_record_round_trips_through_its_canonical_form(self):
        entry = R1Operation(
            sequence=4, transaction_id="tx-1", operation=R1PolicyOperation.DELETE,
            outcome=R1OperationOutcome.UNKNOWN, adapter="r1-cap",
            policy_type_id="AIC_UeDlPrbCap_1.0.0", policy_id="pol-1",
            fencing_token=2, reference="r1-cap:undo:3", at="2026-09-04T09:00:00Z",
            detail="TimeoutError: no answer")
        self.assertEqual(
            R1Operation.from_canonical_dict(entry.to_canonical_dict()), entry)

    def test_a_sequence_below_one_is_refused(self):
        with self.assertRaises(ValueError):
            _entry(0, R1PolicyOperation.CREATE)


class TheCounts(unittest.TestCase):

    def test_a_lost_answer_counts_as_a_possible_write_and_a_refusal_does_not(self):
        counts = write_counts([
            _entry(1, R1PolicyOperation.CREATE),
            _entry(2, R1PolicyOperation.UPDATE, R1OperationOutcome.UNKNOWN),
            _entry(3, R1PolicyOperation.CREATE, R1OperationOutcome.REFUSED),
            _entry(4, R1PolicyOperation.DELETE),
            _entry(5, R1PolicyOperation.STATUS),
        ])
        self.assertEqual(counts, {"applies": 2, "withdrawals": 1,
                                  "refused": 1, "unknown": 1})

    def test_the_ring_bounds_the_detail_and_never_the_count(self):
        journal = InMemoryR1OperationJournal(limit=2)
        for index in range(5):
            journal.append(_entry(index + 1, R1PolicyOperation.CREATE))
        self.assertEqual(len(journal.operations()), 2)
        self.assertEqual([entry.sequence for entry in journal.operations()], [4, 5])
        self.assertEqual(journal.total, 5)
        self.assertEqual(journal.writes_issued, 5)
        self.assertEqual(journal.next_sequence(), 6)

    def test_operations_can_be_read_for_one_transaction(self):
        journal = InMemoryR1OperationJournal()
        journal.append(_entry(1, R1PolicyOperation.CREATE))
        journal.append(R1Operation(sequence=2, transaction_id="tx-2",
                                   operation=R1PolicyOperation.CREATE,
                                   outcome=R1OperationOutcome.ISSUED))
        self.assertEqual([entry.transaction_id
                          for entry in journal.operations("tx-2")], ["tx-2"])


class TheDurableFile(unittest.TestCase):

    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "r1-cap-operations.jsonl"

    def test_a_tail_without_a_newline_does_not_glue_the_next_record(self):
        """죽은 프로세스가 남긴 개행 없는 꼬리 (2026-09-22).

        `}` 까지 쓰고 개행 전에 죽으면 로딩은 `splitlines()` 라 그 줄을 정상으로
        받아들이는데, 다음 append 가 바로 이어 붙어 `{...}{...}` 가 된다.  그 다음
        기동에서는 journal 을 **통째로** 못 읽어 쓰기 이력 복원이 막힌다.
        """
        JsonlR1OperationJournal(self.path).append(_entry(1, R1PolicyOperation.CREATE))
        body = self.path.read_text(encoding="utf-8")
        self.path.write_text(body.rstrip("\n"), encoding="utf-8")   # 개행만 잘라낸다
        JsonlR1OperationJournal(self.path).append(_entry(2, R1PolicyOperation.DELETE))
        restarted = JsonlR1OperationJournal(self.path)               # 읽을 수 있어야 한다
        self.assertEqual([1, 2], [item.sequence for item in restarted.operations()])

    def test_the_counts_survive_the_process_that_wrote_them(self):
        first = JsonlR1OperationJournal(self.path)
        first.append(_entry(1, R1PolicyOperation.CREATE))
        first.append(_entry(2, R1PolicyOperation.DELETE, R1OperationOutcome.UNKNOWN))

        restarted = JsonlR1OperationJournal(self.path)
        self.assertEqual(restarted.total, 2)
        self.assertEqual(restarted.writes_that_may_have_occurred, 2)
        self.assertEqual(restarted.next_sequence(), 3)
        self.assertEqual(write_counts(restarted.operations()),
                         {"applies": 1, "withdrawals": 1,
                          "refused": 0, "unknown": 1})

    def test_it_is_append_only_on_disk(self):
        journal = JsonlR1OperationJournal(self.path)
        journal.append(_entry(1, R1PolicyOperation.CREATE))
        journal.append(_entry(2, R1PolicyOperation.DELETE))
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn('"operation":"CREATE"', lines[0])
        self.assertIn('"operation":"DELETE"', lines[1])


class TheAdapterRecordsWhatItIssued(unittest.TestCase):
    """The journal and the returned result can never disagree."""

    def setUp(self):
        self.base = config_hash(baseline_config())

    def _staged(self, faults=None):
        harness = build_cap_harness(faults=faults)
        harness.gateway.prepare(token=permit("PREPARE", self.base, 0),
                                plan=harness.plan(steering=False))
        harness.gateway.ready(token=permit("READY", self.base, 1))
        return harness

    def test_a_prepare_records_a_validate_and_no_write(self):
        harness = self._staged()
        entries = harness.adapter.operations("tx-cap")
        # Two records for the one call: the in-flight intent, then its answer.
        self.assertEqual([entry.operation for entry in entries],
                         [R1PolicyOperation.VALIDATE, R1PolicyOperation.VALIDATE])
        self.assertEqual([entry.outcome for entry in entries],
                         [R1OperationOutcome.IN_FLIGHT,
                          R1OperationOutcome.ISSUED])
        self.assertEqual(entries[1].resolves, entries[0].sequence)
        self.assertEqual(harness.adapter.write_counts("tx-cap"),
                         {"applies": 0, "withdrawals": 0,
                          "refused": 0, "unknown": 0})
        self.assertEqual(harness.adapter.unresolved_operations(), ())

    def test_a_commit_records_the_create_with_the_policy_it_returned(self):
        harness = self._staged()
        harness.gateway.commit(token=permit("COMMIT", self.base, 2))
        created = [entry for entry in harness.adapter.operations("tx-cap")
                   if entry.operation is R1PolicyOperation.CREATE]
        self.assertEqual([entry.outcome for entry in created],
                         [R1OperationOutcome.IN_FLIGHT,
                          R1OperationOutcome.ISSUED])
        # The in-flight record cannot name the policy: it is written before the
        # call that returns one.  The resolution does, and settles it.
        self.assertIsNone(created[0].policy_id)
        self.assertEqual(created[1].policy_id,
                         harness.adapter.bound_policy("tx-cap"))
        self.assertEqual(created[1].resolves, created[0].sequence)
        self.assertEqual(created[1].adapter, CAP.adapter)
        self.assertEqual(created[1].fencing_token, 2)
        # One call, one apply -- never two because two records were written.
        self.assertEqual(harness.adapter.write_counts("tx-cap")["applies"], 1)

    def test_a_refusal_is_recorded_as_refused_exactly_as_the_result_says(self):
        harness = self._staged(faults=CapFaults(refuse_create=A1Conflict))
        result = harness.gateway.commit(token=permit("COMMIT", self.base, 2))
        self.assertIs(result.outcome, GatewayOutcome.REJECTED)
        refusals = [entry for entry in harness.adapter.operations("tx-cap")
                    if entry.is_write]
        self.assertEqual([entry.outcome for entry in refusals],
                         [R1OperationOutcome.IN_FLIGHT,
                          R1OperationOutcome.REFUSED])
        self.assertIn("A1Conflict", refusals[1].detail)
        # REJECTED and REFUSED are the same judgement, made once.
        self.assertEqual(harness.adapter.write_counts("tx-cap")["refused"], 1)
        self.assertEqual(harness.adapter.write_counts("tx-cap")["applies"], 0)

    def test_a_lost_answer_is_recorded_as_unknown_exactly_as_the_result_says(self):
        harness = self._staged(faults=CapFaults(refuse_create=TimeoutError))
        result = harness.gateway.commit(token=permit("COMMIT", self.base, 2))
        self.assertIn(result.outcome,
                      (GatewayOutcome.UNKNOWN, GatewayOutcome.PARTIAL_APPLY))
        writes = [entry for entry in harness.adapter.operations("tx-cap")
                  if entry.is_write]
        self.assertEqual([entry.outcome for entry in writes],
                         [R1OperationOutcome.IN_FLIGHT,
                          R1OperationOutcome.UNKNOWN])
        self.assertEqual(harness.adapter.write_counts("tx-cap"),
                         {"applies": 1, "withdrawals": 0,
                          "refused": 0, "unknown": 1})

    def test_the_withdrawal_is_recorded_once_however_often_it_is_asked_for(self):
        harness = self._staged()
        harness.gateway.commit(token=permit("COMMIT", self.base, 2))
        applied = config_hash({**baseline_config(), CAP.axis: APPLIED_CAP})
        harness.gateway.reverse_rollback(
            token=permit("REVERSE_ROLLBACK", applied, 3))
        harness.gateway.reverse_rollback(
            token=permit("REVERSE_ROLLBACK", applied, 3))
        self.assertEqual(harness.adapter.write_counts("tx-cap")["withdrawals"], 1)

    def test_the_finalize_status_read_is_recorded_and_never_counted(self):
        harness = self._staged()
        harness.gateway.commit(token=permit("COMMIT", self.base, 2))
        harness.gateway.finalize_live(token=permit("FINALIZE_LIVE",
                                                   config_hash({
                                                       **baseline_config(),
                                                       CAP.axis: APPLIED_CAP}), 3))
        reads = [entry for entry in harness.adapter.operations("tx-cap")
                 if entry.operation is R1PolicyOperation.STATUS]
        self.assertTrue(reads)
        self.assertEqual(harness.adapter.write_counts("tx-cap")["applies"], 1)


class TheRecordIsDurableBeforeTheCall(unittest.TestCase):
    """WP-R10 finding 1: a completed write must not be able to go unrecorded.

    The port is touched only after the in-flight record has been appended and
    ``fsync``-ed, so the window in which a crash loses a real E2 write does not
    exist.  What a crash leaves instead is an unresolved record, which counts
    as one possible write -- the only safe reading.
    """

    def setUp(self):
        self.base = config_hash(baseline_config())
        self.directory = Path(tempfile.mkdtemp())

    def test_the_record_is_on_disk_before_the_port_is_touched(self):
        seen: list = []
        journal = JsonlR1OperationJournal(self.directory / "ops.jsonl")

        class _Watching(CountingCapPolicyPort):
            def create_policy(self, ric, policy_type_id, body):
                # What a crash at this instant would have found on disk.
                seen.append(
                    (self.owner_path.read_text(encoding="utf-8").splitlines()))
                return super().create_policy(ric, policy_type_id, body)

        harness = build_cap_harness(operation_journal=journal,
                                    port_class=_Watching)
        harness.port.owner_path = journal.path
        harness.gateway.prepare(token=permit("PREPARE", self.base, 0),
                                plan=harness.plan(steering=False))
        harness.gateway.ready(token=permit("READY", self.base, 1))
        harness.gateway.commit(token=permit("COMMIT", self.base, 2))

        self.assertTrue(seen, "the create was never reached")
        lines = [json.loads(line) for line in seen[0]]
        pending = [entry for entry in lines
                   if entry["operation"] == "CREATE"
                   and entry["outcome"] == "IN_FLIGHT"]
        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0]["writeMayHaveOccurred"])

    def test_a_crash_between_the_call_and_its_answer_reads_as_one_write(self):
        # The durable file as a crashed process would leave it: the in-flight
        # record written, the resolution never appended.
        path = self.directory / "ops.jsonl"
        journal = JsonlR1OperationJournal(path)
        journal.append(_entry(1, R1PolicyOperation.CREATE,
                              R1OperationOutcome.IN_FLIGHT))

        restarted = JsonlR1OperationJournal(path)
        self.assertEqual(restarted.counts("tx-1"),
                         {"applies": 1, "withdrawals": 0,
                          "refused": 0, "unknown": 1})
        self.assertEqual(len(restarted.unresolved()), 1)
        # And the same reading is reached by folding the file directly.
        self.assertEqual(write_counts(restarted.operations()),
                         restarted.counts())

    def test_a_resolution_replaces_the_in_flight_reading_and_never_adds(self):
        journal = InMemoryR1OperationJournal()
        journal.append(_entry(1, R1PolicyOperation.CREATE,
                              R1OperationOutcome.IN_FLIGHT))
        journal.append(R1Operation(sequence=2, transaction_id="tx-1",
                                   operation=R1PolicyOperation.CREATE,
                                   outcome=R1OperationOutcome.ISSUED,
                                   resolves=1))
        self.assertEqual(journal.counts("tx-1"),
                         {"applies": 1, "withdrawals": 0,
                          "refused": 0, "unknown": 0})
        self.assertEqual(journal.unresolved(), ())
        self.assertEqual(write_counts(journal.operations()),
                         journal.counts())

    def test_a_refusal_resolution_takes_the_apply_back_off_the_count(self):
        journal = InMemoryR1OperationJournal()
        journal.append(_entry(1, R1PolicyOperation.CREATE,
                              R1OperationOutcome.IN_FLIGHT))
        journal.append(R1Operation(sequence=2, transaction_id="tx-1",
                                   operation=R1PolicyOperation.CREATE,
                                   outcome=R1OperationOutcome.REFUSED,
                                   resolves=1))
        # The producer answered no: nothing was sent after all.
        self.assertEqual(journal.counts("tx-1"),
                         {"applies": 0, "withdrawals": 0,
                          "refused": 1, "unknown": 0})

    def test_an_in_flight_record_may_not_claim_to_resolve_anything(self):
        with self.assertRaises(ValueError):
            R1Operation(sequence=2, transaction_id="tx-1",
                        operation=R1PolicyOperation.CREATE,
                        outcome=R1OperationOutcome.IN_FLIGHT, resolves=1)


class TheCountsSurviveASaturatedRing(unittest.TestCase):
    """WP-R10 finding 2: a count must not be lost behind the newest entries.

    The ring keeps the most recent records, so a count derived by scanning it
    reports zero applies for a transaction whose CREATE has scrolled out.  The
    tallies are kept as records arrive instead, and stay exact.
    """

    def setUp(self):
        self.base = config_hash(baseline_config())

    def test_a_create_behind_a_full_ring_of_reads_still_counts(self):
        harness = build_cap_harness(
            operation_journal=InMemoryR1OperationJournal(limit=8))
        harness.gateway.prepare(token=permit("PREPARE", self.base, 0),
                                plan=harness.plan(steering=False))
        harness.gateway.ready(token=permit("READY", self.base, 1))
        harness.gateway.commit(token=permit("COMMIT", self.base, 2))
        self.assertEqual(harness.adapter.write_counts("tx-cap")["applies"], 1)

        # Now push the CREATE out of the ring with status reads.  Dispatched
        # on the adapter directly: the gateway would answer the repeats out of
        # its idempotency record and the adapter would never see them, and what
        # is under test here is the adapter's own accounting.
        applied = config_hash({**baseline_config(), CAP.axis: APPLIED_CAP})
        for sequence in range(12):
            token = permit("FINALIZE_LIVE", applied, 3 + sequence,
                           key=f"tx-cap:FINALIZE_LIVE:2:{3 + sequence}")
            harness.adapter.dispatch(
                token=token,
                command=build_command(token, GatewayOperation.FINALIZE,
                                      scope=plan_scope(), index=1))

        retained = harness.adapter.operations("tx-cap")
        self.assertLessEqual(len(retained), 8)
        self.assertNotIn(R1PolicyOperation.CREATE,
                         [entry.operation for entry in retained],
                         "the CREATE must have scrolled out for this to test "
                         "anything")
        # Scanning what is retained would say zero.  The tallies say one.
        self.assertEqual(write_counts(retained)["applies"], 0)
        self.assertEqual(harness.adapter.write_counts("tx-cap")["applies"], 1)

    def test_the_tallies_are_kept_per_transaction(self):
        journal = InMemoryR1OperationJournal(limit=2)
        journal.append(_entry(1, R1PolicyOperation.CREATE))
        journal.append(R1Operation(sequence=2, transaction_id="tx-2",
                                   operation=R1PolicyOperation.DELETE,
                                   outcome=R1OperationOutcome.ISSUED))
        for sequence in range(3, 8):
            journal.append(R1Operation(sequence=sequence, transaction_id="tx-3",
                                       operation=R1PolicyOperation.STATUS,
                                       outcome=R1OperationOutcome.ISSUED))
        self.assertEqual(journal.counts("tx-1")["applies"], 1)
        self.assertEqual(journal.counts("tx-2")["withdrawals"], 1)
        self.assertEqual(journal.counts("tx-3"), {"applies": 0, "withdrawals": 0,
                                                  "refused": 0, "unknown": 0})
        self.assertEqual(journal.counts(), {"applies": 1, "withdrawals": 1,
                                            "refused": 0, "unknown": 0})

    def test_an_unknown_transaction_counts_zero_rather_than_raising(self):
        journal = InMemoryR1OperationJournal()
        self.assertEqual(journal.counts("never-seen"),
                         {"applies": 0, "withdrawals": 0,
                          "refused": 0, "unknown": 0})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

class AJournalWithoutAClockIsRefused(unittest.TestCase):
    """durable 원장을 남기면서 시계를 안 주면 모든 `at` 이 빈 문자열이 된다.

    2026-09-17: `joint_runtime` 의 조종 경로가 `clock=` 없이 어댑터를 만들어
    `r1-steer@*-operations.jsonl` 의 모든 항목이 `"at": ""` 로 쌓였다.  다른 두
    호출부는 `clock=clock.now` 를 주고 있어 파일을 나란히 놓기 전에는 안 드러났다.
    시각 없는 원장은 **되읽기 실패가 언제 났는지 물을 수 없으므로** 조용히 쓸모없다.
    즉시 거절한다.
    """

    def test_an_operation_journal_without_a_clock_raises(self):
        from assurance.gateway.r1_adapter import R1Adapter
        with self.assertRaises(ValueError) as caught:
            R1Adapter(policy_port=object(), policy_builder=lambda command: {},
                      policy_type_id="T", near_rt_ric_id="R",
                      operation_journal=InMemoryR1OperationJournal(),
                      name="r1-steer@ue1")
        self.assertIn("needs a clock", str(caught.exception))
        self.assertIn("r1-steer@ue1", str(caught.exception))

    def test_a_clock_makes_it_acceptable(self):
        from assurance.gateway.r1_adapter import R1Adapter
        adapter = R1Adapter(policy_port=object(), policy_builder=lambda command: {},
                            policy_type_id="T", near_rt_ric_id="R",
                            operation_journal=InMemoryR1OperationJournal(),
                            clock=lambda: "2026-09-17T08:00:00Z",
                            name="r1-steer@ue1")
        self.assertEqual(adapter._clock(), "2026-09-17T08:00:00Z")

    def test_no_journal_still_needs_no_clock(self):
        """원장을 안 주면 시계도 필요 없다 -- 기존 호출부를 깨지 않는다."""
        from assurance.gateway.r1_adapter import R1Adapter
        adapter = R1Adapter(policy_port=object(), policy_builder=lambda command: {},
                            policy_type_id="T", near_rt_ric_id="R", name="r1")
        self.assertEqual(adapter._clock(), "")

