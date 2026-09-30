"""keeper 가 역할의 번호 변화를 조종 A1-P 의 후계 지도에 적는다 (2026-09-23).

조종 producer 는 정책 본문의 조립 시점 번호로만 되읽어, 핸드오버 중 죽고 새 번호로
재등록한 UE 를 영영 못 봤다(v46r8 판 462: RECOVERY_PENDING 13분, PUT/DELETE 409).
"""
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

OPS = (pathlib.Path(__file__).resolve().parents[1]
       / 'experiment_results' / 'ota-20260911' / 'ops')


def _keeper():
    sys.path.insert(0, str(OPS))
    try:
        spec = importlib.util.spec_from_file_location('ota_keeper_rebind', OPS / 'keeper.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(OPS))


class TheRebindMapRecordsOnlyRealChanges(unittest.TestCase):

    def setUp(self):
        self.k = _keeper()
        self.k.UE_REBIND_MAP = pathlib.Path(tempfile.mkdtemp()) / 'ue-rebinds.json'
        self.k.log = lambda *a, **kw: None

    def entries(self):
        return json.loads(self.k.UE_REBIND_MAP.read_text())['rebinds']

    def test_a_new_id_is_appended_and_chains(self):
        self.k._record_rebinds({'ue2': (640, 647)})
        self.k._record_rebinds({'ue2': (647, 660)})
        self.assertEqual([(e['role'], e['from'], e['to']) for e in self.entries()],
                         [('ue2', 640, 647), ('ue2', 647, 660)])

    def test_an_unchanged_or_unknown_id_is_not_a_rebind(self):
        self.k._record_rebinds({'ue1': (645, 645), 'ue3': (None, 650)})
        self.assertFalse(self.k.UE_REBIND_MAP.exists())

    def test_the_map_is_bounded(self):
        for n in range(self.k.UE_REBIND_KEEP + 5):
            self.k._record_rebinds({'ue1': (n, n + 1)})
        self.assertEqual(len(self.entries()), self.k.UE_REBIND_KEEP)


if __name__ == '__main__':
    unittest.main()
