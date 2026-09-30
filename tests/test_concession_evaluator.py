import unittest

from assurance.coordination.concession import Deadline, Level, evaluate, in_omega, supports

OWNERS = ("operator", "ue1", "ue2", "ue3")
REQS = (
    Level("I0c", "operator", "cell@gnb1", 14.0, 12.0),
    Level("I1g", "ue1", "g@ue1", 7.5, 6.0),
    Level("I2g", "ue2", "g@ue2", 8.0, None),                      # protected
    Level("I3g", "ue3", "g@ue3", 4.0, 3.0),
    Deadline("I3d", "ue3", "d@ue3", 0.6, 0.4, 3000, 4000),
)


def obs(**kw):
    base = {"cell@gnb1": 15.0, "g@ue1": 8.0, "g@ue2": 9.0, "g@ue3": 5.0,
            "d@ue3": {3000: 0.7, 3500: 0.8, 4000: 0.9}}
    base.update(kw)
    return base


class Evaluator(unittest.TestCase):
    def test_t0(self):
        v = evaluate(REQS, OWNERS, obs())
        self.assertTrue(v.attained and v.t0)
        self.assertEqual(0, v.p)

    def test_small_concession_is_not_zero(self):
        v = evaluate(REQS, OWNERS, obs(**{"g@ue1": 7.49}))
        self.assertEqual((0, 1, 0, 0), v.owner_bins)       # 0.0067 of span rounds up
        v = evaluate(REQS, OWNERS, obs(**{"g@ue1": 7.1}))   # 0.267 -> 6 bins
        self.assertEqual((0, 6, 0, 0), v.owner_bins)

    def test_below_limit_or_protected_is_not_attained(self):
        self.assertFalse(evaluate(REQS, OWNERS, obs(**{"g@ue1": 5.9})).attained)
        self.assertFalse(evaluate(REQS, OWNERS, obs(**{"g@ue2": 7.99})).attained)

    def test_deadline_pair_takes_fewest_bins(self):
        # ratio 0.55 at 3000 (5 bins) vs 0.6 at 3500 (10 deadline bins): 5 wins
        v = evaluate(REQS, OWNERS, obs(**{"d@ue3": {3000: 0.55, 3500: 0.6}}))
        self.assertIn(("I3d.ratio", 5), v.coordinates)
        self.assertIn(("I3d.deadline", 0), v.coordinates)

    def test_lexicographic(self):
        a = evaluate(REQS, OWNERS, obs(**{"g@ue1": 7.35, "g@ue3": 3.0}))   # ue1 2 bins, ue3 20
        b = evaluate(REQS, OWNERS, obs(**{"g@ue1": 7.05}))                 # ue1 6 bins
        self.assertLess(a.p, b.p, "a higher owner's smaller concession wins")
        c = evaluate(REQS, OWNERS, obs(**{"cell@gnb1": 13.9}))             # operator 1 bin
        self.assertGreater(c.p, a.p)
        self.assertGreater(c.p, b.p)

    def test_target_support_and_authorization(self):
        t = {"I0c": 13.0, "I1g": 7.2, "I2g": 8.0, "I3g": 3.5, "I3d": (0.5, 3500)}
        self.assertTrue(in_omega(t, REQS))
        self.assertTrue(supports(t, REQS, obs()))
        self.assertFalse(supports(t, REQS, obs(**{"g@ue1": 7.1})))
        self.assertFalse(in_omega(dict(t, I2g=7.0), REQS), "protected level cannot move")
        self.assertFalse(in_omega(dict(t, I1g=5.0), REQS))


if __name__ == "__main__":
    unittest.main()


class GenericRankScales(unittest.TestCase):
    """v4.7: a workload the frozen table does not match still gets a verified rank."""

    def test_rank_agrees_with_the_lexicographic_tuple(self):
        from fractions import Fraction
        from itertools import product
        from assurance.coordination.preference import p_rank, p1_vector
        auth = {"I0c.r1": {"owner": "op", "steps": 5}, "I1g.r1": {"owner": "u1", "steps": 5},
                "I3g.r1": {"owner": "u3", "steps": 5},
                "I3d.r1": {"owner": "u3", "steps": 5, "deadlineSteps": 5}}
        order = ("op", "u1", "u3")
        rows = []
        for a, b, c, d, e in product(range(6), [0, 2, 5], [0, 5], [0, 3], [0, 1, 5]):
            t = {"concession": {"I0c.r1": str(Fraction(a, 5)), "I1g.r1": str(Fraction(b, 5)),
                                "I3g.r1": str(Fraction(c, 5)), "I3d.r1": str(Fraction(d, 5)),
                                "I3d.r1#deadline": str(Fraction(e, 5))}}
            rows.append((p1_vector(t, auth, order), p_rank(t, auth, order)))
        for lv, lr in rows:
            for rv, rr in rows:
                self.assertEqual(lv < rv, lr < rr)
                self.assertEqual(lv == rv, lr == rr)


class FromTheV47Corpus(unittest.TestCase):
    def test_trials_of_a_block_corpus(self):
        import importlib.util
        from pathlib import Path
        from types import SimpleNamespace
        from assurance.coordination import Intent
        from assurance.coordination.concession import evaluate_trials
        path = Path(__file__).resolve().parents[1] / "experiment_results/ota-20260911/ops/make_v47_corpus.py"
        spec = importlib.util.spec_from_file_location("make_v47_corpus", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        corpus = mod.build({"reference": {"ue1": 10.0, "ue2": 10.0, "ue3": 10.0, "gnb1Sum": 12.5}})
        intents = [Intent.from_record(i) for i in corpus["intents"]]
        owners = ("operator-gnb1", "ue1-video", "ue2-map", "ue3-incumbent")
        keys = {i.requirement.req_id: i.requirement.observation_key for i in intents}
        good = {keys["I0c.r1"]: 13.0, keys["I1g.r1"]: 7.6, keys["I2g.r1"]: 8.1,
                keys["I3g.r1"]: 5.0,
                keys["I1d.r1"]: {"byDeadlineMs": {"3000": 0.9}},
                keys["I2d.r1"]: {"byDeadlineMs": {"3000": 0.95}},
                keys["I3d.r1"]: {"byDeadlineMs": {"3000": 0.5, "4000": 0.7}}}
        bad = dict(good, **{keys["I2g.r1"]: 7.0})                 # protected ue2 missed
        out = evaluate_trials(intents, owners, [SimpleNamespace(trial_index=0, kpis=good),
                                                SimpleNamespace(trial_index=1, kpis=bad)])
        self.assertTrue(out["trials"][0]["attained"])
        self.assertFalse(out["trials"][0]["t0"])                  # ue3 conceded
        self.assertEqual([0, 0, 0], out["trials"][0]["ownerBins"][:3])
        self.assertFalse(out["trials"][1]["attained"])
        self.assertEqual(0, out["best"]["trialIndex"])


class CodexReviewFixes(unittest.TestCase):
    def test_an_invalid_window_is_unassessable(self):
        from types import SimpleNamespace
        from assurance.coordination.concession import evaluate_trials
        out = evaluate_trials([], ("a",), [SimpleNamespace(trial_index=3, kpis={}, window_valid=False)])
        self.assertFalse(out["trials"][0]["attained"])
        self.assertIsNone(out["best"])

    def test_the_corpus_refuses_a_non_positive_or_tiny_reference(self):
        import importlib.util
        from pathlib import Path
        path = Path(__file__).resolve().parents[1] / "experiment_results/ota-20260911/ops/make_v47_corpus.py"
        spec = importlib.util.spec_from_file_location("mk", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with self.assertRaises(SystemExit):
            mod.build({"reference": {"ue1": 0.0, "ue2": 9, "ue3": 9, "gnb1Sum": 12}})
        with self.assertRaises(SystemExit):
            mod.build({"reference": {"ue1": 0.001, "ue2": 9, "ue3": 9, "gnb1Sum": 12}})
        c = mod.build({"reference": {"ue1": 9.87, "ue2": 9.6, "ue3": 9.8, "gnb1Sum": 12.53}})
        i1g = next(i for i in c["intents"] if i["intentId"] == "I1g")["requirement"]
        self.assertEqual((7.4, 5.92), (i1g["value"], i1g["bound"]))   # 75% / 60% kept to 0.01
        self.assertNotIn("conflictNote", c["domain"])

    def test_p_rank_accepts_float_concessions(self):
        from assurance.coordination.preference import p_rank
        auth = {"I1g.r1": {"owner": "u1", "steps": 5}}
        t = {"concession": {"I1g.r1": 0.19999999999999987}}
        self.assertEqual(1, p_rank(t, auth, ("u1",)))


class V47TensionLevels(unittest.TestCase):
    """안 1 (off by default): levels on the configuration-A share, ue2 capped below the load."""
    def _mod(self):
        import importlib.util
        from pathlib import Path
        path = Path(__file__).resolve().parents[1] / "experiment_results/ota-20260911/ops/make_v47_corpus.py"
        spec = importlib.util.spec_from_file_location("make_v47_corpus_t", path)
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        return mod

    def test_contention_levels(self):
        mod = self._mod()
        ref = {"reference": {"ue1": 9.8, "ue2": 9.9, "ue3": 9.9, "gnb1Sum": 17.6},
               "raw": {"A": {"ue2": [9.0] * 5, "ue3": [8.5] * 5}}}
        lv = mod.contention_levels(ref)
        self.assertEqual(lv["I2g"], (9.45, None))            # 9.0 x 1.05
        self.assertEqual(lv["I3g"], (8.5, 5.1))               # x 1.00 / x 0.60
        self.assertEqual(lv["I0c"], (17.5, 9.45))             # sum x 1.00 / min(85%, ue2)
        self.assertEqual(lv["I1g"], mod.levels(ref["reference"])["I1g"])

    def test_ue2_capped_below_the_load(self):
        mod = self._mod()
        ref = {"reference": {"ue1": 9.8, "ue2": 9.9, "ue3": 9.9, "gnb1Sum": 19.0},
               "raw": {"A": {"ue2": [9.8] * 5, "ue3": [9.2] * 5}}}
        self.assertEqual(mod.contention_levels(ref)["I2g"], (9.7, None))   # min(10.29, 9.7)
