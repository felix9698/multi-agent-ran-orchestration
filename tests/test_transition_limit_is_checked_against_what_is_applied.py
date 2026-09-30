"""전이 한도는 C0 가 아니라 **지금 적용된 설정**에 대고 재야 한다 (2026-09-23 결정 §3.3).

후보 승인 때 세는 수는 C0 기준이고 그것이 맞다 -- C0 는 후보를 전개하는 고정 기준이다.
그런데 유지 모드에서는 배치가 C1 에 앉아 있을 수 있고, 서로 다른 축을 쓰는 두 후보는
각각 C0 와 4축만 달라도 C1→C2 전이가 8축을 움직인다 (codex 감사 #5).
"""
import unittest
from types import SimpleNamespace

from assurance.coordination.tc import CompatibilityRules, ControlCandidate, ControlCandidates
from tools.liveconsole.agent import AgentSitting, MAX_CHANGED_ENTRIES


BASELINE = {f"{axis}@ue{ue}": "0" for axis in ("a", "b") for ue in (1, 2, 3)}
BASELINE["c@ue1"] = "0"
BASELINE["c@ue2"] = "0"


def _sitting(applied, configuration):
    candidate = ControlCandidate(control_id="C9", configuration=configuration)
    sitting = AgentSitting.__new__(AgentSitting)
    sitting.controls = ControlCandidates(candidates=(candidate,))
    sitting.compatibility = CompatibilityRules(max_changed_entries=MAX_CHANGED_ENTRIES)
    sitting.retention_runtime = None
    sitting.runtime = SimpleNamespace(live_baseline=lambda: dict(applied))
    sitting.baselines = dict(BASELINE)
    return sitting


class TheTransitionLimitFollowsTheAppliedConfiguration(unittest.TestCase):
    def test_four_moves_from_what_is_applied_is_allowed(self):
        applied = dict(BASELINE)
        configuration = {**BASELINE, "a@ue1": "1", "a@ue2": "1", "a@ue3": "1", "b@ue1": "1"}
        self.assertEqual("", _sitting(applied, configuration)._transition_exceeds("C9"))

    def test_a_candidate_within_four_of_C0_can_still_be_eight_from_what_is_applied(self):
        """두 후보가 서로 다른 축을 쓰면 승인 검사는 둘 다 통과시킨다."""
        applied = {**BASELINE, "a@ue1": "1", "a@ue2": "1", "a@ue3": "1", "b@ue1": "1"}
        configuration = {**BASELINE, "b@ue2": "1", "b@ue3": "1", "c@ue1": "1", "c@ue2": "1"}
        reason = _sitting(applied, configuration)._transition_exceeds("C9")
        self.assertIn("moves 8 function/scope entries", reason)
        self.assertIn("at most 4 may change per transition", reason)

    def test_an_unknown_control_or_empty_belief_refuses_nothing(self):
        applied = dict(BASELINE)
        sitting = _sitting(applied, dict(BASELINE))
        self.assertEqual("", sitting._transition_exceeds("C404"))
        sitting.runtime = SimpleNamespace(live_baseline=dict)
        sitting.baselines = {}
        self.assertEqual("", sitting._transition_exceeds("C9"))


if __name__ == "__main__":
    unittest.main()
