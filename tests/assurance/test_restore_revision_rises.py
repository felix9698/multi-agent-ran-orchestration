"""2026-09-29 board 885: two restore writes of one trial (HALT hand-back, UNDO re-write) went out
at the same revision (trial+1000); R1 refused the second AIC_STALE_REVISION, the DELETE never
followed and the policy kept ue2's scope.  Each restore write must draft a higher revision."""
import unittest

from tools.g3ota.composition import LivePolicyBuilder, RESTORE_REVISION_OFFSET


class EachRestoreWriteRises(unittest.TestCase):
    def test_restores_of_one_trial_rise_and_trials_are_independent(self):
        b = object.__new__(LivePolicyBuilder)
        b.restores = {}
        revs = [3 + RESTORE_REVISION_OFFSET + b._next_restore(3) for _ in range(3)]
        self.assertEqual([1003, 1004, 1005], revs)
        self.assertEqual(1004, 4 + RESTORE_REVISION_OFFSET + b._next_restore(4))


if __name__ == "__main__":
    unittest.main()
