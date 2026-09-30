"""v5.1 (owner 2026-09-26): ROC-weighted concession sum over the priority order."""
import os
import unittest
from unittest import mock

from assurance.coordination import concession as c

REQS = (c.Level("E", "E", "e", 18.0, 6.0), c.Level("U1", "U1", "u1", 10.0, 6.0),
        c.Deadline("U2d", "U2d", "d", 0.9, 0.6, 500.0, 500.0), c.Level("U3", "U3", "u3", 10.0, 4.0),
        c.Level("U2g", "U2g", "g", 4.0, None))
ORDER = ("E", "U1", "U2d", "U3", "U2g")


def obs(e, u1, d, u3, g=5.0):
    return {"e": e, "u1": u1, "d": {"byDeadlineMs": {"500.0": d}}, "u3": u3, "g": g}


class Weighted(unittest.TestCase):
    def test_roc(self):
        self.assertEqual((25, 13, 7, 3), c.roc_weights(4))

    def test_the_two_examples(self):
        with mock.patch.dict(os.environ, {"AIC_V51": "1"}):
            only_e = c.evaluate(REQS, ORDER, obs(18.0, 6.0, 0.6, 4.0))
            self.assertEqual(13 * 20 + 7 * 20 + 3 * 20, only_e.p)
            self.assertAlmostEqual(0.521, 1 - only_e.p / 960, places=3)
            one_step_e = c.evaluate(REQS, ORDER, obs(17.4, 10.0, 0.9, 10.0))
            self.assertEqual(25, one_step_e.p)
            self.assertAlmostEqual(0.974, 1 - one_step_e.p / 960, places=3)
            self.assertFalse(c.evaluate(REQS, ORDER, obs(18.0, 10.0, 0.9, 10.0, g=3.0)).attained)

    def test_the_record_decides_not_the_environment(self):
        with mock.patch.dict(os.environ, {"AIC_V51": ""}):
            self.assertEqual(25, c.evaluate(REQS, ORDER, obs(17.4, 10.0, 0.9, 10.0), True).p)
            trial = type("T", (), {"kpis": obs(17.4, 10.0, 0.9, 10.0), "trial_index": 1})()
            out = c.evaluate_trials((), ORDER, [], weighted=True)
            self.assertEqual(0, out["pScale"])                      # no adjustable requirement
            self.assertTrue(c.weighted_rule(type("P", (), {"rule": "v5.1: weighted ..."})()))
            self.assertFalse(c.weighted_rule(type("P", (), {"rule": "v5: coordinate-lexicographic"})()))

    def test_off_keeps_lexicographic(self):
        with mock.patch.dict(os.environ, {"AIC_V51": ""}):
            v = c.evaluate(REQS, ORDER, obs(17.4, 10.0, 0.9, 10.0))
            self.assertEqual(21 ** 3, v.p)


class PreferenceFollowsTheSameRule(unittest.TestCase):
    def test_rule_and_target_ranking(self):
        from tests.test_v5_design import _intents
        from assurance.coordination import tc
        with mock.patch.dict(os.environ, {"AIC_DESIGN": "v5", "AIC_V51": "1"}):
            intents = _intents()
            pref = tc.Preference.from_intents(intents)
            self.assertTrue(pref.rule.startswith("v5.1:"), pref.rule)
            self.assertIn("25 k[I4e.r1] + 13 k[I1g.r1] + 7 k[I2d.r1] + 3 k[I3g.r1]", pref.rule)
            a = tc.Authorization.from_intents(intents, preference=pref)
            t0 = {"requirements": {k: e.original for k, e in a.requirements.items()}}
            rows = [{"levels": {"I4e.r1": 1, "I1g.r1": 0, "I2d.r1": 0, "I3g.r1": 0, "I2g.r1": 0}},
                    {"levels": {"I4e.r1": 0, "I1g.r1": 1, "I2d.r1": 1, "I3g.r1": 1, "I2g.r1": 0}}]
            contract = tc.TargetContract.from_compact(t0, {}, [], {}, a, rows)
            keys = sorted(tc.preference_key(t, a) for t in contract.alternatives)
            self.assertEqual([(23.0,), (25.0,)], keys)      # 13+7+3 beats one E step
            replay = tc.Preference.from_record(pref.to_record())
            self.assertEqual(pref.rule, replay.rule)


if __name__ == "__main__":
    unittest.main()
