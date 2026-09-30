"""판이 켜고 돈 두 셈 정책을 판 스스로 적는다 (2026-09-23)."""
import unittest

from assurance.coordination.tc import EpisodeRecord


class TheCountingPoliciesAreOnTheRecord(unittest.TestCase):
    """`condition.name` 은 코퍼스 이름이라 셈 정책을 말해 주지 못한다."""

    def test_both_policies_round_trip(self):
        for reference, retain in ((False, False), (True, False),
                                  (False, True), (True, True)):
            record = EpisodeRecord(episode_id="e", method="three-agent",
                                   formal_reference_trial=reference,
                                   retain_on_improvement=retain).to_record()
            self.assertEqual(reference, record["formalReferenceTrial"])
            self.assertEqual(retain, record["retainOnImprovement"])
            back = EpisodeRecord.from_record(record)
            self.assertEqual(reference, back.formal_reference_trial)
            self.assertEqual(retain, back.retain_on_improvement)

    def test_an_older_record_without_the_keys_reads_as_off(self):
        """셈 정책이 없던 판은 꺼진 상태로 돈 판이다 -- 기본값이 그것이다."""
        back = EpisodeRecord.from_record({"episodeId": "old", "method": "three-agent"})
        self.assertFalse(back.formal_reference_trial)
        self.assertFalse(back.retain_on_improvement)


if __name__ == "__main__":
    unittest.main()
