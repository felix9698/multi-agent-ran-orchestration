"""The live restriction path must preserve the functions the model selected."""

from dataclasses import replace
from unittest.mock import patch

from tests.test_agent_sitting import AgentSittingFixture, C_ANSWER, T_ANSWER
from tools.liveconsole.agent import build_agent_sitting


class RestrictedFunctionIds(AgentSittingFixture):
    def test_asymmetric_ue_domains_keep_the_selected_steer_function(self):
        resolver, models = self.scripted({
            'target': [T_ANSWER],
            'control': [{**C_ANSWER, 'candidates': C_ANSWER['candidates'][:1]}],
        })

        def live_restriction(profile, request, **kwargs):
            return build_agent_sitting(
                profile, replace(request, restrict_catalog_to_candidates=True), **kwargs)

        # The emulated convenience entry normally disables this live-only
        # reduction. Exercise its real composition with asymmetric C instead.
        with patch('socket.socket', side_effect=AssertionError('network forbidden')):
            with patch('tools.liveconsole.agent.build_agent_sitting', side_effect=live_restriction):
                sitting = self.build(resolver=resolver, role_models=models,
                                     axes=('servingCell',), budget_trials=1)
        self.assertEqual(('C0', 'C1'), sitting.controls.control_ids)
        self.assertEqual('steer', sitting.controls.candidate('C1').functions[0].function_id)
        self.assertEqual(('12345678',), sitting.controls.action_space['servingCell@132'])
        self.assertEqual([], sitting.preflight['droppedCandidates'])
        self.assertEqual([], sitting.preflight['unmappedControls'])

