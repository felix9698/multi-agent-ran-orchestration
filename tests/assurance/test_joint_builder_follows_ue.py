"""Every steering write names the UE the role *is*, not the one it was.

2026-09-20 board 123728: the trial steered ue1 away, ue1 re-registered (AMF id
431 -> 434) while the trial ran, and the hand-back policy was still built
against 431.  The radio never saw it, the UE never came back, the readback
produced no observation and the Kernel locked the trial down.  The readback had
followed the role since 2026-09-16; the body had not.
"""
import unittest

from assurance.live.joint_runtime import _builder_following_the_ue


class _Participant:
    def __init__(self, ids):
        self._ids = list(ids)
        self.built = 0

    def amf_of(self):
        return self._ids[0] if self._ids else None

    def advance(self):
        if len(self._ids) > 1:
            self._ids.pop(0)

    def policy_builder_factory(self, kernel, joint, participant):
        self.built += 1
        pinned = self._ids[0] if self._ids else None
        return lambda command: {"ueId": str(pinned), "command": dict(command)}


class TheBodyFollowsTheRole(unittest.TestCase):
    def test_a_re_registration_is_written_against_the_new_id(self):
        participant = _Participant([431, 434])
        build = _builder_following_the_ue(participant, kernel=object(), joint=object())
        self.assertEqual("431", build({"axis": "servingCell"})["ueId"])
        participant.advance()
        self.assertEqual("434", build({"axis": "servingCell"})["ueId"])
        self.assertEqual(2, participant.built)   # rebuilt exactly once more

    def test_a_steady_role_is_built_once(self):
        participant = _Participant([431])
        build = _builder_following_the_ue(participant, kernel=object(), joint=object())
        for _ in range(3):
            self.assertEqual("431", build({"axis": "servingCell"})["ueId"])
        self.assertEqual(1, participant.built)

    def test_a_participant_without_a_resolver_is_untouched(self):
        participant = _Participant([431])
        participant.amf_of = None
        build = _builder_following_the_ue(participant, kernel=object(), joint=object())
        self.assertEqual("431", build({"axis": "servingCell"})["ueId"])
        self.assertEqual(1, participant.built)


if __name__ == "__main__":
    unittest.main()
