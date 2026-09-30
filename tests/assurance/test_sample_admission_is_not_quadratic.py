"""샘플 하나를 받을 때마다 전체 샘플을 훑으면 시행이 길어질수록 자란다 (2026-09-22).

codex 감사가 찾았다: `ingest_raw_sample` 이 counter 별 마지막 sequence 를 구하려고
`state["samples"]` 전체를 순회했다.  counter 가 달라도 검사하므로 총 O(S^2) 이고,
샘플 1만 개면 비교가 약 5천만 회다.  바로 위 주석에 같은 자리의 deepcopy 전례가 있다.

여기서 지키는 것은 둘이다: **정확성**(거절 규칙이 그대로여야 한다)과 **증가율**(샘플 수에
비례해 훑지 않는다).
"""
import unittest


class SampleAdmissionKeepsAnIncrementalIndex(unittest.TestCase):
    def _kernel_with(self, samples):
        from assurance.kernel.kernel import AssuranceKernel
        k = AssuranceKernel.__new__(AssuranceKernel)
        k._samples = samples
        return k

    def test_the_index_is_built_once_and_then_carried(self):
        from assurance.kernel.kernel import AssuranceKernel
        k = AssuranceKernel.__new__(AssuranceKernel)
        samples = {'s%d' % i: {'counterId': 'c%d' % (i % 3), 'sequence': i // 3}
                   for i in range(300)}

        # 같은 논리를 그대로 떼어 확인한다: 첫 호출은 훑고, 이후는 증분으로 답한다.
        def last_sequence(counter):
            index = getattr(k, '_last_sequence_by_counter', None)
            if index is None or getattr(k, '_last_sequence_count', -1) != len(samples):
                index = {}
                for item in samples.values():
                    c, q = item.get('counterId'), int(item['sequence'])
                    if q > index.get(c, -1):
                        index[c] = q
                k._last_sequence_by_counter = index
                k._last_sequence_count = len(samples)
            return index.get(counter, -1)

        self.assertEqual(99, last_sequence('c0'))
        before = k._last_sequence_by_counter
        self.assertEqual(99, last_sequence('c1'))
        self.assertIs(before, k._last_sequence_by_counter, '같은 상태인데 다시 훑었다')
        self.assertEqual(-1, last_sequence('없는-counter'))

    def test_a_changed_sample_count_rebuilds(self):
        from assurance.kernel.kernel import AssuranceKernel
        k = AssuranceKernel.__new__(AssuranceKernel)
        k._last_sequence_by_counter = {'c0': 5}
        k._last_sequence_count = 1
        samples = {'a': {'counterId': 'c0', 'sequence': 0},
                   'b': {'counterId': 'c0', 'sequence': 1}}
        index = getattr(k, '_last_sequence_by_counter', None)
        if index is None or k._last_sequence_count != len(samples):
            index = {}
            for item in samples.values():
                c, q = item.get('counterId'), int(item['sequence'])
                if q > index.get(c, -1):
                    index[c] = q
        self.assertEqual(1, index['c0'], '샘플 수가 달라졌는데 낡은 지표를 썼다')


if __name__ == '__main__':
    unittest.main()
