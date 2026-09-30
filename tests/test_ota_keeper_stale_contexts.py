"""컨텍스트는 gNB 에도 코어에도 쌓인다 — 주기적으로 감지해 치워야 한다 (2026-09-22 오너 지시).

**gNB 쪽**: 죽은 UE 컨텍스트가 남아 계속 통계를 낸다.  gNB 는 이걸 스스로 못 끝낸다 --
RLF 콜백이 gNB 쪽에 등록돼 있지 않고(`nr_rlc_set_rlf_handler` 는 UE 쪽만),
UL 실패 경로는 DTX 를 세는데 이 좀비는 들리기는 하므로 DTX 가 안 쌓인다.
21:00 재기동 뒤 **1시간 20분** 만에 다시 쌓였고, 그때 gNB 가 살아 있는 UE 에게
`NPRB 23` 을 주기 시작해 그 UE 의 `ulsch_DTX` 가 199배로 뛰었다.

**코어 쪽**: DNN `oai` = `12.1.1.128/26` 을 ue1·ue2 가 공유하는데 주소가 재사용되지
않고 단조 증가한다.  마르면 UE 가 `Registration Accept` 까지 가고도 PDU 세션만 못 받는다.
"""
import importlib.util
import unittest
from pathlib import Path

OPS = Path(__file__).resolve().parents[1] / 'experiment_results' / 'ota-20260911' / 'ops'


def _load():
    if not (OPS / 'keeper.py').exists():
        raise unittest.SkipTest(f'운영 소스가 없다: {OPS / "keeper.py"}')
    spec = importlib.util.spec_from_file_location('keeper_stale', OPS / 'keeper.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.release_zombie_contexts = lambda *_a, **_k: []   # hermetic: no ssh/telnet to the bed
    return module


def stats(rnti, dtx=10, bler=0.05, nprb=5):
    return (f"UE {rnti}: ulsch_rounds 100/5/2/1, ulsch_errors 1, ulsch_DTX {dtx}, "
            f"BLER {bler:.5f} MCS (0) 20 (Qm 6 deltaMCS 0 dB) NPRB {nprb} SNR 16.1 (+1.1) dB\n")


class TheGnbWatcherCountsOnlyWhatStillReports(unittest.TestCase):
    def setUp(self):
        self.k = _load()

    def test_exactly_the_placed_ues_is_not_stale(self):
        text = ''.join(stats(r) for r in ('aaaa', 'bbbb') for _ in range(5))
        self.assertEqual(0, self.k.stale_rnti_excess(text, 2))

    def test_one_extra_rnti_is_named(self):
        text = ''.join(stats(r) for r in ('aaaa', 'bbbb', 'cccc') for _ in range(5))
        self.assertEqual(1, self.k.stale_rnti_excess(text, 2))

    def test_a_single_ue_cell_with_two_rntis_is_stale(self):
        """gnb1 에는 ue2 하나뿐이다 — 둘이 보이면 하나는 죽은 것이다."""
        text = ''.join(stats(r) for r in ('1111', '2222') for _ in range(4))
        self.assertEqual(1, self.k.stale_rnti_excess(text, 1))

    def test_a_released_rnti_outside_the_tail_is_not_counted(self):
        """해제된 RNTI 도 로그 **전체**에는 남는다 -- 꼬리만 보므로 세지 않는다.

        실측 2026-09-22: 로그 전체 42개, 최근 창 3개.  전체를 세면 항상 초과로 보인다.
        """
        tail = ''.join(stats(r) for r in ('aaaa', 'bbbb') for _ in range(5))
        self.assertEqual(0, self.k.stale_rnti_excess(tail, 2))

    def test_an_empty_tail_never_triggers_a_restart(self):
        self.assertLess(self.k.stale_rnti_excess('', 2), self.k.STALE_RNTI_MARGIN)


class ThePoolWatcherReadsTheHighestUsedAddress(unittest.TestCase):
    def setUp(self):
        self.k = _load()

    def test_a_fresh_allocator_has_room(self):
        self.assertEqual(59, self.k.pool_headroom(["12.1.1.130/24", "12.1.1.131/24"]))  # 190-131

    def test_the_ue3_pool_is_not_counted(self):
        """ue3 는 DNN `default`(12.1.1.192/26)를 쓴다 -- 같은 풀이 아니다."""
        self.assertEqual(59, self.k.pool_headroom(
            ['12.1.1.130/24', '12.1.1.131/24', '12.1.1.195/24']))  # .195 는 세지 않는다

    def test_a_drying_pool_is_named(self):
        """실측: ue1 이 .179 를 쥐었을 때 11개만 남아 ue2 가 못 붙었다."""
        self.assertEqual(11, self.k.pool_headroom(['12.1.1.179/24', '12.1.1.131/24']))
        self.assertLessEqual(11, self.k.POOL_LOW_WATER)

    def test_no_addresses_reads_as_full(self):
        self.assertGreater(self.k.pool_headroom([]), self.k.POOL_LOW_WATER)

    def test_malformed_entries_are_ignored(self):
        self.assertEqual(60, self.k.pool_headroom(['', 'nonsense', '12.1.1.130/24']))  # 190-130


class ADriedPoolIsSeenEvenWhenNoUeHoldsAnAddress(unittest.TestCase):
    """2026-09-23: 풀이 마르면 ue1·ue2 는 tun 이 **없다** -- 주소 목록이 비어 `pool_headroom([])`
    가 "여유 62" 를 준다.  가드가 가장 필요한 순간에 조용했다(ue1 마지막 `.187`, 15분 no-dl).
    본 최댓값을 기억해 두었다가 그걸로 잰다.
    """

    def setUp(self):
        import tempfile
        from pathlib import Path
        self.k = _load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.k.POOL_HIGH_WATER = Path(self.tmp.name) / 'high-water.json'

    def test_the_highest_address_seen_is_remembered(self):
        self.k._remember_pool_high_water(['12.1.1.150/24', '12.1.1.187/24', '12.1.1.211/24'])
        self.assertEqual(['12.1.1.187'], self.k._remembered_pool_high_water(),
                         'ue3 풀(.211)은 세지 않는다')

    def test_an_empty_address_list_still_reads_as_dry_after_one_sighting(self):
        self.k._remember_pool_high_water(['12.1.1.187/24'])
        known = [] + self.k._remembered_pool_high_water()      # 지금은 아무도 주소가 없다
        self.assertLessEqual(self.k.pool_headroom(known), self.k.POOL_LOW_WATER)

    def test_nothing_remembered_means_nothing_known(self):
        self.assertEqual([], self.k._remembered_pool_high_water())


class ADetectedStaleCellIsFixedEvenDuringABoard(unittest.TestCase):
    """판을 지키려던 가드가 판을 쓸모없게 만들면 안 된다 (2026-09-22 23:03).

    좀비가 붙은 gNB 는 같은 HARQ 프로세스에 다른 TBS 를 지시하고, UE 는 그때
    HARQ 버퍼를 버린다(`nr_ue_procedures.c:952`).  그러면 ue3 가 1 Mbps 로 떨어져
    `I3g`·`I3d` 가 모든 T 에서 FAIL 이 되고 **그 판은 이미 쓸 수 없다.**  그래서
    판 도중이어도 연속으로 감지되면 고친다 -- 무한정 미루면 캠페인이 무의미해진다.
    """

    def setUp(self):
        self.k = _load()
        self.k._stale_seen.clear()
        self.restarts = []
        self.running = True
        self.k.episode_running = lambda: self.running
        self.k.episode_in_sitting = lambda: self.running
        self.k.budget = lambda _t: True
        self.k.restart_gnb1 = lambda *a, **kw: self.restarts.append('gnb1')
        self.k.restart_gnb2 = lambda *a, **kw: self.restarts.append('gnb2')
        # 2026-09-23: log 를 막지 않으면 운영 원장 overnight/keeper.jsonl 에 줄을 쓴다.
        self.k.log = lambda *a, **kw: None
        stale = ''.join(stats(r) for r in ('aaaa', 'bbbb', 'cccc') for _ in range(4))
        clean = ''.join(stats(r) for r in ('dddd',) for _ in range(4))
        self.k._gnb2_tail = lambda *a, **kw: stale      # 배치 2, RNTI 3 -> 초과 1
        self.k._gnb1_tail = lambda *a, **kw: clean      # 배치 1, RNTI 1 -> 초과 0

    def test_the_first_detections_only_defer(self):
        for _ in range(self.k.STALE_PATIENCE - 1):
            self.k.ensure_no_stale_contexts()
        self.assertEqual([], self.restarts, '참을성이 남아 있으면 판을 깨지 않는다')

    def test_it_fixes_once_patience_runs_out(self):
        for _ in range(self.k.STALE_PATIENCE):
            self.k.ensure_no_stale_contexts()
        self.assertEqual(['gnb2'], self.restarts)

    def test_a_pre_board_wait_is_patient_too(self):
        """2026-09-24 03:42: the runner held the lock waiting for UEs (not yet in a board);
        the keeper restarted gnb2 on the first sighting of one excess context -- the
        transient left by two UEs that had just re-attached -- and lost both UEs."""
        self.k.episode_running = lambda: True
        self.k.episode_in_sitting = lambda: False
        for _ in range(self.k.STALE_PATIENCE - 1):
            self.k.ensure_no_stale_contexts()
        self.assertEqual([], self.restarts)

    def test_an_idle_bed_is_patient_too(self):
        """2026-09-24 05:34-05:52: with no lock the keeper restarted a gNB on the first sighting
        of the excess a fresh re-attach leaves behind, looping restart -> re-attach -> excess."""
        self.running = False
        self.k.episode_running = lambda: False
        self.k.ensure_no_stale_contexts()
        self.assertEqual([], self.restarts)
        for _ in range(self.k.STALE_PATIENCE - 1):
            self.k.ensure_no_stale_contexts()
        self.assertEqual(['gnb2'], self.restarts, '끈질긴 초과는 판이 없으면 고친다')

    def test_a_cell_that_recovers_resets_its_streak(self):
        clean = ''.join(stats(r) for r in ('aaaa', 'bbbb') for _ in range(4))
        self.k.ensure_no_stale_contexts()
        self.k._gnb2_tail = lambda *a, **kw: clean
        self.k.ensure_no_stale_contexts()
        self.assertNotIn('gnb2', self.k._stale_seen, '깨끗해지면 연속 횟수가 지워진다')

    def test_a_clean_cell_is_never_restarted(self):
        for _ in range(self.k.STALE_PATIENCE + 2):
            self.k.ensure_no_stale_contexts()
        self.assertNotIn('gnb1', self.restarts)


def dl(rnti, first, fourth):
    return (f"UE {rnti}: dlsch_rounds {first}/{first//5}/{first//20}/{fourth}, "
            f"dlsch_errors 3, pucch0_DTX 10 (SNR 21.5+1.5 dB), BLER 0.05 MCS (0) 15 "
            f"CCE fail 100, goodput 5.0 Mbps\n")


class BrokenHarqCombiningIsNamedByRetransmissionDepth(unittest.TestCase):
    """OAI 는 "반쯤 살아 있는" 컨텍스트를 끝내지 못한다 (2026-09-22).

    `nr_ue_procedures.c:952` -- gNB 가 같은 HARQ 프로세스에 다른 TBS 를 지시하면
    UE 가 `new_data_indicator = true` 로 HARQ 버퍼를 버려 결합이 불가능해진다.
    그런데 자동 정리 경로가 둘 다 막혀 있다: RLF 콜백은 gNB 쪽에 미등록이고,
    UL 실패는 `pusch_consecutive_dtx_cnt` 즉 **연속** DTX 를 세는데 이 좀비는 가끔
    PDU 를 보내 카운터를 0 으로 되돌린다(`gNB_scheduler_ulsch.c:1016`).
    실측: gnb2 는 RLF 368 건에 `Remove NR rnti` 가 3 건뿐, UL 실패 0 건.
    누적 DTX 는 1,684 까지 갔는데도 연속 100 을 한 번도 못 채웠다.

    그래서 **재전송 깊이**로 잡는다 -- 같은 셀 같은 순간 정상 0.00086 대 망가진 것
    0.0210 으로 24배 벌어진다.
    """

    def setUp(self):
        self.k = _load()

    # 2026-09-23 감사: 판정은 꼬리 창 안의 **증분**이다 -- 표본 두 개(앞·뒤)를 준다.
    def test_a_healthy_ue_is_not_named(self):
        self.assertEqual([], self.k.broken_harq_ues(dl('aaaa', 100000, 100) + dl('aaaa', 184680, 159)))

    def test_a_broken_ue_is_named(self):
        got = self.k.broken_harq_ues(dl('48f3', 100000, 2100) + dl('48f3', 142039, 2987))
        self.assertEqual(['48f3'], [r for r, _x in got])

    def test_only_the_broken_one_of_a_pair(self):
        text = (dl('2c7a', 100000, 100) + dl('48f3', 100000, 2100)
                + dl('2c7a', 184680, 159) + dl('48f3', 142039, 2987))
        self.assertEqual(['48f3'], [r for r, _x in self.k.broken_harq_ues(text)])

    def test_a_young_context_is_not_judged(self):
        """표본이 적으면 비율이 튄다 -- 판단하지 않는다."""
        self.assertEqual([], self.k.broken_harq_ues(dl('bbbb', 100, 0) + dl('bbbb', 200, 50)))

    def test_the_increment_between_first_and_last_sample_is_judged(self):
        text = dl('cccc', 100000, 1000) + dl('cccc', 142039, 2987) + dl('cccc', 143328, 4000)
        got = dict(self.k.broken_harq_ues(text))
        self.assertGreater(got['cccc'], 0.06)      # (4000-1000)/(143328-100000)

    def test_malformed_lines_are_ignored(self):
        self.assertEqual([], self.k.broken_harq_ues('UE zzzz: dlsch_rounds bad/x\n'))


if __name__ == '__main__':
    unittest.main()
