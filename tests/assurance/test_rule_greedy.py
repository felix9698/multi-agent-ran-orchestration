"""rule-greedy (owner 2026-09-30): a deterministic baseline in the basic monolith's place."""
import unittest

from assurance.coordination import (Authorization, BasicInputs, RoleAgents, RoleModels)
from assurance.coordination.agents import METHOD_BASIC_MONOLITH
from assurance.coordination.rule_greedy import RULE_GREEDY, decide
from tests.assurance.test_coordination_agents import (AWAY, CATALOG, COMPATIBILITY, HOME,
                                                      intents, network_state)


def _no_model(name):
    raise AssertionError(f"rule-greedy must not resolve a model ({name})")


class RuleGreedyInTheBasicMonolithsPlace(unittest.TestCase):
    def setUp(self):
        self.state, self.record = network_state()
        self.applied = self.state.applied_configuration()
        self.agents = RoleAgents(models=RoleModels(monolith=RULE_GREEDY,
                                                   method=METHOD_BASIC_MONOLITH),
                                 resolver=_no_model)

    def inputs(self, kpis, tried=()):
        return BasicInputs(
            intents=tuple(intents()), authorization=Authorization.from_intents(intents()),
            function_catalog=CATALOG, compatibility=COMPATIBILITY, network_state=self.record,
            observations=({"trialIndex": 0, "valid": True, "configuration": dict(self.applied),
                           "kpis": kpis},),
            dispatched=tuple(tried))

    def test_a_short_ue_moves_its_competitor_off_the_shared_cell(self):
        decision, record = self.agents.basic_monolith_decide(
            self.inputs({"dlGoodputMbps@131": 1.0, "dlGoodputMbps@132": 1.6}))
        self.assertEqual(record.model, RULE_GREEDY)
        self.assertTrue(record.accepted)
        self.assertEqual(decision.configuration["servingCell@132"], AWAY)
        self.assertEqual(decision.configuration["servingCell@131"], HOME)

    def test_a_tried_step_is_skipped_for_the_next_rule(self):
        moved = dict(self.applied, **{"servingCell@132": AWAY})
        decision, _ = self.agents.basic_monolith_decide(
            self.inputs({"dlGoodputMbps@131": 1.0, "dlGoodputMbps@132": 1.6}, tried=[moved]))
        self.assertEqual(decision.configuration["dlPrbCap@132"], "12")    # cap the competitor

    def test_nothing_short_means_no_step(self):
        decision, record = self.agents.basic_monolith_decide(
            self.inputs({"dlGoodputMbps@131": 3.5, "dlGoodputMbps@132": 1.6}))
        self.assertIsNone(decision)
        self.assertFalse(record.accepted)


class TheRuleStartsFromTheBestConfigurationMeasured(unittest.TestCase):
    def test_a_worse_step_is_abandoned(self):
        reqs = {"I1": {"kpi": "dlGoodputMbps", "scope": "ue@a", "op": ">=",
                       "original": 5.0, "limit": 3.0}}
        c0 = {"servingCell@a": "X", "servingCell@b": "X", "txAttenuationDb@X": "8.0"}
        worse = dict(c0, **{"txAttenuationDb@X": "8.5"})
        dom = {"servingCell": ("values", ["X", "Y"]), "txAttenuationDb": ("range", 8.0, 20.0, 0.5)}
        obs = [{"trialIndex": 0, "valid": True, "configuration": c0, "kpis": {"dlGoodputMbps@a": 4.0}},
               {"trialIndex": 1, "valid": True, "configuration": worse, "kpis": {"dlGoodputMbps@a": 3.0}}]
        got, _why = decide(reqs, obs, worse, [c0, worse], dom)
        self.assertEqual(got["txAttenuationDb@X"], "8.0")     # built on c0, not on the worse trial
        self.assertEqual(got["servingCell@b"], "Y")


LIVE_DOMAINS = {   # the stored catalog expands every range into its value strings
    "servingCell": ("values", ["12345678", "87654321"]),
    "dlPrbCap": ("values", [str(v) for v in range(37, -1, -1)]),
    "pfWeight": ("values", [str(v / 4) for v in range(1, 33)]),
    "txAttenuationDb": ("values", [f"{8 + v / 2:.1f}" for v in range(25)]),
}


class OnTheLiveCatalogShape(unittest.TestCase):
    reqs = {"I4e.r1": {"kpi": "cellTxAttenuationDb", "scope": "cell@12345678", "op": ">=",
                       "original": 20.0, "limit": 8.0},
            "I2d.r1": {"kpi": "deadlineSuccessRatio", "scope": "ue@ue2", "op": ">=",
                       "original": 0.9, "limit": 0.6, "deadlineMs": 100.0}}
    c0 = {"servingCell@ue2": "12345678", "servingCell@ue3": "12345678", "dlPrbCap@ue2": "0",
          "dlPrbCap@ue3": "0", "pfWeight@ue2": "1.0", "pfWeight@ue3": "1.0",
          "txAttenuationDb@12345678": "8.0"}

    def obs(self, ratio):
        return [{"trialIndex": 0, "valid": True, "configuration": self.c0,
                 "kpis": {"deadlineSuccessRatio@ue2": {"byDeadlineMs": {"100": ratio}},
                          "cellTxAttenuationDb@12345678": 8.0}}]

    def test_energy_steps_use_the_catalog_string(self):
        got, _ = decide(self.reqs, self.obs(0.95), self.c0, [self.c0], LIVE_DOMAINS)
        self.assertEqual(got["txAttenuationDb@12345678"], "8.5")

    def test_pf_weight_doubles_to_a_listed_value(self):
        tried = [self.c0, dict(self.c0, **{"servingCell@ue3": "87654321"})]
        got, why = decide(self.reqs, self.obs(0.0), self.c0, tried, LIVE_DOMAINS)
        self.assertEqual(got["pfWeight@ue2"], "2.0", why)

    def test_a_spent_cap_moves_on_to_a_tighter_one(self):
        tried = [self.c0, dict(self.c0, **{"servingCell@ue3": "87654321"}),
                 dict(self.c0, **{"pfWeight@ue2": "2.0"}), dict(self.c0, **{"dlPrbCap@ue3": "25"})]
        got, why = decide(self.reqs, self.obs(0.0), self.c0, tried, LIVE_DOMAINS)
        self.assertEqual(got["dlPrbCap@ue3"], "20", why)


    def test_below_the_coarse_ladder_it_keeps_tightening(self):
        base = dict(self.c0, **{"dlPrbCap@ue3": "5"})
        obs = [{"trialIndex": 0, "valid": True, "configuration": base,
                "kpis": {"deadlineSuccessRatio@ue2": {"byDeadlineMs": {"100": 0.0}},
                         "cellTxAttenuationDb@12345678": 8.0}}]
        tried = [base, dict(base, **{"servingCell@ue3": "87654321"}),
                 dict(base, **{"pfWeight@ue2": "2.0"})]
        got, why = decide(self.reqs, obs, base, tried, LIVE_DOMAINS)
        self.assertEqual(got["dlPrbCap@ue3"], "4", why)

    def test_a_scope_uses_its_own_ladder(self):
        domains = dict(LIVE_DOMAINS, **{"dlPrbCap@ue3": ("values", ["0", "12", "6"])})
        tried = [self.c0, dict(self.c0, **{"servingCell@ue3": "87654321"}),
                 dict(self.c0, **{"pfWeight@ue2": "2.0"})]
        got, why = decide(self.reqs, self.obs(0.0), self.c0, tried, domains)
        self.assertEqual(got["dlPrbCap@ue3"], "12", why)


    def test_no_valid_measurement_still_proposes_a_step(self):
        got, why = decide(self.reqs, [], self.c0, [], LIVE_DOMAINS)
        self.assertIsNotNone(got, why)

    def test_a_refused_start_falls_back_to_the_applied_configuration(self):
        far = dict(self.c0, **{"servingCell@ue3": "87654321", "pfWeight@ue2": "2.0"})
        best_first = lambda cfg: cfg.get("servingCell@ue3") == "12345678"   # refuses every step from c0
        got, why = decide(self.reqs, self.obs(0.0), far, [self.c0, far], LIVE_DOMAINS,
                          refused=best_first)
        self.assertEqual(got["servingCell@ue3"], "87654321", why)


    def test_a_local_optimum_moves_on_to_the_next_best_measurement(self):
        """Board 952: 8.5 dB best (energy short only, 9.0 tried), 9.0 dB broke ue2's deadline."""
        reqs = dict(self.reqs)
        att85 = dict(self.c0, **{"txAttenuationDb@12345678": "8.5"})
        att90 = dict(self.c0, **{"txAttenuationDb@12345678": "9.0"})
        obs = [{"trialIndex": 0, "valid": True, "configuration": self.c0,
                "kpis": {"deadlineSuccessRatio@ue2": {"byDeadlineMs": {"100": 0.95}},
                         "cellTxAttenuationDb@12345678": 8.0}},
               {"trialIndex": 1, "valid": True, "configuration": att85,
                "kpis": {"deadlineSuccessRatio@ue2": {"byDeadlineMs": {"100": 0.95}},
                         "cellTxAttenuationDb@12345678": 8.5}},
               {"trialIndex": 2, "valid": True, "configuration": att90,
                "kpis": {"deadlineSuccessRatio@ue2": {"byDeadlineMs": {"100": 0.3}},
                         "cellTxAttenuationDb@12345678": 9.0}}]
        got, why = decide(reqs, obs, self.c0, [self.c0, att85, att90], LIVE_DOMAINS)
        self.assertIsNotNone(got, "a local optimum must not end the search")
        self.assertEqual(got["txAttenuationDb@12345678"], "9.0", why)     # repair the 9.0 dB point
        self.assertEqual(got["servingCell@ue3"], "87654321", why)


if __name__ == "__main__":
    unittest.main()
