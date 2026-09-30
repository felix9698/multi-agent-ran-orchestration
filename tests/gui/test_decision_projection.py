import unittest

from gui.operator.sources.live import project_calibration, project_decision, project_intent_row
from gui.operator.status import terminal_state_label
from gui.operator.widgets.intent_table import LIFECYCLES


class DecisionProjectionTest(unittest.TestCase):
    def _result(self, outcome="commit_original"):
        return {
            "episode_id": "ep-7",
            "success": outcome in {"commit_original", "commit_revised"},
            "terminal_outcome": outcome,
            "terminal_reason": "verified",
            "agreement": {"accepted": True},
            "cycles": [{
                "feasible": True,
                "raw_confidence": 0.61,
                "calibrated_probability": 0.82,
                "threshold": 0.75,
                "threshold_applied_to": "calibrated_probability",
                "calibration_reason": "isotonic",
                "routed_to": "trial",
                "rolled_back": False,
                "alternatives": [{"id": "a-1", "description": "lower target",
                                   "confidence": 0.71, "accepted": True}],
                "profile_trial": {
                    "policyId": "policy-3",
                    "assurance_decision": "SATISFIED",
                    "policy": {"trace": {"intentId": "intent-2",
                                           "intentRevision": "4",
                                           "policyRevision": "6"}},
                    "policy_status": {
                        "enforceStatus": "ENFORCED",
                        "aicStatus": {"policyState": "ACTIVE",
                                      "episodeState": "APPLIED_VERIFIED"},
                    },
                },
            }],
        }

    def test_calibrated_route_and_terminal_semantics_are_explicit(self):
        decision = project_decision(self._result(), intent_text="throughput target")
        self.assertEqual(decision.raw_confidence, 0.61)
        self.assertEqual(decision.calibrated_probability, 0.82)
        self.assertEqual(decision.theta_star, 0.75)
        self.assertEqual(decision.threshold_applied_to, "calibrated_probability")
        self.assertTrue(decision.agreement)
        self.assertTrue(decision.success)
        self.assertEqual(decision.terminal_outcome, "commit_original")
        self.assertEqual(decision.eq12_state, "Admitted")

    def test_four_internal_outcomes_map_to_three_eq12_states(self):
        expected = {
            "commit_original": "Admitted", "commit_revised": "Admitted",
            "pending_not_admitted": "NotAdmitted",
            "technical_failsafe": "TechnicalFailsafe",
        }
        self.assertEqual({key: terminal_state_label(key) for key in expected}, expected)
        revised = project_decision(self._result("commit_revised"))
        original = project_decision(self._result("commit_original"))
        self.assertEqual(revised.eq12_state, original.eq12_state)
        self.assertNotEqual(revised.terminal_outcome, original.terminal_outcome)

    def test_intent_policy_and_evidence_are_three_fields(self):
        row = project_intent_row(self._result(), intent_text="throughput target")
        self.assertEqual(row.eq12_state, "Admitted")
        self.assertEqual(row.policy_status, "OK")
        self.assertEqual(row.evidence_status, "OK")
        self.assertEqual(row.policy_id, "policy-3")
        self.assertEqual(row.policy_version, "1.0.0")
        unknown_evidence = self._result()
        unknown_evidence["cycles"][0]["profile_trial"].pop("assurance_decision")
        row = project_intent_row(unknown_evidence, intent_text="throughput target")
        self.assertEqual(row.eq12_state, "Admitted")
        self.assertEqual(row.policy_status, "OK")
        self.assertEqual(row.evidence_status, "UNKNOWN")

    def test_policy_axis_uses_worst_status_across_all_three_contract_fields(self):
        cases = (
            ({"enforceStatus": "NOT_ENFORCED",
              "aicStatus": {"policyState": "ERROR", "episodeState": "NO_ACTION"}},
             "ERROR", "ERROR"),
            ({"enforceStatus": "ENFORCED",
              "aicStatus": {"policyState": "ACTIVE", "episodeState": "APPLY_FAILED"}},
             "ERROR", "APPLY_FAILED"),
        )
        for policy_status, expected_status, expected_detail in cases:
            with self.subTest(policy_status=policy_status):
                row = project_intent_row({"policy_status": policy_status})
                self.assertEqual(row.policy_status, expected_status)
                self.assertEqual(row.policy_status_detail, expected_detail)

    def test_gap03_conflict_is_unknown_not_inferred_from_fsm(self):
        result = self._result()
        result["transitions"] = [{"from": "S0", "to": "S1"},
                                 {"from": "S1", "to": "S2"}]
        self.assertIsNone(project_decision(result).has_conflict)

    def test_failsafe_is_a_distinct_sink_not_a_normal_s6(self):
        result = self._result("technical_failsafe")
        decision = project_decision(result)
        states = {stage.stage_id: stage.state for stage in decision.fsm_stages}
        self.assertEqual(states["S_TECHNICAL_FAILSAFE"], "FAILED")
        self.assertNotEqual(states["S6"], "DONE")
        row = project_intent_row(result)
        self.assertEqual(row.intent_state, "S_TECHNICAL_FAILSAFE")
        self.assertEqual(row.lifecycle, "FAILSAFE")
        self.assertIn("FAILSAFE", LIFECYCLES)

    def test_terminal_pipeline_marks_unexecuted_llm_stage_skipped(self):
        decision = project_decision({
            "episode_id": "ep-skip", "terminal_outcome": "commit_original",
            "success": True, "cycles": [{"raw_confidence": 0.9}],
        })
        states = {stage.stage_id: stage.state for stage in decision.llm_stages}
        self.assertEqual(states["parse"], "DONE")
        self.assertEqual(states["feasibility"], "DONE")
        self.assertEqual(states["alternatives"], "SKIPPED")

    def test_joint_evaluation_conflict_ids_are_consumed_without_inventing_s1_verdict(self):
        decision = project_decision({
            "episode_id": "ep-joint", "terminal_outcome": "pending_not_admitted",
            "joint_evaluation": {"conflict_ids": ["intent-1", "intent-2"]},
        })
        self.assertEqual(decision.conflict_intent_ids, ("intent-1", "intent-2"))
        self.assertIsNone(decision.has_conflict)

    def test_calibration_global_and_partition_counts_stay_separate(self):
        view = project_calibration({
            "mode": "global", "theta_star": 0.7, "theta_star_raw": 0.8,
            "n_max": 3, "counters_scope": "global_aggregate_cross_partition",
            "total_episodes": 10, "total_negotiations": 4, "total_rollbacks": 2,
            "active_partition_counters": {"episodes": 3, "negotiations": 1},
            "operating_context": {"model_id": "m", "regime_id": "r"},
            "calibration_partitions": [{"model_id": "m", "regime_id": "r",
                                        "ece": 0.1, "brier": 0.2,
                                        "sample_count": 3,
                                        "sufficient_samples": False}],
        })
        self.assertEqual(view.total_episodes, 10)
        self.assertEqual(view.partition_counters["episodes"], 3)
        self.assertEqual(view.ece, 0.1)


if __name__ == "__main__":
    unittest.main()
