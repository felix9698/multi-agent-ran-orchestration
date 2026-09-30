"""keeper 감사(2026-09-23) 결함 1~4 -- 판을 끝내던 결함마다 옛 코드에서 실패하는 시험.

전부 hermetic 이다: ssh·run·Popen 은 가짜로 바꾸고, 기록(LOG·예산·상태 파일)은
임시 디렉터리로 돌린다.  주의 -- ``log`` 를 막지 않은 keeper 시험은 **운영 원장
overnight/keeper.jsonl 에 줄을 쓴다** (09-23 21:43:34 의 같은 초 STALE_CONTEXTS_FOUND
네 줄이 시험이 남긴 것이었다).
"""
import importlib.util
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

OPS = Path(__file__).resolve().parents[1] / 'experiment_results' / 'ota-20260911' / 'ops'


def _load(test):
    if not (OPS / 'keeper.py').exists():
        raise unittest.SkipTest('운영 소스가 없다')
    spec = importlib.util.spec_from_file_location('keeper_audit_0923', OPS / 'keeper.py')
    k = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(k)
    k.release_zombie_contexts = lambda *_a, **_k: []   # hermetic: no ssh/telnet to the bed
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    root = Path(tmp.name)
    k.OUT = root
    k.LOG = root / 'keeper.jsonl'
    k.LOCK = root / 'keeper.lock'
    k.READY = root / 'ready.json'
    k.BUDGET_FILE = root / 'budget.json'
    k.KEEPER_STATE = root / 'keeper-state.json'
    k.BUSY = root / 'busy.lock'
    test.logs = []
    k.log = lambda event, **fields: test.logs.append((event, fields))
    k.time = SimpleNamespace(**{n: getattr(time, n) for n in dir(time) if not n.startswith('_')})
    k.time.sleep = lambda _s: None
    return k


def dl(rnti, first, fourth):
    return (f'UE {rnti}: dlsch_rounds {first}/{first // 5}/{first // 20}/{fourth}, '
            f'dlsch_errors 3, BLER 0.05 MCS (0) 15\n')


def ul(rnti):
    return f'UE {rnti}: ulsch_rounds 100/5/2/1, ulsch_errors 1\n'


class HarqDepthIsJudgedOnTheRecentIncrement(unittest.TestCase):
    """결함 1(a): 누적 비율은 한 번 넘으면 영원히 '망가짐' 이다."""

    def setUp(self):
        self.k = _load(self)

    def test_a_healed_ue_with_a_bad_history_is_not_named(self):
        # 누적 8000/100000 = 0.08 (09-23 기록값 대역), 최근 창 증분 10/20000 = 0.0005.
        text = dl('aaaa', 100000, 8000) + dl('aaaa', 120000, 8010)
        self.assertEqual([], self.k.broken_harq_ues(text))

    def test_a_ue_breaking_now_is_named(self):
        text = dl('bbbb', 100000, 100) + dl('bbbb', 120000, 600)     # 500/20000 = 0.025
        self.assertEqual(['bbbb'], [r for r, _x in self.k.broken_harq_ues(text)])

    def test_one_sample_has_no_increment(self):
        self.assertEqual([], self.k.broken_harq_ues(dl('cccc', 142039, 2987)))

    def test_a_counter_reset_is_not_judged(self):
        text = dl('dddd', 900000, 9000) + dl('dddd', 10000, 900)
        self.assertEqual([], self.k.broken_harq_ues(text))


class HarqAloneNeverRestartsAGnbMidBoard(unittest.TestCase):
    """결함 1(b)(c)(d): 09-23 강제 재기동 59회, 진행 중 판 280행 중 92행이 HARQ 만으로."""

    def setUp(self):
        self.k = _load(self)
        self.k._stale_seen.clear()
        self.gnb, self.ues, self.charged = [], [], []
        self.running = True
        self.k.episode_running = lambda: self.running
        self.k.episode_in_sitting = lambda: self.running
        # 이 묶음은 사유 분류를 본다 -- 인내(2026-09-24 부터 늘 적용)는 test_ota_keeper_stale_contexts 가 본다.
        self.k.STALE_PATIENCE = 1
        self.k.budget = lambda t: self.charged.append(t) or True
        self.k.restart_gnb1 = lambda *a, **kw: self.gnb.append(('gnb1', kw.get('reason')))
        self.k.restart_gnb2 = lambda *a, **kw: self.gnb.append(('gnb2', kw.get('reason')))
        self.k.restart_ue = lambda host, evidence=None: self.ues.append((host, evidence))
        self.k._host_of_rnti = lambda rnti: None
        broken = ul('aaaa') + ul('bbbb') + dl('bbbb', 100000, 100) + dl('bbbb', 120000, 600)
        self.k._gnb2_tail = lambda *a, **kw: broken             # 배치 수 그대로, HARQ 만 깨짐
        self.k._gnb1_tail = lambda *a, **kw: ul('cccc')
        self.k.CELL_OF = {'ue1': 'gnb1', 'ue2': 'gnb2', 'ue3': 'gnb2'}

    def test_harq_only_during_a_board_is_deferred_forever(self):
        for _ in range(self.k.STALE_PATIENCE * 3):
            self.k.ensure_no_stale_contexts()
        self.assertEqual([], self.gnb)
        self.assertEqual([], self.ues, '판 도중 UE 재부착도 하지 않는다 (09-20 다섯 판 손실)')
        reasons = {f.get('reason') for e, f in self.logs if e == 'STALE_CONTEXTS_DEFERRED'}
        self.assertEqual({'harq'}, reasons)

    def test_at_the_boundary_a_mapped_ue_is_reattached_not_the_gnb(self):
        self.running = False
        self.k._host_of_rnti = lambda rnti: 'ue3' if rnti == 'bbbb' else None
        self.k.ensure_no_stale_contexts()
        self.assertEqual([('ue3', 'harq-broken')], self.ues)
        self.assertEqual([], self.gnb)

    def test_a_ue_broken_again_soon_after_its_reattach_is_left_and_nothing_restarts(self):
        # 2026-09-24 23:07: ue1 의 새 컨텍스트가 부하 20초 만에 다시 걸렸다 -- 재부착은 churn 뿐.
        self.running = False
        self.k._host_of_rnti = lambda rnti: 'ue3' if rnti == 'bbbb' else None
        for _ in range(3):
            self.k.ensure_no_stale_contexts()
        self.assertEqual([('ue3', 'harq-broken')], self.ues)
        self.assertEqual([], self.gnb)
        self.assertIn('HARQ_BROKEN_PERSISTS_AFTER_REATTACH', [e for e, _f in self.logs])

    def test_at_the_boundary_an_unmapped_rnti_restarts_the_cell_with_the_true_reason(self):
        self.running = False
        self.k.ensure_no_stale_contexts()
        self.assertEqual([('gnb2', 'harq')], self.gnb)
        found = [f for e, f in self.logs if e == 'STALE_CONTEXTS_FOUND']
        self.assertEqual('harq', found[0]['reason'])

    def test_a_real_excess_says_excess(self):
        self.running = False
        self.k._gnb2_tail = lambda *a, **kw: ul('aaaa') + ul('bbbb') + ul('dddd')
        self.k.ensure_no_stale_contexts()
        self.assertEqual([('gnb2', 'excess')], self.gnb)

    def test_the_budget_is_charged_once_by_the_restart_not_here(self):
        self.running = False
        self.k._gnb2_tail = lambda *a, **kw: ul('aaaa') + ul('bbbb') + ul('dddd')
        self.k.ensure_no_stale_contexts()
        self.assertEqual([], self.charged, 'restart_gnb* 가 이미 예산을 쓴다 -- 여기서 또 빼면 이중 차감')

    def test_an_ssh_timeout_decides_nothing(self):
        def boom(*a, **kw):
            raise subprocess.TimeoutExpired('ssh', 40)
        self.k._gnb2_tail = boom
        self.k.ensure_no_stale_contexts()
        self.assertEqual([], self.gnb)


class AFreezeDoesNotOutliveItsEpisode(unittest.TestCase):
    """결함 2: 판 A 의 첫 목격 시각이 남아 판 B 의 첫 목격이 곧바로 TOO_LONG(heldS 6196)."""

    def setUp(self):
        self.k = _load(self)
        self.restarted = []
        self.k.restart_ue = lambda host, evidence=None: self.restarted.append(host)
        self.k.zombie_release_evidence = lambda host: 'bearer-frozen'
        self.k._busy_streak.update({'ue1': 0})
        self.sitting = True
        self.k.episode_in_sitting = lambda: self.sitting

    def _bad(self):
        for _ in range(self.k.FAIL_STREAK):
            self.k.recover_during_episode('ue1', False)

    def test_a_healthy_probe_forgets_the_freeze(self):
        self.k._frozen_since['ue1'] = time.monotonic() - 6196
        self.k.recover_during_episode('ue1', True)
        self._bad()
        self.assertEqual([], self.restarted)

    def test_leaving_the_sitting_forgets_the_freeze(self):
        self.k._frozen_since['ue1'] = time.monotonic() - 6196
        self.sitting = False
        self.k.recover_during_episode('ue1', False)
        self.sitting = True
        self._bad()
        self.assertEqual([], self.restarted)


class OneBadCycleDoesNotKillTheKeeper(unittest.TestCase):
    """결함 3: ric_running() 의 TimeoutExpired 가 keeper 전체를 죽였다."""

    def setUp(self):
        self.k = _load(self)

        def refuse(*a, **kw):
            raise subprocess.TimeoutExpired('docker', 30)
        self.k.run = refuse
        self.k.ssh = refuse
        self.k.signal = SimpleNamespace(signal=lambda *a: None, SIGTERM=15, SIGINT=2)

    def test_a_timeout_in_a_cycle_is_logged_and_the_loop_goes_on(self):
        calls = []

        def cycle():
            calls.append(1)
            if len(calls) == 2:
                self.k._stop = True
            raise subprocess.TimeoutExpired('docker', 30)
        self.k.cycle = cycle
        self.assertEqual(0, self.k.main())
        self.assertEqual(2, len(calls))
        self.assertEqual(2, [e for e, _f in self.logs].count('CYCLE_FAILED'))

    def test_decision_state_survives_a_keeper_restart(self):
        self.k._gnb_restarted_at = 1234.5
        self.k._stale_seen.update({'gnb2': 2})
        self.k._gnb_restart_deferred.update({'gnb1': 1})
        self.k._board_acted_at = 99.0
        self.k._a1p_repin_at = 77.0
        self.k._last_chain_rebuild = 55.0
        self.k._save_state()
        fresh = _load(self)
        fresh.KEEPER_STATE = self.k.KEEPER_STATE
        fresh._load_state()
        self.assertEqual(1234.5, fresh._gnb_restarted_at)
        self.assertEqual({'gnb2': 2}, fresh._stale_seen)
        self.assertEqual({'gnb1': 1}, fresh._gnb_restart_deferred)
        self.assertEqual((99.0, 77.0, 55.0),
                         (fresh._board_acted_at, fresh._a1p_repin_at, fresh._last_chain_rebuild))

    def test_a_corrupt_state_file_is_ignored(self):
        self.k.KEEPER_STATE.write_text('{not json')
        self.k._load_state()
        self.assertEqual(0.0, self.k._gnb_restarted_at)


class TheKeepersUeRestartMatchesItsSiblings(unittest.TestCase):
    """결함 4: rc 무시, authorized 토글 없음, 반송파 되읽기 없음, 낡은 busy."""

    def setUp(self):
        self.k = _load(self)
        self.calls = []
        self.stop_rc = 0
        self.carrier = None
        self.k.ue_auto_restart_held = lambda: False
        self.k.budget = lambda _t: True
        self.k.usb_wedged = lambda host: False
        self.k.usb_reset = lambda host: self.calls.append('usb')
        self.k._ue_password = lambda: 'x'
        self.k.episode_running = lambda: False
        self.k.episode_in_sitting = lambda: False
        k = self.k

        def ssh(host, argv, **kw):
            text = ' '.join(map(str, argv))
            if 'AIC_UE_NO_SCAN=1' in text:
                self.calls.append('start')
                return SimpleNamespace(returncode=0, stdout='', stderr='')
            if 'authorized' in text:
                self.calls.append('authorized')
                return SimpleNamespace(returncode=0, stdout='AUTHORIZED_TOGGLED', stderr='')
            if '/proc/$p/cmdline' in text:
                self.calls.append('readback')
                want = k.CARRIER[k.CELL_OF[host]]
                return SimpleNamespace(returncode=0, stdout=str(self.carrier or want), stderr='')
            self.calls.append('stop')
            return SimpleNamespace(returncode=self.stop_rc, stdout='', stderr='')
        self.k.ssh = ssh

    def test_a_failed_stop_does_not_start(self):
        self.stop_rc = 255
        self.k.restart_ue('ue1')
        self.assertEqual(['stop'], self.calls)
        self.assertIn('UE_RESTART_FAILED', [e for e, _f in self.logs])

    def test_the_carrier_the_process_got_is_read_back(self):
        self.carrier = 1
        self.k.restart_ue('ue1')
        self.assertEqual(['stop', 'start', 'readback'], self.calls)
        self.assertIn('UE_CARRIER_MISMATCH', [e for e, _f in self.logs])

    def test_a_second_restart_this_hour_escalates_to_the_authorized_toggle(self):
        self.k._events['ue:ue1'] = [time.time() - 60, time.time()]
        self.k.restart_ue('ue1', evidence='softmodem-absent')
        self.assertEqual(['stop', 'usb', 'authorized', 'start', 'readback'], self.calls)

    def test_a_lock_taken_since_the_cycle_began_stops_an_unevidenced_restart(self):
        self.k.episode_running = lambda: True
        self.k.episode_in_sitting = lambda: True
        self.k.restart_ue('ue2')
        self.assertEqual([], self.calls)

    def test_the_sweep_rechecks_the_lock(self):
        self.k.episode_running = lambda: True
        self.k.episode_in_sitting = lambda: True
        self.k.sweep_stale_workload()
        self.assertEqual([], self.calls)


class AnUnreadableGnb2IsNotHealthyForever(unittest.TestCase):
    """결함 4: 예외면 False(정상) 였다 -- 형제 ue_zombie.cell_is_down 은 True."""

    def setUp(self):
        self.k = _load(self)
        self.k._gnb2_unreadable = 0

    def test_three_unreadable_cycles_in_a_row_say_stalled(self):
        def boom(*a, **kw):
            raise subprocess.TimeoutExpired('ssh', 30)
        self.k.ssh = boom
        self.assertEqual([False, False, True], [self.k.gnb2_stalled() for _ in range(3)])

    def test_an_ssh_failure_is_not_read_as_an_absent_process(self):
        self.k.ssh = lambda *a, **kw: SimpleNamespace(returncode=255, stdout='', stderr='no route')
        self.assertFalse(self.k.gnb2_stalled())
        self.assertNotIn('GNB2_PROCESS_ABSENT', [e for e, _f in self.logs])

    def test_a_good_read_resets_the_count(self):
        answers = iter([None, None, '1 0', None, None])

        def ssh(*a, **kw):
            got = next(answers)
            if got is None:
                raise subprocess.TimeoutExpired('ssh', 30)
            return SimpleNamespace(returncode=0, stdout=got, stderr='')
        self.k.ssh = ssh
        self.assertEqual([False] * 5, [self.k.gnb2_stalled() for _ in range(5)])


class Gnb1RestartOwesARepinEvenWhenNotReady(unittest.TestCase):
    """결함 4: gnb1 은 ready 일 때만 빚을 적었다 -- gnb2 는 언제나 적는다."""

    def test_a_not_ready_restart_still_records_the_debt(self):
        k = _load(self)
        k.GNB1_LOG_DIR = k.OUT
        k._defer_gnb_restart = lambda *a, **kw: False
        k.budget = lambda _t: True
        k.gnb1_pids = lambda: []
        k.run = lambda *a, **kw: SimpleNamespace(returncode=0, stdout='', stderr='')
        k.subprocess = SimpleNamespace(Popen=lambda *a, **kw: None, STDOUT=-2, DEVNULL=-3)
        k._gnb_restarted_at = 0.0
        k.restart_gnb1()
        self.assertNotEqual(0.0, k._gnb_restarted_at)


if __name__ == '__main__':
    unittest.main()


class AnRlfContextIsReleasedEvenMidBoard(unittest.TestCase):
    """2026-09-25: ue2 d462 sat 13 min with its RLC stopped; the gNB only logged 'RLF detected'."""

    def setUp(self):
        self.k = _load(self)
        self.sent = []
        self.k._cell_runs_checked_release = lambda cell: True
        self.k._cell_telnet = lambda cell, cmd: self.sent.append((cell, cmd)) or 'ok'
        self.k.episode_in_sitting = lambda: True
        rlf = 'UE d462: RLF detected, but no callable RLF handler registered\n'
        self.k._gnb1_tail = lambda *a, **kw: ul('d462') + rlf * 25 + ul('aaaa') + 'UE aaaa: RLF detected\n' * 3
        self.k._gnb2_tail = lambda *a, **kw: ul('bbbb') + 'UE 0000: RLF detected\n' * 30   # 0000 no longer active

    def test_only_the_active_rnti_past_the_threshold_is_released_once(self):
        self.assertEqual(['d462'], self.k.release_rlf_contexts())
        self.assertEqual([('gnb1', 'ci force_ue_release d462')], self.sent)
        self.assertEqual([], self.k.release_rlf_contexts())    # once per RNTI


class LivenessIsJudgedOnRecentStatsBlocks(unittest.TestCase):
    """2026-09-25 05:3x: a handover leftover gone for minutes still counted as live -> gnb1 restarted mid-board."""

    def test_an_rnti_absent_from_the_last_blocks_is_not_live(self):
        k = _load(self)
        old = ''.join('Frame.Slot %d\n' % i + ul('dead') + ul('7f45') for i in range(20))
        new = ''.join('Frame.Slot %d\n' % i + ul('7f45') for i in range(20, 30))
        self.assertEqual({'7f45'}, k.active_rntis(k.recent_stats_blocks(old + new)))

    def test_short_text_is_judged_whole(self):
        k = _load(self)
        self.assertEqual({'aaaa', 'bbbb'}, k.active_rntis(k.recent_stats_blocks(ul('aaaa') + ul('bbbb'))))


class ADesyncedLeftoverIsAZombieEvenWithAHeldRnti(unittest.TestCase):
    """2026-09-25 08:0x: ue1 got 6610 on gnb1 too; gnb2's out-of-sync 6610 was 'held' and never released."""

    def test_desynced_rntis(self):
        k = _load(self)
        t = ('UE RNTI 6610 CU-UE-ID 2 out-of-sync PH 10 dB\n' + 'UE RNTI 3d25 CU-UE-ID 3 in-sync PH 14 dB\n') * 4
        self.assertEqual({'6610'}, k.desynced_rntis(t))

    def test_a_held_but_desynced_rnti_is_released_mid_board(self):
        k = _load(self)
        spec = importlib.util.spec_from_file_location('keeper_desync', OPS / 'keeper.py')
        fresh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fresh)
        k.release_zombie_contexts = fresh.release_zombie_contexts
        fresh.__dict__.update({n: getattr(k, n) for n in ('log', 'time')})
        k = fresh
        sent = []
        k._cell_runs_checked_release = lambda cell: True
        k._ue_held_rntis = lambda: {'ue1': '6610', 'ue3': '3d25'}
        k._retired_rntis = lambda: set()
        k.episode_in_sitting = lambda: True
        k._cell_telnet = lambda cell, cmd: sent.append(cmd) or 'softmodem_gnb> '
        line = 'UE RNTI 6610 CU-UE-ID 2 out-of-sync PH 10 dB\n' * 4 + 'UE RNTI 3d25 CU-UE-ID 3 in-sync\n' * 4
        k._gnb2_tail = lambda *a, **kw: line
        # the case itself: ue1 lives on gnb1 under the same RNTI (2026-09-27: required, since a held
        # RNTI seen only on its own cell is a throttled UE and is spared during a board)
        k._gnb1_tail = lambda *a, **kw: 'UE 6610: ulsch_rounds 10/0\n' * 4
        self.assertEqual(['6610'], k.release_zombie_contexts('gnb2', {'6610', '3d25'}))
        self.assertEqual('ci force_ue_release 6610', sent[0])
