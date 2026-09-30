"""v4.7 without a ladder (owner 2026-09-25): 21 levels per coordinate is too large to expand, so
T0 + the all-at-limit target + the Target agent's own rows are built directly."""
import time
import unittest

from assurance.coordination import agents, tc
from assurance.coordination.tc import Authorization, Intent, Preference, TargetContract


def _auth(steps=20):
    rows = [("I0c", "operator", "cellGoodputMbps", 17.6, 10.3, "cell@1"),
            ("I1g", "ue1", "goodputMbps", 9.6, 7.7, "ue@1"),
            ("I2g", "ue2", "goodputMbps", 10.3, None, "ue@2"),
            ("I3g", "ue3", "goodputMbps", 7.7, 5.1, "ue@3"),
            ("I3x", "ue3", "rsrpDbm", -80.0, -90.0, "ue@3"),
            ("I1x", "ue1", "rsrpDbm", -80.0, -90.0, "ue@1")]
    intents = [Intent.from_record({"intentId": i, "owner": o, "priority": n + 1, "requirement": {
        "reqId": f"{i}.r1", "kpi": k, "op": ">=", "value": v, "unit": "x", "scope": s,
        **({"bound": b, "steps": steps} if b is not None else {"steps": 0})}})
        for n, (i, o, k, v, b, s) in enumerate(rows)]
    return Authorization.from_intents(intents, preference=Preference.from_intents(intents))


class TargetWithoutLadder(unittest.TestCase):
    def setUp(self):
        self.a = _auth()
        self.t0 = {"requirements": {k: e.original for k, e in self.a.requirements.items()}}

    def _row(self, q):
        return {"levels": {k: (q if e.relaxable else 0) for k, e in self.a.requirements.items()}}

    def test_grid_too_large_to_expand(self):
        with self.assertRaises(tc.TargetValidationError):
            tc.expand_targets(self.a)   # 21**5 > MAX_EXPANSION

    def test_mandatory_is_t0_and_all_at_limit(self):
        started = time.monotonic()
        rows = agents._mandatory_rows(self.a)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual([r["levels"]["I1g.r1"] for r in rows], [0, 20])
        self.assertEqual(rows[1]["levels"]["I2g.r1"], 0)   # protected stays

    def test_selected_levels_become_targets(self):
        contract = TargetContract.from_compact(self.t0, {}, [], {}, self.a,
                                               [self._row(3), self._row(11)])
        self.assertEqual(len(contract.targets), 4)
        value = {t.levels["I1g.r1"]: t.requirements["I1g.r1"] for t in contract.targets}
        self.assertAlmostEqual(value[11], 9.6 - 11 / 20 * (9.6 - 7.7), places=6)

    def test_past_the_limit_is_refused(self):
        with self.assertRaises(tc.TargetValidationError):
            TargetContract.from_compact(self.t0, {}, [], {}, self.a, [self._row(21)])

    def test_small_grid_still_expands(self):
        a = _auth(steps=2)
        self.assertEqual(len(tc.omega_or_sparse(a).targets), 3 ** 5)


if __name__ == "__main__":
    unittest.main()
