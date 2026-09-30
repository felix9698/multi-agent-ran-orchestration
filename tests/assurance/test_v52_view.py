"""v5.2 (AIC_V52=1): the v5.2c system texts and the model's view of the inputs -- a block reference
and its measurement-supported target, no duplicated intents, a catalog without UE1-specific ids
or baselines, targets scored by the one p, candidates as changes from C0."""
import json
import os
import tempfile
import unittest
from unittest import mock

from assurance.coordination import v52

REQS = {
    "I4e.r1": {"kpi": "cellTxAttenuationDb", "scope": "cell@12345678", "op": ">=", "original": 20.0,
               "limit": 8.0, "steps": 20, "levels": [20.0 - 0.6 * k for k in range(21)], "weight": 1.0},
    "I1g.r1": {"kpi": "dlGoodputMbps", "scope": "ue@ue1", "op": ">=", "original": 9.36, "limit": 5.91,
               "steps": 20, "levels": [9.36 - 0.1725 * k for k in range(21)]},
    "I2d.r1": {"kpi": "deadlineSuccessRatio", "scope": "ue@ue2", "op": ">=", "original": 0.9, "limit": 0.6,
               "steps": 20, "deadlineMs": 33.5, "levels": [0.9 - 0.015 * k for k in range(21)]},
    "I3g.r1": {"kpi": "dlGoodputMbps", "scope": "ue@ue3", "op": ">=", "original": 9.39, "limit": 3.95,
               "steps": 20, "levels": [9.39 - 0.272 * k for k in range(21)]},
    "I2g.r1": {"kpi": "dlGoodputMbps", "scope": "ue@ue2", "op": ">=", "original": 3.95},
}
C0 = {"servingCell@ue1": "87654321", "servingCell@ue2": "12345678", "pfWeight@ue2": "1.0",
      "txAttenuationDb@12345678": "8.0"}
REF = {"at": "t", "reference": {"ue1": 9.9}, "energyBaselineDb": 8.0, "ue2DeadlineMs": 33.5,
       "ue2SuccessAtDInA": 0.752, "raw": {"A": {"ue1": [9.887] * 5, "ue2": [9.886] * 5, "ue3": [6.706] * 5}}}


class V52View(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(REF, self.tmp); self.tmp.close()
        self.env = mock.patch.dict(os.environ, {"AIC_V52": "1", "AIC_V52_REFERENCE": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop(); os.unlink(self.tmp.name)

    def test_formation_gets_the_reference_and_its_supported_target_and_no_duplicate_intents(self):
        payload = {"input.intents": [{"intentId": "I4e"}], "input.authorization": {"requirements": REQS, "preference": {}},
                   "input.network_state": {"appliedConfiguration": C0},
                   "input.construction_policy": {"retain": 11, "mustIncludeBaseline": True},
                   "input.function_catalog": [{"functionId": "steer", "actionId": "steer@ue1",
                                               "policyFields": {"servingCell": {"values": ["1", "2"], "baseline": "87654321"}}}]}
        out = v52.view("control", payload)
        self.assertNotIn("input.intents", out)
        self.assertEqual({"I4e.r1": 20, "I1g.r1": 0, "I2d.r1": 10, "I3g.r1": 10, "I2g.r1": 0},
                         out["input.measurementSupportedTarget"]["levels"])
        self.assertEqual(25 * 20 + 7 * 10 + 3 * 10, out["input.measurementSupportedTarget"]["p"])
        cat = out["input.function_catalog"][0]
        self.assertNotIn("actionId", cat)
        self.assertNotIn("baseline", cat["policyFields"]["servingCell"])
        self.assertEqual({"additionalCandidates": 10, "note": "code adds C0; return ten others"},
                         out["input.construction_policy"])
        self.assertNotIn("levels", out["input.authorization"]["requirements"]["I4e.r1"])
        self.assertTrue(out["input.authorization"]["requirements"]["I2g.r1"]["protected"])
        self.assertEqual(33.5, out["input.authorization"]["requirements"]["I2d.r1"]["deadlineMs"])

    def test_selection_scores_targets_by_p_and_shows_candidates_as_changes(self):
        payload = {"input.network_state": {"appliedConfiguration": C0},
                   "input.target_contract": {"authorization": REQS, "t0": {"targetId": "T0", "levels": {k: 0 for k in REQS}},
                                             "alternatives": [{"targetId": "T1", "levels": {"I4e.r1": 17, "I1g.r1": 4, "I2d.r1": 10, "I3g.r1": 10}, "cost": 441.0}]},
                   "input.control_candidates": [{"controlId": "C1", "functions": [
                       {"functionId": "cell-tx-attenuation", "scope": "cell@12345678", "policy": {"txAttenuationDb": "9.0"}},
                       {"functionId": "ue-sched-priority", "scope": "ue@ue2", "policy": {"pfWeight": "1.0"}}]}],
                   "input.observations": [{"trialIndex": 0, "configuration": C0, "kpis": {}, "verdicts": {"T0": "FAIL"}}]}
        out = v52.view("trajectory", payload)
        t1 = out["input.target_contract"]["targets"][1]
        self.assertEqual(25 * 17 + 13 * 4 + 7 * 10 + 3 * 10, t1["p"])
        self.assertEqual({"vs": "measurementSupportedTarget", "tightened": ["I4e.r1"], "relaxed": ["I1g.r1"]},
                         t1["tradeoff"])
        self.assertEqual({"txAttenuationDb@12345678": "9.0"}, out["input.control_candidates"][0]["changesFromC0"])
        self.assertNotIn("verdicts", out["input.observations"][0])

    def test_system_texts_and_kinds(self):
        self.assertEqual("basic-monolith", v52.kind("monolith", "selection", {"input.tried_configurations": []}))
        self.assertEqual("monolith-select", v52.kind("monolith", "selection", {}))
        self.assertEqual("monolith-form", v52.kind("monolith", "formation", {}))
        for k in ("control", "monolith-form", "basic-monolith"):
            self.assertIn(v52.CONTENTION, v52.SYSTEM[k])
        self.assertIn("Return exactly eight distinct alternatives", v52.SYSTEM["target"])

    def test_no_reference_gives_null_measurement_and_target(self):
        with mock.patch.dict(os.environ, {"AIC_V52_REFERENCE": ""}):
            out = v52.view("target", {"input.authorization": {"requirements": REQS},
                                      "input.network_state": {"appliedConfiguration": C0}})
        self.assertIsNone(out["input.initial_measurement"])
        self.assertIsNone(out["input.measurementSupportedTarget"])


if __name__ == "__main__":
    unittest.main()
