"""v5.3 (AIC_V53=1 over AIC_V52=1): the runbook's role texts, the energy arithmetic computed by code
(energyStepValues, each candidate's resulting cells, attenuation and energy steps), targets sorted by
the one p, and the Target rule checked without padding."""
import json
import os
import tempfile
import unittest
from unittest import mock

from assurance.coordination import v52
from tests.assurance.test_v52_view import C0, REF, REQS

CAT = [{"functionId": "cell-tx-attenuation", "scopes": ["cell@12345678"],
        "policyFields": {"txAttenuationDb": {"unit": "dB", "min": "8.0", "max": "20.0", "step": 0.5}}}]


class V53View(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(REF, self.tmp); self.tmp.close()
        self.env = mock.patch.dict(os.environ, {"AIC_V52": "1", "AIC_V53": "1", "AIC_V52_REFERENCE": self.tmp.name,
                                                "AIC_V51_ENERGY_CELL": "gnb1", "AIC_ENERGY_STEPS": "4"})
        self.env.start()

    def tearDown(self):
        self.env.stop(); os.unlink(self.tmp.name)

    def test_energy_steps_skip_the_half_step_that_earns_no_level(self):
        out = v52.view("control", {"input.authorization": {"requirements": REQS},
                                   "input.network_state": {"appliedConfiguration": C0}, "input.function_catalog": CAT})
        steps = out["input.energyStepValues"]
        self.assertEqual([9.0, 9.5, 10.0, 10.5], [v["txAttenuationDb"] for v in steps["values"]])
        self.assertEqual([1, 2, 3, 4], [v["energyImprovementSteps"] for v in steps["values"]])
        self.assertEqual(20, steps["c0EnergyLevel"])
        self.assertIn("unavailable", v52.view("basic-monolith", {
            "input.authorization": {"requirements": REQS}, "input.network_state": {"appliedConfiguration": C0},
            "input.function_catalog": []})["input.energyStepValues"])

    def test_two_energy_steps_unless_four_are_asked_for(self):
        """2026-09-30: the four-step offer is switched on per block (AIC_ENERGY_STEPS=4)."""
        with mock.patch.dict(os.environ, {"AIC_ENERGY_STEPS": ""}):
            steps = v52.energy_step_values(REQS, CAT, C0)
        self.assertEqual([9.0, 9.5], [v["txAttenuationDb"] for v in steps["values"]])

    def test_selection_sees_resulting_cells_attenuation_and_targets_by_p(self):
        payload = {"input.network_state": {"appliedConfiguration": C0},
                   "input.target_contract": {"authorization": REQS, "t0": {"targetId": "T0", "levels": {}},
                                             "alternatives": [
                                                 {"targetId": "T1", "levels": {"I4e.r1": 20}},
                                                 {"targetId": "T2", "levels": {"I1g.r1": 1}}],
                                             "provenance": {"modelSelection": {"order": [
                                                 {"targetId": "T1", "reason": "max energy"}]}}},
                   "input.control_candidates": [{"controlId": "C1", "functions": [
                       {"functionId": "steer", "scope": "ue@ue1", "policy": {"servingCell": "12345678"}},
                       {"functionId": "cell-tx-attenuation", "scope": "cell@12345678", "policy": {"txAttenuationDb": "9.5"}}]}],
                   "input.observations": [{"trialIndex": 0, "configuration": C0, "kpis": {}}]}
        out = v52.view("trajectory", payload)
        self.assertEqual(["T0", "T2", "T1"], [t["targetId"] for t in out["input.target_contract"]["targets"]])
        self.assertEqual("max energy", out["input.target_contract"]["targets"][2]["reason"])
        self.assertNotIn("tradeoff", out["input.target_contract"]["targets"][1])
        c1 = out["input.control_candidates"][0]
        self.assertEqual({"ue1": "gnb1", "ue2": "gnb1"}, c1["resultingCells"])
        self.assertEqual(9.5, c1["gnb1AttenuationDb"])
        self.assertEqual(2, c1["energyImprovementSteps"])
        self.assertEqual(0, out["input.observations"][0]["energyImprovementSteps"])

    def test_target_rule(self):
        sup = v52.supported_target(REQS, v52.reference_measurement({}))["levels"]     # E20 UE1 0 I2d 10 UE3 10
        others = [{"I4e.r1": 20 - k, "I1g.r1": 1} for k in range(7)]
        self.assertIsNone(v52.check_targets([sup] + others, REQS))
        self.assertIn("exactly once", v52.check_targets(others + [{"I1g.r1": 9}], REQS))
        self.assertIn("exactly eight", v52.check_targets(others, REQS))
        self.assertIn("distinct", v52.check_targets([sup, sup] + others[:6], REQS))
        with mock.patch.dict(os.environ, {"AIC_V52_REFERENCE": ""}):
            self.assertIn("maximum-concession", v52.check_targets([sup] + others, REQS))
            mx = {k: 20 for k in v52.KEYS}
            self.assertIsNone(v52.check_targets([mx] + others, REQS))

    def test_raw_answer_is_checked_before_the_validator_trims_it(self):
        rows = [{"levels": {"I4e.r1": k}} for k in range(1, 9)]
        self.assertIsNone(v52.check_raw_targets(rows))
        self.assertIn("exactly eight", v52.check_raw_targets(rows + [rows[0]]))
        self.assertIn("not an object", v52.check_raw_targets(rows[:7] + [None]))
        self.assertIn("not an integer", v52.check_raw_targets(rows[:7] + [{"levels": {"I4e.r1": 2.5}}]))
        self.assertIn("not an authorized", v52.check_raw_targets(rows[:7] + [{"levels": {"x.r1": 5}}], set(REQS)))
        self.assertIn("protected", v52.check_raw_targets(rows[:7] + [{"levels": {"I2g.r1": 1}}], set(REQS)))

    def test_system_texts(self):
        self.assertTrue(v52.system("target").startswith(v52.V53_COMMON))
        self.assertIn("Choose one applicable, untried control", v52.system("monolith-select"))
        self.assertIn(v52.V53_IM_OUTPUT, v52.system("monolith-form"))
        with mock.patch.dict(os.environ, {"AIC_V53": ""}):
            self.assertEqual(v52.SYSTEM["target"], v52.system("target"))


if __name__ == "__main__":
    unittest.main()
