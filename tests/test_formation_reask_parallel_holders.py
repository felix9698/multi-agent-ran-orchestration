"""Board 938 (2026-09-29): Control finishing first must not clear Target's re-ask allowance."""
import types
import unittest

from tools.liveconsole.agent import _formation_reasks


class FormationReaskHoldersTest(unittest.TestCase):
    def test_first_holder_leaving_keeps_the_hook_for_the_other(self):
        agents = types.SimpleNamespace(may_reask=None)
        request = types.SimpleNamespace(formation_deadline_ms=600000)
        clock = types.SimpleNamespace(monotonic_ms=lambda: 1000.0)
        target = _formation_reasks(agents, request, clock, 0.0)
        control = _formation_reasks(agents, request, clock, 0.0)
        target.__enter__()
        control.__enter__()
        control.__exit__(None, None, None)          # Control is done first
        self.assertIsNotNone(agents.may_reask)      # Target may still re-ask
        self.assertTrue(agents.may_reask("refused"))
        target.__exit__(None, None, None)
        self.assertIsNone(agents.may_reask)


if __name__ == "__main__":
    unittest.main()
