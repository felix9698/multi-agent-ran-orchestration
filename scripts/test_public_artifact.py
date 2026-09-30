#!/usr/bin/env python3
"""Run the publication's self-contained offline regression selection.

This intentionally does not run old release-packaging tests or tests requiring
the unavailable original OTA pilot corpus. No LLM or radio integration is selected.
"""
from pathlib import Path
import os
import subprocess
import sys

TESTS = (
    'tests.assurance.test_rule_greedy',
    'tests.assurance.test_v52_view',
    'tests.assurance.test_v53_view',
    'tests.assurance.test_v53_fair_prompts',
    'tests.assurance.test_v51_weighted_evaluator.Weighted',
    'tests.assurance.test_kern_replay',
    'tests.test_agent_metrics',
    'tests.assurance.test_kern_evaluator',
    'tests.assurance.test_kern_lifecycle',
    'tests.assurance.test_kern_evidence',
    'tests.assurance.test_kern_mailbox',
    'tests.assurance.test_kcon_contracts',
    'tests.assurance.test_kgw_gateway',
    'tests.assurance.test_kgw_partial_apply',
    'tests.assurance.test_kgw_recovery',
    'tests.assurance.test_kgw_fencing',
    'tests.assurance.test_coordination_intake',
    'tests.assurance.test_coordination_predictor',
    'tests.assurance.test_coordination_board',
    'tests.assurance.test_coordination_model_provenance',
    'tests.assurance.test_coordination_tc',
    'tests.assurance.test_coordination_agents',
    'tests.assurance.test_vertical_replay',
    'tests.test_agent_metrics_continuity',
    'tests.test_agent_quality',
    'tests.test_public_credentials',
    'tests.test_public_llm_configuration',
)

def main():
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', MPLBACKEND='Agg')
    # Ignore inherited live-experiment/model settings for this offline selection.
    for name in tuple(env):
        if name.startswith(('AIC_', 'ANTHROPIC_', 'OPENAI_', 'GOOGLE_', 'LITELLM_')):
            env.pop(name)
    return subprocess.call([sys.executable, '-m', 'unittest', '-q', *TESTS],
                           cwd=root, env=env)

if __name__ == '__main__':
    raise SystemExit(main())
