"""Every method must be scored on the same target domain.

2026-09-23 (external review of the V44 package, confirmed against the raw
records): `Trial.judge_against` scores against **the contract it is handed**, and
the sitting hands it `self.contract` -- which is the *formed* T for three-agent
and internal-monolith (6-8 targets) but `expand_targets(authorization)` for
basic-monolith (144).  basic-monolith therefore had up to 18x more targets
available to match, which invalidates any cross-method comparison of attainment.

2026-09-23: the fix is in (`AgentSitting.evaluation_contract = omega_contract(contract)`,
the grid stands on it, and every `judge_against` call uses it).  The skip is removed
and the check runs a hardware-free sitting of **each** compared method (codex audit:
"the full-domain verification the G3 comment points to is still a skipped,
unimplemented test").
"""
import unittest

from assurance.coordination.tc import Trial


class TestEvaluationDomainIsCommon(unittest.TestCase):
    def test_judge_against_scores_the_contract_it_is_handed(self):
        """The mechanism, stated so the asymmetry cannot be called a mystery."""
        import inspect
        source = inspect.getsource(Trial.judge_against)
        self.assertIn('for target in contract.targets', source,
                      'judging iterates the handed contract; whoever chooses that '
                      'contract chooses the domain')

    def test_every_method_is_scored_on_the_full_expansion(self):
        """Every trial of every method is judged over the same Omega -- by vector.

        Non-vacuous by construction: with no role models the Target step falls back
        to the mandatory anchors, so three-agent and internal-monolith form a T
        **smaller** than Omega.  Judging against that T was the asymmetry.
        """
        from tests.test_agent_sitting import AgentSittingFixture, I1, I2
        from assurance.coordination import (
            METHOD_BASIC_MONOLITH, METHOD_INTERNAL_MONOLITH, METHOD_THREE_AGENT)

        def signature(target):
            return (tuple(sorted(dict(target.levels).items())),
                    tuple(sorted(dict(target.deadline_levels).items())))

        omegas = {}
        for method in (METHOD_THREE_AGENT, METHOD_INTERNAL_MONOLITH, METHOD_BASIC_MONOLITH):
            with self.subTest(method=method):
                fixture = AgentSittingFixture()
                fixture.setUp()
                sitting = fixture.build([I1, I2], method=method, budget_trials=2)
                sitting.confirm()
                sitting.run()
                omega = {t.target_id: signature(t) for t in sitting.evaluation_contract.targets}
                omegas[method] = sorted(omega.values())
                self.assertTrue(sitting.grid.trials, "the sitting ran no trial")
                for trial in sitting.grid.trials:
                    self.assertEqual(set(omega), set(trial.success),
                                     f"trial {trial.trial_index} is not judged over Omega")
                self.assertEqual(set(omega), set(sitting.grid.to_record()["targets"]),
                                 "the grid's rows are not Omega")
                if method != METHOD_BASIC_MONOLITH:
                    self.assertLess(len(sitting.contract.targets), len(omega),
                                    "vacuous: the formed T is already all of Omega")
                fixture.doCleanups()
        self.assertEqual(1, len({tuple(v) for v in omegas.values()}),
                         "the three methods were not judged over the same Omega")


if __name__ == '__main__':
    unittest.main()
