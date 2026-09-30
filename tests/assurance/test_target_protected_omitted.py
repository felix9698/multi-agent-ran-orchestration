"""v54t (2026-09-30): local qwen3 left the protected requirement (I2g.r1, level 0 in every authorized
target) out of its Target rows; every row missed the lookup and was refused as outside the owner's targets."""
import json
import os
import unittest
from unittest import mock

from assurance.coordination import tc, agents

AUTH = json.loads('{"requirements": {"I4e.r1": {"kpi": "cellTxAttenuationDb", "scope": "cell@12345678", "unit": "dB", "op": ">=", "original": 20.0, "limit": 8.0, "steps": 20, "owner": "operator", "protected": false}, "I1g.r1": {"kpi": "dlGoodputMbps", "scope": "ue@ue1", "unit": "Mbps", "op": ">=", "original": 5.63, "limit": 3.55, "steps": 20, "owner": "operator", "protected": false}, "I2d.r1": {"kpi": "deadlineSuccessRatio", "scope": "ue@ue2", "unit": "ratio", "op": ">=", "original": 0.9, "limit": 0.6, "steps": 20, "owner": "operator", "deadlineMs": 100.0, "deadlineSteps": 0, "deadlineBound": 100.0, "protected": false}, "I3g.r1": {"kpi": "dlGoodputMbps", "scope": "ue@ue3", "unit": "Mbps", "op": ">=", "original": 5.63, "limit": 2.37, "steps": 20, "owner": "operator", "protected": false}, "I2g.r1": {"kpi": "dlGoodputMbps", "scope": "ue@ue2", "unit": "Mbps", "op": ">=", "original": 0.93, "limit": 0.93, "steps": 0, "owner": "operator", "protected": true}}, "jointConditions": [], "modeConstraints": []}')


def row(levels):
    return {"levels": levels, "deadlineLevels": {"I2d.r1": 0}, "reason": "x", "evidenceRefs": []}


class AnOmittedProtectedRequirementIsLevelZero(unittest.TestCase):
    def test_rows_without_the_protected_requirement_are_found(self):
        auth = tc.Authorization.from_record(AUTH)
        rows = [row({"I4e.r1": e, "I1g.r1": a, "I2d.r1": b, "I3g.r1": c})
                for e, a, b, c in ((20, 0, 0, 0), (0, 20, 0, 0), (0, 0, 20, 0), (0, 0, 0, 20),
                                   (10, 10, 0, 0), (10, 0, 10, 0))]
        with mock.patch.dict(os.environ, {"AIC_V52": "", "AIC_V53": ""}):
            contract, _notes = agents._validated_contract({"alternatives": rows}, auth)
        self.assertTrue(any(t.levels.get("I4e.r1") == 20 and t.levels.get("I2g.r1", 0) == 0
                            for t in contract.alternatives))

    def test_a_missing_relaxable_requirement_is_still_refused(self):
        auth = tc.Authorization.from_record(AUTH)
        with self.assertRaises(Exception):
            agents._validated_contract({"alternatives": [row({"I4e.r1": 20})] * 8}, auth)


if __name__ == "__main__":
    unittest.main()
