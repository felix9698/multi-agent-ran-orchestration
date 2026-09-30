"""v5 retention (2026-09-26): the retained control is the common evaluator's best over the whole
Omega, not the best column of the arm's T (which under v5 attained nothing, so boards 681 and 684
tried to retain C0 while the report's best was trial 2 at p=11)."""
import unittest
from types import SimpleNamespace

from tools.liveconsole.agent import AgentSitting


class _Stub:
    contract = SimpleNamespace(t0=SimpleNamespace(target_id="T0"))
    evaluation_contract = contract
    grid = SimpleNamespace(
        trials=(SimpleNamespace(trial_index=0, control_id="C0", kernel={}, success={}),
                SimpleNamespace(trial_index=2, control_id="C10", kernel={}, success={})),
        best_attained=lambda _contract: None)            # T attains nothing
    controls = SimpleNamespace(candidate=lambda _id: SimpleNamespace(configuration={"a": "1"}))
    request = SimpleNamespace(retain_best=True)
    retained = None
    _disconnect_trial_ids = staticmethod(lambda: frozenset())
    _unaddressable_ues = staticmethod(lambda: ())
    applied_configuration = staticmethod(lambda: {"a": "1"})   # already held: no new trial
    _v5_order = staticmethod(lambda: ("I4e.r1",))
    _v51 = staticmethod(lambda: False)
    _v5_online = staticmethod(lambda: {"bestSoFar": {"attained": True, "p": 11, "trialIndex": 2}})
    intents = ()


class V5Retention(unittest.TestCase):
    def test_the_common_best_is_retained_not_c0(self):
        stub = _Stub()
        AgentSitting._retain(stub, 1, None)
        self.assertEqual("C10", stub.retained["controlId"])
        self.assertEqual("omega:p=11", stub.retained["targetId"])

    def test_nothing_attained_falls_back_to_c0(self):
        stub = _Stub()
        stub._v5_online = lambda: {"bestSoFar": {"attained": False}}
        AgentSitting._retain(stub, 1, None)
        self.assertEqual("C0", stub.retained["controlId"])


if __name__ == "__main__":
    unittest.main()
