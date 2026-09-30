"""셈 정책이 다른 판은 한 수로 합쳐지면 안 된다 (2026-09-23, codex 감사 #8).

`formalReferenceTrial` 은 기준 관측을 `N_max` 에 청구하고 달성 시각을 실제로 적는다;
`retainOnImprovement` 는 충족한 설정을 남긴다.  둘 다 **세는 방식**을 바꾸므로 시행 수와
달성 시각의 뜻이 달라진다.  `condition.name` 은 코퍼스 이름이라 이것을 말해 주지 않는다.
"""
import unittest

from experiments.agent_metrics import counting_policy, summarize


def _episode(method, **flags):
    return {'schemaVersion': 'agent-episode/1.3.0', 'episodeId': f'e{id(flags)}',
            'method': method, 'condition': {'name': 'pilot38-v4.5-L10-P1'},
            'timing': {'timingMode': 'cold-start'}, 'trials': [], 'calls': [],
            **flags}


class TheCountingPolicyIsPartOfTheCohortKey(unittest.TestCase):
    def test_a_record_without_the_keys_reads_as_both_off(self):
        self.assertEqual((False, False), counting_policy({}))

    def test_two_policies_do_not_share_a_group_or_an_overall_row(self):
        report = summarize([_episode('three-agent'),
                            _episode('three-agent', formalReferenceTrial=True,
                                     retainOnImprovement=True)])
        self.assertEqual(2, len(report['groups']), '같은 condition·method 인데 정책이 다르다')
        self.assertEqual(2, len(report['overall']), 'overall 이 다시 합치면 안 된다')
        self.assertEqual({(False, False), (True, True)},
                         {(row['formalReferenceTrial'], row['retainOnImprovement'])
                          for row in report['overall']})

    def test_the_same_policy_still_shares_one_row(self):
        report = summarize([_episode('three-agent', formalReferenceTrial=True),
                            _episode('three-agent', formalReferenceTrial=True)])
        self.assertEqual(1, len(report['overall']))
        self.assertTrue(report['overall'][0]['formalReferenceTrial'])
        self.assertFalse(report['overall'][0]['retainOnImprovement'])


if __name__ == '__main__':
    unittest.main()


class TheCampaignIsPartOfTheCohortKey(unittest.TestCase):
    """같은 코퍼스·같은 셈 정책이라도 캠페인이 다르면 한 수로 합치지 않는다."""

    def test_two_campaigns_under_one_policy_do_not_pool(self):
        from experiments.agent_metrics import campaign_of
        on = dict(formalReferenceTrial=True, retainOnImprovement=True)
        a = _episode('three-agent', **on); a['condition'] = {**a['condition'], 'campaign': 'blocks18-v46'}
        b = _episode('three-agent', **on); b['condition'] = {**b['condition'], 'campaign': 'blocks18-v46r'}
        self.assertEqual('blocks18-v46r', campaign_of(b))
        self.assertEqual('', campaign_of(_episode('three-agent')))
        report = summarize([a, b])
        self.assertEqual({'blocks18-v46', 'blocks18-v46r'},
                         {row['campaign'] for row in report['overall']})


class ACodeChangeInsideACampaignSplitsTheCohort(unittest.TestCase):
    """캠페인 중 라이브 경로 동결 -- 어기면 결과에서 저절로 갈라진다."""

    def test_two_source_digests_do_not_pool(self):
        from experiments.agent_metrics import campaign_of
        on = dict(formalReferenceTrial=True, retainOnImprovement=True)
        a = _episode('three-agent', **on, sourceDigest='a' * 64)
        b = _episode('three-agent', **on, sourceDigest='b' * 64)
        for e in (a, b):
            e['condition'] = {**e['condition'], 'campaign': 'blocks18-v46r4'}
        self.assertNotEqual(campaign_of(a), campaign_of(b))
        self.assertEqual(2, len(summarize([a, b])['overall']))
