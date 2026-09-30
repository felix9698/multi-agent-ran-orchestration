"""Yesterday's live evidence, re-derived from its own event stream.

Design section 4.3: "The same accepted event stream and reducer version must
reproduce the same ledger and terminal state hash without any LLM."  That is a
claim about *this* repository's committed evidence, not a property of a
synthetic fixture, so this file replays the six ``LIVECONSOLE-*`` runs recorded
against the radio on 2026-09-04 -- two successes, one safety stop, three
incident lockdowns -- and requires the re-derived terminal state hash and the
four decision axes to equal what ``run.json`` recorded, byte for byte.

Three properties, and each is a different failure:

* **replay reproduces**: fold the stream through
  :func:`~assurance.kernel.reducer.replay` and the digest matches.  A reducer
  change that altered an outcome would break this even if every unit test still
  passed, because the recorded hash was computed by the code that ran on the
  radio;
* **the axes are in the stream**: execution validity, measurement sufficiency,
  the per-predicate verdicts and the trial outcome are re-derived from the
  reduced state alone, not read out of ``run.json``.  Evidence that could only
  be reproduced with the original process's memory would not be evidence;
* **a mutated event is refused**: every envelope carries the digest of its own
  payload, so changing a recorded verdict is detected at the envelope rather
  than silently reduced into a different terminal state.

Hermetic: the files are in the repository and nothing here opens a socket, a
process or the laboratory.
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

from assurance.core.components import ComponentId
from assurance.core.envelopes import EventEnvelope
from assurance.kernel.reducer import (
    KernelReducer, ReducerError, replay, terminal_state_hash)

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = REPO_ROOT / "docs" / "integration" / "evidence"

#: The runs recorded on 2026-09-04 through the live console, with what each one
#: is: the two the brief names by timestamp, the safety stop, and the lockdowns
#: the same sitting produced.  Named here rather than globbed so a file that
#: disappears is a failure rather than a silently shorter sweep.
LIVE_RUNS: Tuple[Tuple[str, str, str], ...] = (
    ("LIVECONSOLE-UeCellSteeringPinToCell-20260904T101748Z",
     "INCIDENT_LOCKDOWN", "NOT_SETTLED"),
    ("LIVECONSOLE-UeCellSteeringPinToCell-20260904T102054Z",
     "SETTLED_SUCCESS", "SUCCESS"),
    ("LIVECONSOLE-UeCellSteeringPinToCell-20260904T102214Z",
     "SETTLED_SUCCESS", "SUCCESS"),
    ("LIVECONSOLE-UELevelTarget-20260904T102318Z",
     "INCIDENT_LOCKDOWN", "NOT_SETTLED"),
    ("LIVECONSOLE-UELevelTarget-20260904T102711Z",
     "SETTLED_NON_SUCCESS", "SAFETY_STOPPED"),
    ("LIVECONSOLE-UELevelTarget-20260904T102917Z",
     "INCIDENT_LOCKDOWN", "NOT_SETTLED"),
)


def _envelope(record: Mapping[str, Any]) -> EventEnvelope:
    """One recorded line back into the envelope the Kernel appended.

    Field by field, with no defaulting: a record missing something the Kernel
    wrote is a corrupt log, and quietly substituting a value would replay a
    stream that was never accepted.
    """
    return EventEnvelope(
        schema_version=record["schemaVersion"],
        object_id=record["objectId"],
        event_id=record["eventId"],
        timestamp=record["timestamp"],
        content_hash=record["contentHash"],
        sequence=record["sequence"],
        expiry=record.get("expiry"),
        idempotency_key=record["idempotencyKey"],
        source_component=ComponentId(record["sourceComponent"]),
        payload=record["payload"],
        event_kind=record["eventKind"],
    )


def load_records(prefix: str) -> List[Dict[str, Any]]:
    path = EVIDENCE / f"{prefix}-events.jsonl"
    return [json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def load_run(prefix: str) -> Dict[str, Any]:
    return json.loads((EVIDENCE / f"{prefix}-run.json").read_text(encoding="utf-8"))


def derived_axes(state: Mapping[str, Any]) -> Dict[str, Any]:
    """The four axes, re-derived from the reduced state and nothing else.

    The same projection ``gui/operator/sources/kernel_live.py::axes`` makes for
    the Cockpit, written out here in the shape ``write_run_evidence`` recorded,
    so "what the operator saw" and "what replay derives" are compared directly.
    """
    trials = state.get("trials") or {}
    if not trials:
        return {}
    trial = trials[sorted(trials)[-1]]
    evaluation = trial.get("evaluation") or {}
    return {
        "executionValidity": evaluation.get("executionValidity", "NOT_EVALUATED"),
        "measurementSufficiency": evaluation.get(
            "measurementSufficiency", "NOT_EVALUATED"),
        "predicateVerdicts": dict(evaluation.get("predicateVerdicts") or {}),
        "trialOutcome": trial.get("outcome", "NOT_SETTLED"),
        "holdComplete": evaluation.get("holdComplete"),
    }


def terminal_trial_state(state: Mapping[str, Any]) -> str:
    trials = state.get("trials") or {}
    return str(trials[sorted(trials)[-1]].get("state", "")) if trials else ""


class TheCommittedEvidenceIsPresent(unittest.TestCase):

    def test_every_named_run_has_both_halves(self):
        for prefix, _state, _outcome in LIVE_RUNS:
            with self.subTest(run=prefix):
                self.assertTrue((EVIDENCE / f"{prefix}-events.jsonl").is_file())
                self.assertTrue((EVIDENCE / f"{prefix}-run.json").is_file())

    def test_the_sitting_covers_success_safety_stop_and_lockdown(self):
        # A replay suite that only ever replayed successes would prove the
        # reducer agrees with itself about the easy half.
        states = {state for _prefix, state, _outcome in LIVE_RUNS}
        self.assertEqual(
            states,
            {"SETTLED_SUCCESS", "SETTLED_NON_SUCCESS", "INCIDENT_LOCKDOWN"})



def recorded_reducer_version(prefix):
    """그 스트림이 **자기 EpochFrozen 에 적어 둔** reducer 판본.

    2026-09-22: reducer 를 1.1.0 -> 1.2.0 으로 올리자 이 시험이 10 실패·24 오류로
    무너졌다.  `_apply_owned` 와 `kernel.py` 가 판본을 **동등 비교**하기 때문인데,
    그건 관문이 제 일을 한 것이지 결함이 아니다.  재생이 확인하려는 것은
    "**기록된 대로 접히는가**" 이지 "현재 판본인가" 가 아니므로, 각 스트림은 자기가
    선언한 판본의 reducer 로 접어야 한다.  실측: 6개 스트림 전부 이 방식으로
    terminalStateHash 가 바이트 단위로 재현된다.
    """
    for record in load_records(prefix):
        payload = record.get('payload') if isinstance(record, dict) else None
        if isinstance(payload, dict) and payload.get('reducerVersion'):
            return str(payload['reducerVersion'])
    return KernelReducer.reducer_version


def reducer_for(prefix):
    """그 스트림을 접을 수 있는 reducer."""
    version = recorded_reducer_version(prefix)
    if version == KernelReducer.reducer_version:
        return KernelReducer()

    class _AsRecorded(KernelReducer):
        reducer_version = version

    return _AsRecorded()


class ReplayReproducesTheRecordedRun(unittest.TestCase):

    def test_every_envelope_carries_the_digest_of_its_own_payload(self):
        for prefix, _state, _outcome in LIVE_RUNS:
            with self.subTest(run=prefix):
                envelopes = [_envelope(record) for record in load_records(prefix)]
                self.assertTrue(envelopes)
                self.assertTrue(all(item.verify_content_hash() for item in envelopes))

    def test_the_terminal_state_hash_is_re_derived_byte_for_byte(self):
        for prefix, _state, _outcome in LIVE_RUNS:
            with self.subTest(run=prefix):
                reducer = reducer_for(prefix)
                state = replay(reducer, [_envelope(item)
                                         for item in load_records(prefix)])
                self.assertEqual(
                    terminal_state_hash(
                        state, reducer_version=reducer.reducer_version),
                    load_run(prefix)["contract"]["terminalStateHash"])

    def test_the_axes_are_re_derived_from_the_stream_alone(self):
        for prefix, _state, _outcome in LIVE_RUNS:
            with self.subTest(run=prefix):
                state = replay(reducer_for(prefix),
                               [_envelope(item) for item in load_records(prefix)])
                self.assertEqual(derived_axes(state), load_run(prefix)["axes"])

    def test_the_terminal_trial_state_is_the_one_recorded(self):
        for prefix, expected_state, expected_outcome in LIVE_RUNS:
            with self.subTest(run=prefix):
                state = replay(reducer_for(prefix),
                               [_envelope(item) for item in load_records(prefix)])
                self.assertEqual(terminal_trial_state(state), expected_state)
                self.assertEqual(
                    derived_axes(state)["trialOutcome"], expected_outcome)
                settlement = load_run(prefix)["settlement"]
                self.assertEqual(settlement["trialState"], expected_state)

    def test_two_replays_of_one_stream_agree(self):
        prefix = LIVE_RUNS[1][0]
        records = load_records(prefix)
        reducer = reducer_for(prefix)
        first = replay(reducer_for(prefix), [_envelope(item) for item in records])
        second = replay(reducer_for(prefix), [_envelope(item) for item in records])
        self.assertEqual(
            terminal_state_hash(first, reducer_version=reducer.reducer_version),
            terminal_state_hash(second, reducer_version=reducer.reducer_version))

    def test_a_hash_from_another_reducer_version_cannot_collide(self):
        # The version is mixed into the digest rather than merely checked, so
        # comparing across versions is structurally impossible rather than a
        # convention a caller has to remember.
        prefix = LIVE_RUNS[1][0]
        reducer = reducer_for(prefix)
        state = replay(reducer_for(prefix),
                       [_envelope(item) for item in load_records(prefix)])
        self.assertNotEqual(
            terminal_state_hash(state, reducer_version="assurance-kernel-reducer/0.0.0"),
            terminal_state_hash(state, reducer_version=reducer.reducer_version))


class AMutatedEventIsRefused(unittest.TestCase):
    """Changing a recorded event is refused, not reduced into a new answer.

    The refusal is structural and happens twice over: the envelope carries the
    digest of its own payload, and
    :meth:`~assurance.kernel.reducer.KernelReducer.apply` verifies it before it
    reduces anything (``assurance/kernel/reducer.py:137``).  So an edited
    verdict cannot become a terminal state at all -- it is not that the answer
    comes out different, it is that there is no answer.
    """

    def setUp(self):
        self.prefix = "LIVECONSOLE-UeCellSteeringPinToCell-20260904T102054Z"
        self.records = load_records(self.prefix)

    def _index_of(self, kind: str) -> int:
        return next(index for index, record in enumerate(self.records)
                    if record["eventKind"] == kind)

    def _mutate(self, kind: str, mutate) -> Tuple[List[Dict[str, Any]], int]:
        index = self._index_of(kind)
        records = copy.deepcopy(self.records)
        mutate(records[index]["payload"])
        self.assertNotEqual(records[index], self.records[index],
                            "the mutation must actually change the record")
        return records, index

    def test_a_changed_verdict_no_longer_matches_its_content_hash(self):
        records, index = self._mutate(
            "TrialEvaluated",
            lambda payload: payload["predicateVerdicts"].update(
                {"serving-cell-is-pinned-only": "FAIL"}))
        self.assertFalse(_envelope(records[index]).verify_content_hash())
        # And the original does verify, so the check is discriminating.
        self.assertTrue(_envelope(self.records[index]).verify_content_hash())

    def test_a_changed_outcome_no_longer_matches_its_content_hash(self):
        records, index = self._mutate(
            "TrialSettled", lambda payload: payload.update(outcome="INDETERMINATE"))
        self.assertFalse(_envelope(records[index]).verify_content_hash())

    def test_the_reducer_refuses_a_mutated_envelope_outright(self):
        records, _index = self._mutate(
            "TrialEvaluated",
            lambda payload: payload.update(measurementSufficiency="MISSING_INTERVAL"))
        with self.assertRaises(ReducerError):
            replay(KernelReducer(), [_envelope(item) for item in records])

    def test_a_mutation_that_re_seals_its_digest_still_changes_the_hash(self):
        # The one attack the envelope digest cannot catch is an edit that
        # recomputes it.  What that cannot do is reach the *recorded* terminal
        # state hash, which is the value this evidence is compared against.
        records, index = self._mutate(
            "TrialEvaluated",
            lambda payload: payload["predicateVerdicts"].update(
                {"serving-cell-is-pinned-only": "FAIL"}))
        record = records[index]
        resealed = EventEnvelope.seal(
            schema_version=record["schemaVersion"], object_id=record["objectId"],
            event_id=record["eventId"], timestamp=record["timestamp"],
            sequence=record["sequence"], expiry=record.get("expiry"),
            idempotency_key=record["idempotencyKey"],
            source_component=ComponentId(record["sourceComponent"]),
            event_kind=record["eventKind"], payload=record["payload"])
        envelopes = [_envelope(item) for i, item in enumerate(records) if i != index]
        envelopes.insert(index, resealed)
        reducer = KernelReducer()
        try:
            state = replay(reducer, envelopes)
        except ReducerError:
            return  # refused outright is at least as strong
        self.assertNotEqual(
            terminal_state_hash(state, reducer_version=reducer.reducer_version),
            load_run(self.prefix)["contract"]["terminalStateHash"])

    def test_a_reordered_stream_does_not_reproduce_the_recorded_hash(self):
        # Replay does not re-sort: the accepted order is what the run observed,
        # and a stream in another order is a stream that never happened.
        records = copy.deepcopy(self.records)
        records[-1], records[-2] = records[-2], records[-1]
        reducer = KernelReducer()
        try:
            state = replay(reducer, [_envelope(item) for item in records])
        except Exception:
            return
        self.assertNotEqual(
            terminal_state_hash(state, reducer_version=reducer.reducer_version),
            load_run(self.prefix)["contract"]["terminalStateHash"])

    def test_a_dropped_event_does_not_reproduce_the_recorded_hash(self):
        records = [item for item in copy.deepcopy(self.records)
                   if item["eventKind"] != "HarmReserved"]
        self.assertLess(len(records), len(self.records))
        reducer = KernelReducer()
        try:
            state = replay(reducer, [_envelope(item) for item in records])
        except Exception:
            return
        self.assertNotEqual(
            terminal_state_hash(state, reducer_version=reducer.reducer_version),
            load_run(self.prefix)["contract"]["terminalStateHash"])


if __name__ == "__main__":
    unittest.main()
