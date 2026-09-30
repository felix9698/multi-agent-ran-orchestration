"""v5 design (owner 2026-09-26): one owner, four coordinates compared lexicographically
(E, UE1 goodput, UE2 deadline success, UE3 goodput) on k = ceil(20 q), UE2 goodput a fixed
condition, only T0 mandatory, and one common evaluator for the report, the selector and retention."""
import importlib.util
import os
import unittest
from pathlib import Path
from types import SimpleNamespace

from assurance.coordination import concession, tc
from assurance.coordination.tc import Authorization, Intent, Preference, TargetContract

ROOT = Path(__file__).resolve().parents[1]
REF = {"reference": {"ue1": 10.0, "ue2": 8.0, "ue3": 10.0, "gnb1Sum": 12.0},
       "ue2DeadlineMs": 450.0, "energyBaselineDb": 6.0, "loadMbps": 9.3}


def _corpus_module():
    spec = importlib.util.spec_from_file_location(
        "make_v47_corpus_v5", ROOT / "experiment_results/ota-20260911/ops/make_v47_corpus.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _intents():
    return [Intent.from_record(i) for i in _corpus_module().build_v5(REF)["intents"]]


def setUpModule():
    # v5 ranking is on only when the corpus declares the design (Codex review 2026-09-26).
    os.environ["AIC_DESIGN"] = "v5"


def tearDownModule():
    os.environ.pop("AIC_DESIGN", None)


class FlagGatesTheDesign(unittest.TestCase):
    def test_one_owner_without_the_declaration_keeps_the_old_ranking(self):
        os.environ.pop("AIC_DESIGN", None)
        try:
            self.assertEqual((), Preference.from_intents(_intents()).coordinate_order)
        finally:
            os.environ["AIC_DESIGN"] = "v5"

    def test_an_invalid_observation_is_never_an_attainment(self):
        intents = _intents()
        order = Preference.from_intents(intents).coordinate_order
        kpis = {"cellTxAttenuationDb@87654321": 18.0, "dlGoodputMbps@328": 9.5,
                "deadlineSuccessRatio@330": {450.0: 0.9}, "dlGoodputMbps@327": 9.5,
                "dlGoodputMbps@330": 5.0}
        good = SimpleNamespace(trial_index=1, window_valid=True, kpis=kpis,
                               observation_validity={"valid": True})
        bad = SimpleNamespace(trial_index=2, window_valid=True, kpis=kpis,
                              observation_validity={"valid": False, "reason": "PARTIAL_APPLY"})
        report = concession.evaluate_trials(intents, order, [bad, good], by_requirement=True)
        self.assertFalse(report["trials"][0]["attained"])
        self.assertEqual(1, report["best"]["trialIndex"])


class Corpus(unittest.TestCase):
    def test_levels_from_the_reference(self):
        rows = {i["intentId"]: i["requirement"] for i in _corpus_module().build_v5(REF)["intents"]}
        self.assertEqual((rows["I4e"]["value"], rows["I4e"]["bound"]), (18.0, 6.0))
        self.assertEqual((rows["I1g"]["value"], rows["I1g"]["bound"]), (9.5, 6.0))
        self.assertEqual((rows["I2d"]["value"], rows["I2d"]["bound"], rows["I2d"]["deadlineSteps"],
                          rows["I2d"]["deadlineMs"]), (0.9, 0.6, 0, 450.0))
        self.assertEqual((rows["I3g"]["value"], rows["I3g"]["bound"]), (9.5, 4.0))
        self.assertEqual((rows["I2g"]["value"], rows["I2g"]["steps"]), (3.2, 0))
        self.assertNotIn("I0c", rows)

    def test_refuses_without_deadline_or_baseline(self):
        mod = _corpus_module()
        for drop in ("ue2DeadlineMs", "energyBaselineDb"):
            with self.assertRaises(SystemExit):
                mod.build_v5({k: v for k, v in REF.items() if k != drop})

    def test_attenuation_ladder_is_one_named_constant(self):
        mod = _corpus_module()
        self.assertEqual(mod.v5_att_cells(REF), "87654321:6,9,12,15,18,21")
        self.assertFalse(mod.v5_enabled() and not mod.V5_FLAG.exists())


class Preference_(unittest.TestCase):
    def test_single_owner_orders_coordinates_by_priority(self):
        pref = Preference.from_intents(_intents())
        self.assertEqual(pref.coordinate_order, ("I4e.r1", "I1g.r1", "I2d.r1", "I3g.r1", "I2g.r1"))
        self.assertEqual(Preference.from_record(pref.to_record()), pref)

    def test_evaluator_p_is_the_mixed_radix_of_k(self):
        intents = _intents()
        order = Preference.from_intents(intents).coordinate_order
        reqs = concession.requirements_from_intents(intents, by_requirement=True)
        obs = {"cellTxAttenuationDb@87654321": 18.0, "dlGoodputMbps@328": 9.15,
               "deadlineSuccessRatio@330": {450.0: 0.78}, "dlGoodputMbps@327": 4.2,
               "dlGoodputMbps@330": 3.3}
        v = concession.evaluate(reqs, order, obs)
        self.assertTrue(v.attained)
        k = dict(v.coordinates)
        self.assertEqual(v.p, 21 ** 3 * k["I4e.r1"] + 21 ** 2 * k["I1g.r1"]
                         + 21 * k["I2d.r1.ratio"] + k["I3g.r1"])
        self.assertEqual((k["I4e.r1"], k["I1g.r1"], k["I2d.r1.ratio"]), (0, 2, 8))

    def test_ue2_goodput_below_its_fixed_level_attains_nothing(self):
        intents = _intents()
        order = Preference.from_intents(intents).coordinate_order
        reqs = concession.requirements_from_intents(intents, by_requirement=True)
        obs = {"cellTxAttenuationDb@87654321": 18.0, "dlGoodputMbps@328": 9.5,
               "deadlineSuccessRatio@330": {450.0: 0.9}, "dlGoodputMbps@327": 9.5,
               "dlGoodputMbps@330": 3.1}
        self.assertFalse(concession.evaluate(reqs, order, obs).attained)

    def test_lexicographic_example_from_the_design(self):
        # (0, 2, 6, 15) is preferred to (0, 3, 0, 0): UE1's smaller concession decides.
        a = (0, 2, 6, 15)
        b = (0, 3, 0, 0)
        p = lambda k: 21 ** 3 * k[0] + 21 ** 2 * k[1] + 21 * k[2] + k[3]
        self.assertLess(a, b)
        self.assertLess(p(a), p(b))


class TargetsAndMandatory(unittest.TestCase):
    def setUp(self):
        intents = _intents()
        self.a = Authorization.from_intents(intents, preference=Preference.from_intents(intents))
        self.t0 = {"requirements": {k: e.original for k, e in self.a.requirements.items()}}

    def test_only_t0_is_mandatory(self):
        contract = tc.omega_or_sparse(self.a)
        self.assertEqual(tc.boundary_targets(contract), (contract.t0,))
        self.assertEqual(len(tc.mandatory_contract(contract).targets), 1)

    def test_preference_key_is_k_in_order_without_dmax(self):
        rows = [{"levels": {"I4e.r1": 0, "I1g.r1": 2, "I2d.r1": 6, "I3g.r1": 15, "I2g.r1": 0}},
                {"levels": {"I4e.r1": 0, "I1g.r1": 3, "I2d.r1": 0, "I3g.r1": 0, "I2g.r1": 0}}]
        contract = TargetContract.from_compact(self.t0, {}, [], {}, self.a, rows)
        keys = [tc.preference_key(t, self.a) for t in contract.alternatives]
        self.assertEqual(sorted(keys), [(0.0, 2.0, 6.0, 15.0, 0.0), (0.0, 3.0, 0.0, 0.0, 0.0)])
        self.assertEqual(contract.alternatives[0].levels["I1g.r1"], 2)   # ranked first

    def test_the_model_may_carry_eight_additions(self):
        rows = [{"levels": {"I4e.r1": q, "I1g.r1": 0, "I2d.r1": 0, "I3g.r1": 0, "I2g.r1": 0}}
                for q in range(1, 9)]
        contract = TargetContract.from_compact(self.t0, {}, [], {}, self.a, rows)
        self.assertEqual(len(contract.targets), 9)


class PromptAndValidatorShareOneNumber(unittest.TestCase):
    def test_v5_prompts_state_the_validator_limit(self):
        from assurance.coordination import agents
        intents = _intents()
        a = Authorization.from_intents(intents, preference=Preference.from_intents(intents))
        n = tc.model_addition_limit(a)
        self.assertEqual(n, tc.MAX_TARGETS - 1)
        words = {7: "seven", 8: "eight", 9: "nine", 5: "five", 6: "six"}
        for prompt in (agents.TARGET_SYSTEM_PROMPT, agents.MONOLITH_FORM_SYSTEM_PROMPT):
            self.assertIn(f"up to {words[n]} additional", agents.prompt_with_additions(prompt, a))
        # The validator admits exactly n and refuses n + 1.
        t0 = {"requirements": {k: e.original for k, e in a.requirements.items()}}
        rows = lambda m: [{"levels": {"I4e.r1": q, "I1g.r1": 0, "I2d.r1": 0, "I3g.r1": 0,
                                      "I2g.r1": 0}} for q in range(1, m + 1)]
        self.assertEqual(len(TargetContract.from_compact(t0, {}, [], {}, a, rows(n)).targets), n + 1)
        if n + 1 <= 20:
            with self.assertRaises(tc.TargetValidationError):
                TargetContract.from_compact(t0, {}, [], {}, a, rows(n + 1))


class OnlineRetentionRank(unittest.TestCase):
    def test_retention_rank_is_the_common_evaluator_p(self):
        from tools.liveconsole.agent import AgentSitting
        intents = _intents()
        pref = Preference.from_intents(intents)
        stub = SimpleNamespace(intents=intents, contract=SimpleNamespace(preference=pref))
        stub._v5_order = lambda: AgentSitting._v5_order(stub)
        stub._v51 = lambda: AgentSitting._v51(stub)
        good = {"cellTxAttenuationDb@87654321": 18.0, "dlGoodputMbps@328": 9.5,
                "deadlineSuccessRatio@330": {450.0: 0.9}, "dlGoodputMbps@327": 9.5,
                "dlGoodputMbps@330": 3.3}
        rank, label = AgentSitting._best_p1_rank(stub, good)
        self.assertEqual((rank, label), (0.0, "omega:p=0"))
        rank, _ = AgentSitting._best_p1_rank(stub, dict(good, **{"dlGoodputMbps@330": 1.0}))
        self.assertEqual(rank, float("inf"))


if __name__ == "__main__":
    unittest.main()
