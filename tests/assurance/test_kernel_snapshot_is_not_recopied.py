"""질의마다 커널 상태를 통째로 복사하면 판이 계산에 갇힌다 (2026-09-22).

한 시행을 처리하는 동안 시팅이 CPU 19분 27초를 태웠다.  정체는 `Kernel._state()` 가
호출될 때마다 상태 전체를 deepcopy 하는 것이었고, kernel.py 안에서만 48곳이 그것을 부른다.
스트림은 append-only 이므로 replay 위치가 그대로면 상태도 그대로다 -- 그 사이의 사본은
서로 구분되지 않는다.  여기서 지키는 것은 두 가지다: 위치가 그대로면 다시 복사하지 않고,
공개 경로는 여전히 독립 스냅샷을 준다.
"""
import unittest
from copy import deepcopy


class _Store:
    def __init__(self):
        self.envelopes = []

    def iterate(self, since_position=0):
        return list(self.envelopes[since_position:])


class SnapshotIsSharedUntilTheStreamMoves(unittest.TestCase):
    def _kernel(self):
        from assurance.kernel.kernel import AssuranceKernel as Kernel
        from assurance.kernel.reducer import KernelReducer
        k = Kernel.__new__(Kernel)
        from threading import RLock
        k._event_store = _Store()
        k._reducer = KernelReducer()
        k._replay_lock = RLock()
        k._replay_state = None
        k._replay_position = 0
        return k

    def test_the_same_position_hands_back_the_same_object(self):
        k = self._kernel()
        first, second = k._state(), k._state()
        self.assertIs(first, second, '위치가 그대로인데 다시 복사했다')

    def test_reduced_state_is_still_independent(self):
        k = self._kernel()
        public = k.reduced_state()
        internal = k._state()
        self.assertIsNot(public, internal,
                         'reduced_state 가 내부 스냅샷을 그대로 빌려주면 안 된다')
        self.assertEqual(deepcopy(dict(public)), deepcopy(dict(internal)))

    def test_a_new_event_invalidates_the_cache(self):
        k = self._kernel()
        before = k._state()
        k._replay_position += 1          # 스트림이 움직인 것과 같은 효과
        k._snapshot_cache = (0, before)  # 낡은 위치의 사본만 들고 있다
        after = k._state()
        self.assertIsNot(before, after, '위치가 바뀌었는데 낡은 사본을 줬다')


if __name__ == '__main__':
    unittest.main()
