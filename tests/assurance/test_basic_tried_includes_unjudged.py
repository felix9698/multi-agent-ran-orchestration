"""Pilot board 733 (2026-09-26): a trial with an unjudgeable window leaves no observation, yet
its configuration was tried -- the basic monolith's tried list must still carry it."""
import unittest

from assurance.coordination.agents import BasicInputs


class TriedIncludesUnjudged(unittest.TestCase):
    def test_dispatched_without_observation_is_tried(self):
        inputs = BasicInputs(observations=({"configuration": {"a": "1"}},),
                             dispatched=({"a": "1"}, {"a": 2}))
        self.assertEqual(({"a": "1"}, {"a": "2"}), inputs.tried_configurations)
        self.assertEqual([{"a": "1"}, {"a": "2"}], inputs.payload()["input.tried_configurations"])


if __name__ == "__main__":
    unittest.main()
