"""``project_correlation_trace`` - the pure projection, tested in isolation.

Final integration section 4.7 requires that, once an operator submits a
natural-language Intent, one correlation id makes eight items traceable: the
Intent and its revision, the three-stage LLM judgement and its final
verdict, the generated rApp/R1 policy identity, the A1-P lifecycle, the
selected UE and target cell, whether an E2 control write was attempted and
how many, the KPM readback and O1 assurance, and the coordinator's FSM
terminal state with its commit/rollback result.

This module tests the projection with plain dicts, the same fixture style
``gui.operator.sources.live``'s other projection functions already use
(``project_decision``, ``project_intent_row``).  It never touches a console,
a transport or a display; the real-console, button-driven proof that this
projection is actually wired into what an operator sees lives in
``tests/gui/test_gui_reachability.py``'s Live-path flow test, so a change
here alone cannot pass without that flow also passing.
"""

from __future__ import annotations

import unittest
import uuid

from gui.operator.sources.live import project_correlation_trace
from gui.operator.viewmodel.types import DecisionView, IntentRowView, StageView


def _stages():
    return (StageView("parse", "Parse / normalize", "DONE"),
            StageView("feasibility", "Feasibility", "DONE"),
            StageView("alternatives", "Alternatives", "SKIPPED"))


def _decision(**overrides):
    values = dict(
        episode_id="episode-1",
        intent_text="keep UE downlink throughput above 1 Mbps",
        llm_stages=_stages(), eq12_state="NotAdmitted",
        terminal_outcome="pending_not_admitted", rolled_back=False)
    values.update(overrides)
    return DecisionView(**values)


def _intent_row(**overrides):
    values = dict(intent_id="intent-1", revision="1", text="intent text")
    values.update(overrides)
    return IntentRowView(**values)


def _dispatched_policy(*, correlation_id):
    return {
        "policyId": "policy-1",
        "scope": {"ueId": {"guAmfUeNgapId": {"amfUeNgapId": 42}}},
        "steeringObjective": {"actionEnvelope": {"allowedCells": [
            {"plmnId": {"mcc": "001", "mnc": "01"}, "cId": {"ncI": 7}}]}},
        "trace": {"intentId": str(uuid.uuid4()), "intentRevision": 1,
                  "policyRevision": 1, "correlationId": correlation_id},
    }


class ADispatchedEpisode(unittest.TestCase):
    """A policy reached R1 and a full A1 status document came back."""

    def _trace(self, *, control, rollback, readback):
        correlation_id = str(uuid.uuid4())
        policy = _dispatched_policy(correlation_id=correlation_id)
        policy_status = {
            "enforceStatus": "ENFORCED",
            "aicStatus": {
                "policyState": "ACTIVE", "episodeState": "APPLIED_VERIFIED",
                "control": control, "readback": readback, "rollback": rollback,
            },
        }
        authoritative = {"profile_trial": {
            "policyId": "policy-1", "policy": policy,
            "policy_status": policy_status}}
        r1_outbound = {
            "policyId": "policy-1", "policyTypeId": "AIC_UECellSteering_1.0.0",
            "policyRevision": 1, "contractValid": True,
            "correlationId": correlation_id,
        }
        return correlation_id, project_correlation_trace(
            authoritative=authoritative,
            decision=_decision(eq12_state="Admitted",
                               terminal_outcome="commit_original"),
            intent_row=_intent_row(), intent_id="intent-1",
            r1_outbound=r1_outbound, policy_status=policy_status,
            policy_context={})

    def test_all_eight_items_are_traced_by_the_one_correlation_id(self):
        correlation_id, trace = self._trace(
            control={"result": "ACK", "writeMayHaveOccurred": True},
            rollback={"state": "NOT_REQUESTED"},
            readback={"result": "VERIFIED"})

        # The correlation id itself.
        self.assertEqual(trace.correlation_id, correlation_id)
        self.assertEqual(trace.correlation_id_status, "OK")

        # 1. original Intent + revision.
        self.assertTrue(trace.intent_id)
        self.assertEqual(trace.intent_status, "OK")

        # 2. three-stage LLM judgement + final verdict.
        self.assertEqual(len(trace.llm_stages), 3)
        self.assertEqual(trace.verdict_status, "OK")
        self.assertEqual(trace.verdict_detail, "Admitted")

        # 3. generated rApp/R1 policy identity.
        self.assertEqual(trace.policy_id, "policy-1")
        self.assertEqual(trace.policy_type_id, "AIC_UECellSteering_1.0.0")
        self.assertEqual(trace.policy_identity_status, "OK")

        # 4. A1-P policy lifecycle/status.
        self.assertEqual(trace.policy_state, "ACTIVE")
        self.assertEqual(trace.episode_state, "APPLIED_VERIFIED")
        self.assertEqual(trace.policy_lifecycle_status, "OK")

        # 5. selected UE + target cell - the dispatched object, not a guess.
        self.assertEqual(trace.target_source, "DISPATCHED")
        self.assertEqual(trace.ue_id, "amfUeNgapId=42")
        self.assertEqual(trace.target_cells, ("001-01/7",))
        self.assertEqual(trace.target_status, "OK")

        # 6. E2 control attempt + write count.
        self.assertIs(trace.e2_control_attempted, True)
        self.assertEqual(trace.e2_control_result, "ACK")
        self.assertEqual(trace.e2_write_count, 1)
        self.assertEqual(trace.e2_status, "OK")

        # 7. KPM effect readback + O1 assurance.
        self.assertEqual(trace.readback_result, "VERIFIED")

        # 8. FSM terminal state + commit/rollback.
        self.assertEqual(trace.eq12_state, "Admitted")
        self.assertEqual(trace.terminal_outcome, "commit_original")
        self.assertIs(trace.rolled_back, False)
        self.assertEqual(trace.fsm_status, "OK")

    def test_a_rollback_write_is_counted_beside_the_original_write(self):
        _correlation_id, trace = self._trace(
            control={"result": "NACK", "writeMayHaveOccurred": True},
            rollback={"state": "SENT"},
            readback={"result": "MISSING"})
        self.assertEqual(trace.e2_write_count, 2)
        self.assertIs(trace.e2_control_attempted, True)

    def test_a_control_write_that_never_reached_the_radio_counts_as_zero(self):
        _correlation_id, trace = self._trace(
            control={"result": "TIMEOUT", "writeMayHaveOccurred": False},
            rollback={"state": "NOT_REQUESTED"},
            readback={"result": "NOT_AVAILABLE"})
        self.assertEqual(trace.e2_write_count, 0)
        self.assertIs(trace.e2_control_attempted, True)
        self.assertEqual(trace.e2_control_result, "TIMEOUT")


class ANonDispatchedEpisode(unittest.TestCase):
    """Nothing reached R1 - the honest-Unknown path, item by item."""

    def test_no_policy_and_no_status_leaves_every_item_honestly_unknown(self):
        trace = project_correlation_trace(
            authoritative={}, decision=_decision(eq12_state=None,
                                                 terminal_outcome=None),
            intent_row=_intent_row(intent_id="unknown", revision=None),
            intent_id="minted-1", r1_outbound={}, policy_status={},
            policy_context={})

        # No contract correlationId exists yet; the console's own minted id
        # is shown instead, and says so.
        self.assertEqual(trace.correlation_id, "minted-1")
        self.assertEqual(trace.correlation_id_status, "UNKNOWN")
        self.assertTrue(trace.correlation_id_reason)

        self.assertEqual(trace.intent_status, "UNKNOWN")
        self.assertTrue(trace.intent_reason)

        self.assertEqual(trace.verdict_status, "UNKNOWN")
        self.assertTrue(trace.verdict_reason)

        self.assertIsNone(trace.policy_id)
        self.assertEqual(trace.policy_identity_status, "UNKNOWN")
        self.assertTrue(trace.policy_identity_reason)

        self.assertEqual(trace.policy_lifecycle_status, "UNKNOWN")
        self.assertTrue(trace.policy_lifecycle_reason)

        self.assertEqual(trace.target_status, "UNKNOWN")
        self.assertTrue(trace.target_reason)
        self.assertIsNone(trace.ue_id)
        self.assertEqual(trace.target_cells, ())

        # No A1 status document at all was observed: the count must stay
        # Unknown, never a synthesised zero.
        self.assertIsNone(trace.e2_control_attempted)
        self.assertIsNone(trace.e2_write_count)
        self.assertEqual(trace.e2_status, "UNKNOWN")
        self.assertTrue(trace.e2_reason)

        self.assertEqual(trace.assurance_status, "UNKNOWN")
        self.assertTrue(trace.assurance_reason)

        self.assertEqual(trace.fsm_status, "UNKNOWN")
        self.assertTrue(trace.fsm_reason)

    def test_a_status_document_with_no_control_block_is_a_real_zero(self):
        """Observed-and-empty is not the same fact as never-observed."""
        policy_status = {"enforceStatus": "NOT_ENFORCED",
                         "aicStatus": {"policyState": "NOT_ENFORCED",
                                       "episodeState": "ABORTED_NO_WRITE"}}
        trace = project_correlation_trace(
            authoritative={}, decision=_decision(), intent_row=_intent_row(),
            intent_id="intent-1", r1_outbound={}, policy_status=policy_status,
            policy_context={})
        self.assertIs(trace.e2_control_attempted, False)
        self.assertEqual(trace.e2_write_count, 0)
        self.assertEqual(trace.e2_status, "OK")
        self.assertIsNone(trace.e2_reason)

    def test_the_declared_scope_is_shown_when_nothing_was_dispatched(self):
        """A pre-submission declaration, honestly labelled as unconfirmed."""
        policy_context = {
            "ueId": {"guAmfUeNgapId": {"amfUeNgapId": 7}},
            "allowedCells": [{"plmnId": {"mcc": "001", "mnc": "01"},
                              "cId": {"ncI": 3}}],
        }
        trace = project_correlation_trace(
            authoritative={}, decision=_decision(), intent_row=_intent_row(),
            intent_id="intent-1", r1_outbound={}, policy_status={},
            policy_context=policy_context)
        self.assertEqual(trace.target_source, "DECLARED_ONLY")
        self.assertEqual(trace.ue_id, "amfUeNgapId=7")
        self.assertEqual(trace.target_cells, ("001-01/3",))
        self.assertEqual(trace.target_status, "UNKNOWN")
        self.assertIn("not a confirmed target", trace.target_reason)


if __name__ == "__main__":
    unittest.main()
