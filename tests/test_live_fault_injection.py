"""Seven ways the live path can fail, injected hermetically.

**What is observed and what is a regression.**  Two of these seven are recorded
on the radio on 2026-09-04 and this module asserts against the committed
records; the other five are hermetic regressions -- behaviour the composition
must have, injected here because no committed 2026-09-04 run exercises it.
Every class says which it is on its first line, and no class claims OTA
provenance it cannot show a record for:

===========================================  ============================  ==================================================
fault                                        provenance                    committed record
===========================================  ============================  ==================================================
1. the A1 producer answers 503               OBSERVED 2026-09-04           ``LIVECONSOLE-UELevelTarget-20260904T102711Z``
2. the UE vanishes mid-handover              OBSERVED 2026-09-04           ``…PinToCell-20260904T101748Z``, ``…UELevelTarget-20260904T{102318,102917}Z``
3. the KPM stream is stale                   HERMETIC REGRESSION           --
4. the KPM JSONL is rotated or truncated     HERMETIC REGRESSION           --
5. the producer refuses the policy           HERMETIC REGRESSION           --
6. the cap readback is from another cell     HERMETIC REGRESSION           --
7. the cap fails after the steering landed   HERMETIC REGRESSION           --
===========================================  ============================  ==================================================

The six committed 2026-09-04 runs are steering-only, so nothing about the
SUPPLEMENTARY UE DL PRB cap (faults 6 and 7) or about a gate restart (fault 4)
can be claimed as an OTA observation, and none is.

**Where the numbers come from.**  Every write count here is read off a
production record -- the R1 adapter's operation journal
(``assurance/gateway/r1_operation_journal.py``) and the producer's control
records -- through ``CapHarness.control_evidence``.  The stood-in worker's own
list is a diagnostic that ``assert_diagnostic_agrees`` checks against those
records rather than a number any claim rests on.

Each case asserts the four things that make a failure *safe* rather than merely
survived:

* the Kernel's terminal state and its four axes -- separately, because
  "nobody could measure it" is not "the objective failed";
* the gateway operation sequence, so the order a recovery took is a fact and
  not a story;
* **no unsafe write**: the count of writes that reached the equipment, which is
  the only number that says whether a refusal was really a refusal;
* that the operator-facing text names the cause, because a run an operator
  cannot act on is a run that will be repeated.

Injected ports only.  No socket, no process, no radio, no model.  Two faults --
the ones about the A1 producer and the supplementary cap -- are driven through
the real ``TokenBoundWriteGateway`` and the real ``R1Adapter`` over the in-repo
Campaign 5 producer, because the behaviour under test is the adapter's; the
composition root that wires that adapter is covered by
``tests/test_liveconsole_action102.py``.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from assurance.core.states import TrialState
from assurance.gateway.mock_adapter import FaultInjection, MockActuationAdapter
from assurance.gateway.plan import config_hash
from assurance.gateway.r1_binding_journal import BindingState
from assurance.gateway.write_gateway import GatewayOutcome

from tools.g3ota.composition import KpmTail
from tools.liveconsole import LiveConsoleError

from tests.assurance.action102_support import (
    APPLIED_CAP, CAP, CAP_FAMILY, UNCAPPED, CapFaults, applied_config,
    baseline_config, build_cap_harness, permit,
)
from tests.test_liveconsole import (
    AMF_UE_NGAP_ID, HOME_NCI, SittingFixture, occupant,
)

from oran.campaign5.producer import A1Conflict, A1NotFound, A1ValidationError


def _committed_run(run_id: str) -> Dict[str, Any]:
    """One committed ``LIVECONSOLE-*-run.json`` from the repository.

    Reading it keeps the module hermetic -- the file is in the repository -- and
    keeps an OTA provenance claim checkable: a class that says a fault was
    observed on 2026-09-04 asserts against the record that observed it.
    """
    path = (Path(__file__).resolve().parents[1] / "docs" / "integration"
            / "evidence" / f"{run_id}-run.json")
    return json.loads(path.read_text(encoding="utf-8"))


class _Unavailable(RuntimeError):
    """What an R1 consumer raises when the endpoint answers 503.

    Deliberately *not* one of the adapter's refusal types.  A 503 says the
    service did not serve the request; it does not prove the request never
    reached it, and the difference between "refused" and "unknown" is the
    difference between aborting a trial and opening a recovery.
    """


# --------------------------------------------------------------------------- #
# 1. the producer is wedged (HTTP 503 on create_policy)
# --------------------------------------------------------------------------- #


class AWedgedProducerWritesNothing(unittest.TestCase):
    """Fault 1 -- **OBSERVED 2026-09-04**, ``LIVECONSOLE-UELevelTarget-20260904T102711Z``.

    The A1 producer's worker was wedged in recovery after the failed handover
    and answered HTTP 503 to ``create_policy``.  The committed record shows what
    that cost: the commit's answer is ``UNKNOWN`` -- the request went out and
    nothing came back -- while ``policyStatus`` is empty, so no policy was ever
    accepted, and the case settled ``SAFETY_STOPPED`` with the harm returned.

    The injection here drives the cap participant rather than the steering one,
    because the behaviour under test belongs to ``R1Adapter`` and is the same
    for either policy type; :meth:`test_it_matches_the_committed_503_record`
    ties the assertions back to the run that actually happened.  What both say
    is the property that makes a 503 safe and does not depend on which label
    the transport chose: **no control reached the equipment**, no policy
    exists, no transaction was bound, and the resource is left blocked rather
    than reported clean.
    """

    def setUp(self):
        self.harness = build_cap_harness(
            faults=CapFaults(refuse_create=_Unavailable))
        self.base = config_hash(baseline_config())

    def _commit(self):
        """One supplementary control and nothing else, so the whole submission
        is the one the wedged producer refuses."""
        plan = self.harness.plan(steering=False)
        self.harness.gateway.prepare(
            token=permit("PREPARE", self.base, 0), plan=plan)
        self.harness.gateway.ready(token=permit("READY", self.base, 1))
        return self.harness.gateway.commit(token=permit("COMMIT", self.base, 2))

    def test_the_commit_never_reaches_the_scheduler(self):
        result = self._commit()
        evidence = self.harness.assert_diagnostic_agrees(self)
        # A 503 is not a refusal: the request went out and the answer was lost,
        # so the adapter records one apply whose fate it cannot state.  What
        # makes the run safe is the second number -- the producer holds no
        # record of a control, and no policy exists to have caused one.
        self.assertEqual(evidence.applies_sent, 1)
        self.assertEqual(evidence.unknown, 1)
        self.assertEqual(evidence.refused, 0)
        self.assertEqual(evidence.controls_reaching_ran, 0)
        self.assertEqual(self.harness.live_cap(), int(UNCAPPED))
        self.assertEqual(
            self.harness.producer.list_policies(CAP_FAMILY.policy_type_id), [])
        self.assertIsNone(self.harness.adapter.bound_policy("tx-cap"))
        self.assertNotIn(result.outcome,
                         (GatewayOutcome.ACKED, GatewayOutcome.ALREADY_APPLIED))

    def test_an_unavailable_endpoint_is_unknown_not_a_clean_rejection(self):
        # A 503 is conservative on purpose: the gateway cannot prove the write
        # did not land, so it says so and the Kernel opens a recovery rather
        # than settling a trial on the belief that nothing happened.
        result = self._commit()
        self.assertIn(result.outcome,
                      (GatewayOutcome.UNKNOWN, GatewayOutcome.PARTIAL_APPLY))
        record = self.harness.transaction_journal.read("tx-cap")
        self.assertTrue(record.is_uncertain)
        self.assertEqual(self.harness.gateway.uncertain_transactions(), ("tx-cap",))

    def test_the_retained_evidence_names_the_cause(self):
        self._commit()
        failures = [self.harness.gateway.evidence(ref)
                    for ref in self.harness.gateway.evidence_references()
                    if self.harness.gateway.evidence(ref)["operation"] == "APPLY"]
        self.assertEqual([item["outcome"] for item in failures], ["UNKNOWN"])
        self.assertTrue(any("_Unavailable" in item["detail"] for item in failures))

    def test_it_matches_the_committed_503_record(self):
        """The committed run says the same three things this injection does."""
        recorded = _committed_run("LIVECONSOLE-UELevelTarget-20260904T102711Z")
        create = [call for call in recorded["transportCalls"]
                  if call["method"] == "create_policy"]
        self.assertEqual([call["outcome"] for call in create], ["RAISED"])
        self.assertIn("503", create[0]["detail"])
        # 1. no policy was ever accepted, so nothing can have been enforced;
        self.assertEqual(recorded["policyStatus"], [])
        # 2. the commit's answer was lost rather than refused;
        self.assertEqual(
            [outcome for _kind, outcome in recorded["settlement"]["gatewayOperations"]],
            ["ACKED", "ACKED", "UNKNOWN"])
        # 3. and the case stopped rather than settling on that uncertainty,
        #    returning the harm it had reserved.
        self.assertEqual(recorded["settlement"]["outcome"], "SAFETY_STOPPED")
        self.assertEqual(recorded["settlement"]["harmCharges"][-1][:6], "RETURN")

        # The injection reproduces the first two: an unresolved commit with no
        # policy behind it.  It does not reproduce the settlement, which is the
        # Kernel's answer and is covered by the sitting tests.
        result = self._commit()
        self.assertIn(result.outcome,
                      (GatewayOutcome.UNKNOWN, GatewayOutcome.PARTIAL_APPLY))
        self.assertEqual(
            self.harness.producer.list_policies(CAP_FAMILY.policy_type_id), [])

    def test_the_settled_detail_says_both_halves(self):
        # The safety-bearing half -- a write may yet have landed -- and the
        # reason, which an operator needs to tell a wedged producer from a lost
        # acknowledgement.  Both, on the line the Cockpit renders.
        result = self._commit()
        self.assertIn("may yet land", result.detail)
        self.assertIn("_Unavailable", result.detail)


# --------------------------------------------------------------------------- #
# 2. the UE vanished mid-handover: applied, unverifiable
# --------------------------------------------------------------------------- #


class AUeThatVanishesMidHandoverEndsInLockdown(SittingFixture):
    """Fault 2 -- **OBSERVED 2026-09-04**: three committed lockdown records.

    ``LIVECONSOLE-UeCellSteeringPinToCell-20260904T101748Z`` and the two
    ``UELevelTarget`` lockdowns all end the same way: PREPARE and READY
    acknowledged, COMMIT ``UNKNOWN``, stop reason ``PARTIAL_APPLY``, trial state
    ``INCIDENT_LOCKDOWN``.  The write may have landed and nobody can say, which
    is the one situation a trial must never settle out of.
    """

    def _wedged_sitting(self, **kwargs):
        adapter = MockActuationAdapter(
            config={"servingCell": str(HOME_NCI)},
            faults=FaultInjection(
                # The write lands; the acknowledgement does not; and the reread
                # that would resolve it cannot be taken either.
                drop_ack_axes=frozenset({"servingCell"}),
                unreadable_after_reads=4))
        adapter.hosts_watchdogs = False
        return adapter, self.sitting(adapter=adapter, **kwargs)

    def test_it_locks_down_rather_than_settling(self):
        adapter, live = self._wedged_sitting()
        view = self.run_case(live, self.sentence())
        settlement = view.settlement
        self.assertEqual(settlement.trial_state, TrialState.INCIDENT_LOCKDOWN.value)
        self.assertEqual(settlement.outcome, "NOT_SETTLED")
        self.assertEqual(settlement.stop_reason, "PARTIAL_APPLY")
        self.assertEqual([outcome for _kind, outcome in settlement.gateway_operations],
                         ["ACKED", "ACKED", "UNKNOWN"])
        # The axes are not evaluated, separately: an unresolvable execution is
        # not a KPI failure and must not be recorded as one.
        self.assertEqual(view.axes.execution_validity, "NOT_EVALUATED")
        self.assertEqual(view.axes.measurement_sufficiency, "NOT_EVALUATED")
        self.assertEqual(view.axes.predicate_verdicts, ())
        # The write did land -- which is precisely why it cannot be dismissed.
        # 2026-09-23: and since then it is also **withdrawn** before the lockdown.
        # The reread cannot be taken, which is uncertain, not foreign; the Kernel
        # now sends the reversal anyway and the gateway, unable to read first,
        # reverses the whole plan (live board 20260923T083611 left two policies at
        # the RIC because nothing was reversed).  The confirming read still cannot
        # be taken, so nothing is counted as restored and the trial still locks down.
        self.assertEqual([command["axis"] for command in adapter.writes],
                         ["servingCell", "servingCell"])
        self.assertEqual(["APPLY", "UNDO"],
                         [str(command["operation"]) for command in adapter.writes])

    def test_it_matches_the_committed_lockdown_records(self):
        _adapter, live = self._wedged_sitting()
        view = self.run_case(live, self.sentence())
        recorded = _committed_run("LIVECONSOLE-UELevelTarget-20260904T102318Z")
        self.assertEqual(view.settlement.trial_state,
                         recorded["settlement"]["trialState"])
        self.assertEqual(view.settlement.stop_reason,
                         recorded["settlement"]["stopReason"])
        self.assertEqual(
            [list(item) for item in view.settlement.gateway_operations],
            recorded["settlement"]["gatewayOperations"])

    def test_the_next_case_is_blocked_by_the_occupant_it_left(self):
        # WP-L's precondition: the policy the lockdown left on the UE scope is
        # named in the next case's preview and is not withdrawn as a side
        # effect of composing one.
        adapter, live = self._wedged_sitting(
            occupants=(occupant("p-lockdown", episode_state="APPLIED_VERIFIED"),),
            scope_clearer=self.record_clearer())
        self.run_case(live, self.sentence())
        preview = live.session.draft(self.sentence())
        self.assertTrue(preview.preconditions)
        self.assertTrue(any("p-lockdown" in item for item in preview.preconditions))
        # And it is covered by the hash the operator confirms.
        self.assertIn("preconditions", preview.to_canonical_dict())
        del adapter


# --------------------------------------------------------------------------- #
# 3. the KPM stream is stale
# --------------------------------------------------------------------------- #


class AStaleKpmStreamRefusesBeforeItGuesses(SittingFixture):
    """Fault 3 -- **HERMETIC REGRESSION**, both halves.

    No committed 2026-09-04 run was refused for a stale stream; all six had a
    fresh attribution.  This is the behaviour the composition must have, not an
    OTA observation.

    At prepare a stale stream means there is no identity to address, so the
    case is refused before a deployment is touched.  Mid-trial it means the
    windows cannot be completed, which is a *measurement* answer and never a
    KPI verdict: an objective is not failed by nobody having measured it.
    """

    def test_a_stale_stream_at_prepare_refuses_with_no_case_opened(self):
        with self.assertRaises(LiveConsoleError) as caught:
            self.compose(lines=[])
        message = str(caught.exception)
        self.assertIn("no fresh KPM UE attribution", message)
        # The refusal states the window and what the reader actually saw, so an
        # operator can tell a silent stream from a stale one.
        self.assertIn("freshness window", message)
        self.assertIn("record(s) discarded", message)

    def test_a_named_ue_that_is_not_observable_is_refused_by_name(self):
        with self.assertRaises(LiveConsoleError) as caught:
            self.compose(amf_ue_ngap_id=4242)
        self.assertIn("4242", str(caught.exception))
        self.assertIn("not observable", str(caught.exception))

    def test_a_stream_that_goes_stale_mid_trial_is_a_measurement_answer(self):
        live = self.sitting()
        # No observation at all for the whole hold: the collector answers with
        # a gap rather than carrying the previous value forward.
        view = self.run_case(live, self.sentence(), observed=[])
        self.assertIn(view.axes.measurement_sufficiency,
                      ("MISSING_INTERVAL", "INSUFFICIENT_COVERAGE"))
        self.assertNotEqual(view.axes.predicate_verdict, "FAIL")
        self.assertNotEqual(view.settlement.outcome, "FAILURE")


# --------------------------------------------------------------------------- #
# 4. the gate restarted and the JSONL was rotated
# --------------------------------------------------------------------------- #


class ARotatedJsonlNeverMisAttributes(unittest.TestCase):
    """Fault 4 -- **HERMETIC REGRESSION**: the gate restarts, the file is replaced.

    No committed 2026-09-04 run saw a rotation; the gate stayed up across all
    six.  The tail is asserted here directly, over temporary files, because a
    reader that can mis-attribute one UE's indication to another is a defect
    whether or not it has yet cost a run.

    The tail is a byte offset, so a file that shrank under it is the one case
    where a naive reader would read from the middle of a record.  What matters
    for safety is that it never does: it either re-syncs or reports nothing,
    and reporting nothing becomes ``MISSING_INTERVAL`` rather than an
    attribution of one UE's indication to another.
    """

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.path = self.directory / "a1-live-kpm.jsonl"

    def _line(self, marker: str) -> str:
        return json.dumps({"event": "kpm_indication", "marker": marker})

    def test_a_growing_file_yields_each_complete_line_once(self):
        self.path.write_text(self._line("a") + "\n", encoding="utf-8")
        tail = KpmTail(self.path, prime_bytes=0)
        self.path.write_text(self._line("a") + "\n" + self._line("b") + "\n",
                             encoding="utf-8")
        self.assertEqual(list(tail.read_new_lines()), [self._line("b")])
        self.assertEqual(list(tail.read_new_lines()), [])

    def test_a_partial_line_is_left_for_the_next_read(self):
        self.path.write_text("", encoding="utf-8")
        tail = KpmTail(self.path, prime_bytes=0)
        self.path.write_text(self._line("a") + "\n" + '{"event":"kpm_ind',
                             encoding="utf-8")
        self.assertEqual(list(tail.read_new_lines()), [self._line("a")])
        self.path.write_text(self._line("a") + "\n" + self._line("b") + "\n",
                             encoding="utf-8")
        self.assertEqual(list(tail.read_new_lines()), [self._line("b")])

    def test_a_rotated_file_never_yields_a_fragment(self):
        self.path.write_text(self._line("first") + "\n" + self._line("second") + "\n",
                             encoding="utf-8")
        tail = KpmTail(self.path, prime_bytes=0)
        self.path.write_text(self._line("x") + "\n", encoding="utf-8")
        # Whatever it answers, every line it answers with is a whole record.
        for line in tail.read_new_lines():
            json.loads(line)

    def test_a_rotated_file_is_re_synced_rather_than_going_silent(self):
        # The offset is a position in a file that no longer exists.  Without a
        # re-sync the tail answers nothing for the rest of the sitting, which
        # is safe and indistinguishable from a gNB that stopped publishing.
        self.path.write_text(self._line("first") + "\n" + self._line("second") + "\n",
                             encoding="utf-8")
        tail = KpmTail(self.path, prime_bytes=0)
        self.assertEqual(tail.rotations, 0)
        self.path.write_text(self._line("after-restart") + "\n", encoding="utf-8")
        self.assertEqual(list(tail.read_new_lines()), [self._line("after-restart")])
        self.assertEqual(tail.rotations, 1)
        # And it keeps up from there.
        self.path.write_text(self._line("after-restart") + "\n"
                             + self._line("next") + "\n", encoding="utf-8")
        self.assertEqual(list(tail.read_new_lines()), [self._line("next")])
        self.assertEqual(tail.rotations, 1)

    def test_a_gate_restart_that_empties_the_file_re_syncs_and_is_counted(self):
        # What a restarting gate actually leaves behind: a file that starts
        # again from nothing.  The offset is a position in a stream that no
        # longer exists, and re-syncing is the only answer that tells "the gate
        # restarted" from "the gNB stopped publishing".
        self.path.write_text(self._line("before") + "\n", encoding="utf-8")
        tail = KpmTail(self.path, prime_bytes=0)
        self.path.write_text("", encoding="utf-8")
        self.assertEqual(list(tail.read_new_lines()), [])
        self.path.write_text(self._line("after") + "\n", encoding="utf-8")
        self.assertEqual(list(tail.read_new_lines()), [self._line("after")])
        self.assertEqual(tail.rotations, 1)

    def test_each_replacement_is_counted_so_a_gap_is_visible(self):
        # Counted rather than hidden: the history before a re-sync belongs to a
        # different file, so a reader that needed it has a gap, not a stream.
        self.path.write_text(self._line("aaaaa") + "\n", encoding="utf-8")
        tail = KpmTail(self.path, prime_bytes=0)
        for marker in ("bb", "c"):
            self.path.write_text(self._line(marker) + "\n", encoding="utf-8")
            tail.read_new_lines()
        self.assertEqual(tail.rotations, 2)

    def test_a_vanished_file_answers_nothing_rather_than_raising(self):
        self.path.write_text(self._line("a") + "\n", encoding="utf-8")
        tail = KpmTail(self.path, prime_bytes=0)
        self.path.unlink()
        self.assertEqual(list(tail.read_new_lines()), [])


# --------------------------------------------------------------------------- #
# 5. the producer refuses: AIC_E2_NOT_READY
# --------------------------------------------------------------------------- #


class AProducerRefusalIsARejectionWithNoWrite(unittest.TestCase):
    """Fault 5 -- **HERMETIC REGRESSION**: the producer refuses the policy.

    A capability refusal (``AIC_E2_NOT_READY`` and its schema/conflict
    siblings) is a shape the producer has refused before, but no committed
    2026-09-04 run carries one: every run of that day reached ``create_policy``
    with the gate header present.

    A refusal is a *decision*: the producer answered, and it answered no.  That
    is not the same as a lost message, and reporting it as one would open a
    recovery for a request that never reached the equipment.
    """

    def setUp(self):
        self.base = config_hash(baseline_config())

    def _refused(self, error):
        harness = build_cap_harness(faults=CapFaults(refuse_create=error))
        plan = harness.plan(steering=False)
        harness.gateway.prepare(token=permit("PREPARE", self.base, 0), plan=plan)
        harness.gateway.ready(token=permit("READY", self.base, 1))
        return harness, harness.gateway.commit(token=permit("COMMIT", self.base, 2))

    def test_a_capability_refusal_is_rejected_with_zero_writes(self):
        for error in (A1ValidationError, A1Conflict, A1NotFound):
            with self.subTest(error=error.__name__):
                harness, result = self._refused(error)
                self.assertIs(result.outcome, GatewayOutcome.REJECTED)
                evidence = harness.assert_diagnostic_agrees(self)
                self.assertEqual(evidence.writes_sent, 0)
                self.assertEqual(evidence.refused, 1)
                self.assertEqual(evidence.controls_reaching_ran, 0)
                self.assertEqual(harness.live_cap(), int(UNCAPPED))
                self.assertIsNone(harness.adapter.bound_policy("tx-cap"))
                self.assertEqual(
                    harness.producer.list_policies(CAP_FAMILY.policy_type_id), [])

    def test_the_scope_is_reserved_but_never_bound(self):
        harness, _result = self._refused(A1ValidationError)
        record = harness.cap_binding()
        self.assertIs(record.state, BindingState.RESERVED)
        self.assertIsNone(record.policy_id)

    def test_the_operator_facing_text_names_the_producer_error(self):
        harness, result = self._refused(A1Conflict)
        self.assertIn("A1Conflict", result.detail)
        self.assertIn("baseline confirmed live", result.detail)
        applied = [harness.gateway.evidence(ref)
                   for ref in harness.gateway.evidence_references()
                   if harness.gateway.evidence(ref)["operation"] == "APPLY"]
        self.assertEqual([item["outcome"] for item in applied], ["REJECTED"])


# --------------------------------------------------------------------------- #
# 6. the cap is acknowledged, the readback is from somewhere else
# --------------------------------------------------------------------------- #


class AReadbackFromAnotherCellDoesNotVerify(unittest.TestCase):
    """Fault 6 -- **HERMETIC REGRESSION**: the WP-B3 rule through the gateway.

    The six committed 2026-09-04 runs are steering-only and carry no UE DL PRB
    cap evidence, so nothing here is claimed as an OTA observation.

    The cap is applied and acknowledged, and the only configuration indication
    available belongs to another cell or another E2 association.  An
    acknowledgement is not an effect and neither is somebody else's
    measurement, so the answer is ``UNKNOWN`` and the reversal happens exactly
    once.
    """

    def setUp(self):
        self.base = config_hash(baseline_config())
        self.applied = config_hash(applied_config())

    def test_an_uncorroborated_cap_is_unknown_not_verified(self):
        harness = build_cap_harness(faults=CapFaults(suppress_counter=True))
        harness.gateway.prepare(token=permit("PREPARE", self.base, 0),
                                plan=harness.plan())
        harness.gateway.ready(token=permit("READY", self.base, 1))
        result = harness.gateway.commit(token=permit("COMMIT", self.base, 2))
        self.assertIs(result.outcome, GatewayOutcome.UNKNOWN)
        # The write happened once; the producer says so and does not claim more.
        evidence = harness.assert_diagnostic_agrees(self)
        self.assertEqual(evidence.applies_sent, 1)
        self.assertEqual(evidence.controls_reaching_ran, 1)
        self.assertEqual(evidence.verified, 0)
        status = harness.port.get_policy_status(
            harness.adapter.bound_policy("tx-cap"))
        self.assertNotEqual(status["enforceStatus"], "ENFORCED")
        self.assertTrue(harness.transaction_journal.read("tx-cap").is_uncertain)

    def test_the_reversal_happens_exactly_once(self):
        harness = build_cap_harness()
        harness.gateway.prepare(token=permit("PREPARE", self.base, 0),
                                plan=harness.plan())
        harness.gateway.ready(token=permit("READY", self.base, 1))
        harness.gateway.commit(token=permit("COMMIT", self.base, 2))
        reversed_result = harness.gateway.reverse_rollback(
            token=permit("REVERSE_ROLLBACK", self.applied, 3))
        self.assertIs(reversed_result.outcome, GatewayOutcome.ACKED)
        once = harness.assert_diagnostic_agrees(self)
        self.assertEqual(once.withdrawals_sent, 1)
        self.assertEqual(once.controls_reaching_ran, 2)  # the apply, then its restore
        replay = harness.gateway.reverse_rollback(
            token=permit("REVERSE_ROLLBACK", self.applied, 3))
        self.assertIs(replay.outcome, GatewayOutcome.ALREADY_APPLIED)
        self.assertEqual(harness.assert_diagnostic_agrees(self), once)
        self.assertEqual(harness.live_cap(), int(UNCAPPED))


# --------------------------------------------------------------------------- #
# 7. the primary landed and the supplementary did not
# --------------------------------------------------------------------------- #


class APartialCompositionUnwindsInReverseOrder(unittest.TestCase):
    """Fault 7 -- **HERMETIC REGRESSION**: steering succeeded, the cap failed.

    No committed 2026-09-04 run composed a supplementary cap alongside the
    steering action, so the two-participant partial apply has never been seen
    on the radio.  It is asserted here because the unwind order is what makes a
    half-live composition recoverable.

    The composition is half live, and that is a *positively observed* state:
    the gateway says ``PARTIAL_APPLY`` rather than a clean rejection, which
    would tell the Kernel nothing happened while the cell had already moved.
    The reversal then walks the participants backwards -- the supplementary
    change comes off before the primary one it supported -- and the harm the
    trial reserved is returned.
    """

    def setUp(self):
        self.base = config_hash(baseline_config())
        self.harness = build_cap_harness(
            faults=CapFaults(refuse_create=A1ValidationError))
        self.harness.gateway.prepare(
            token=permit("PREPARE", self.base, 0), plan=self.harness.plan())
        self.harness.gateway.ready(token=permit("READY", self.base, 1))
        self.result = self.harness.gateway.commit(
            token=permit("COMMIT", self.base, 2))

    def test_a_half_live_composition_is_a_partial_apply(self):
        self.assertIs(self.result.outcome, GatewayOutcome.PARTIAL_APPLY)
        record = self.harness.transaction_journal.read("tx-cap")
        self.assertEqual(record.applied_axes, ("servingCell",))
        self.assertEqual(record.participants, ("mock", CAP.adapter))
        # The cap never reached the scheduler: refused before acceptance, so
        # the adapter issued nothing and the producer recorded no control.
        evidence = self.harness.assert_diagnostic_agrees(self)
        self.assertEqual(evidence.writes_sent, 0)
        self.assertEqual(evidence.refused, 1)
        self.assertEqual(evidence.controls_reaching_ran, 0)
        self.assertEqual(self.harness.live_cap(), int(UNCAPPED))

    def test_the_reversal_restores_the_supplementary_axis_before_the_primary(self):
        observed = config_hash({"servingCell": "87654321", CAP.axis: UNCAPPED})
        reversed_result = self.harness.gateway.reverse_rollback(
            token=permit("REVERSE_ROLLBACK", observed, 3))
        self.assertIs(reversed_result.outcome, GatewayOutcome.ACKED)
        undone = [command["axis"] for command in self.harness.steering.commands
                  if command["operation"] == "UNDO"]
        # The mock carries only the primary axis, and it is undone last: the
        # supplementary participant is unwound first.
        self.assertEqual(undone, ["servingCell"])
        self.assertEqual(self.harness.steering.snapshot()["servingCell"],
                         str(HOME_NCI))

    def test_the_primary_write_is_never_silently_dismissed(self):
        # The one answer this must never give: "rejected", which would say the
        # cell did not move when it did.
        self.assertIsNot(self.result.outcome, GatewayOutcome.REJECTED)
        self.assertEqual(
            [command["axis"] for command in self.harness.steering.writes
             if command.get("operation") == "APPLY"],
            ["servingCell"])


if __name__ == "__main__":
    unittest.main()
