"""커밋 해시는 미커밋 수정을 못 말한다 (2026-09-23 실측).

`blocks18-v46` 의 네 판이 전부 `codeRevision = 0d3d8a6b2…` 를 적었다 -- 그 사이에 판의
거동을 바꾸는 수정이 여럿 들어갔는데도. 경계 시각을 사람이 기억해야 하는 기록은 기록이
아니므로, 판은 소스 내용 해시도 함께 적는다.
"""
import hashlib
import unittest
from pathlib import Path

from tools.liveconsole.agent import _SOURCE_FILES, _SOURCE_ROOTS, _source_digest


class TheSourceDigestSaysWhatTheCommitCannot(unittest.TestCase):
    def test_it_is_stable_and_covers_the_modules_that_decide_a_board(self):
        first = _source_digest()
        self.assertIsInstance(first, str)
        self.assertEqual(64, len(first))
        self.assertEqual(first, _source_digest(), '같은 트리는 같은 해시다')

    def test_it_is_derived_from_content_not_from_mtime(self):
        """mtime 은 checkout·rsync·touch 로 움직이지만 거동은 안 바뀐다."""
        root = Path(__file__).resolve().parents[1]
        expected = hashlib.sha256()
        paths = sorted({*(path for name in _SOURCE_ROOTS
                          for path in (root / name).rglob('*.py')
                          if '__pycache__' not in path.parts),
                        *(root / name for name in _SOURCE_FILES)})
        self.assertTrue(paths, '소스 뿌리가 비면 해시가 아무것도 안 지킨다')
        for path in paths:
            expected.update(str(path.relative_to(root)).encode('utf-8'))
            expected.update(path.read_bytes())
        self.assertEqual(expected.hexdigest(), _source_digest())

    def test_every_root_exists(self):
        root = Path(__file__).resolve().parents[1]
        for name in _SOURCE_ROOTS:
            self.assertTrue((root / name).is_dir(), f'{name} 가 없으면 조용히 안 세어진다')


if __name__ == '__main__':
    unittest.main()


class TheDigestIgnoresOfflineAnalysis(unittest.TestCase):
    def test_offline_metrics_are_not_part_of_a_boards_behaviour(self):
        """지표 코드를 고쳐도 판의 해시는 안 바뀐다 -- 판은 그걸 부르지 않는다."""
        self.assertNotIn('experiments', _SOURCE_ROOTS)
        self.assertIn('experiments/metrics.py', _SOURCE_FILES)
