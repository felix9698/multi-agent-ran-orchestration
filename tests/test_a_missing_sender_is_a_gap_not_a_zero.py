"""우리 부하원이 UE 를 못 맞히면 그것은 0 Mbps 가 아니라 간극이다 (2026-09-22 실측).

판 20260922T075847 의 기준선에서 `dlGoodputMbps@ue3 = 0.00` 이 coverage 1.0 으로 기록됐다.
그런데 같은 창의 ue3 마감 성공률은 0.30 이었고, 직후 재 보니 ue3 는 4989 kbps 를 받고 있었다.
기전: 판 도중 UE 가 재부착하면 tun 이름은 그대로라 신원 검사를 통과하고 수신기 프로세스가
살아 있어 하트비트도 오른다.  바뀌는 것은 주소뿐이고 송신기는 낡은 주소로 쏜다.

여기서 지키는 것: **tun 은 받는데 우리 flow 의 payload 만 0 이면 KPI 를 내지 않는다.**
"""
import unittest


def _observer(rows_first, rows_second):
    from tools.liveconsole.kpi_observer import LiveFlowGoodputObserver as FlowGoodputObserver
    o = FlowGoodputObserver.__new__(FlowGoodputObserver)
    o.hosts = {'ue3': 'ue3'}
    o.previous = {'ue3': rows_first}
    o.failures = []
    o.intervals = {}
    o.stall_since = {}
    o.stall_payload = {}
    o.hol_stalls = []
    o._lines_by_ue = lambda: {'ue3': ['x']}
    o._snapshot = lambda ue, alias, lines=None: rows_second
    o._snapshot_missing = lambda ue: None
    return o


BASE = {'observedAtMs': 1000.0, 'payloadBytes': 5_000_000, 'tunRxBytes': 6_000_000,
        'clockId': 'c', 'interface': 'oaitun_ue1', 'connection': 'x'}


class AMissingSenderIsNotAZero(unittest.TestCase):
    def test_tun_advances_but_flow_does_not_yields_no_kpi(self):
        # 2026-09-27: a same-connection stall up to _HOL_STALL_MAX_MS is TCP head-of-line
        # waiting (see the next class); a sender that never delivers stays a gap past it.
        later = dict(BASE, observedAtMs=21_000.0,
                     payloadBytes=5_000_000,            # 한 바이트도 안 늘었다
                     tunRxBytes=6_000_000 + 5_000_000)  # tun 은 5 MB 받았다
        o = _observer(BASE, later)
        observed = o.sample()
        self.assertNotIn('dlGoodputMbps@ue3', observed,
                         '송신기가 못 맞힌 창을 0 Mbps 로 기록했다')
        self.assertTrue(o.failures, '간극을 실패로 남기지 않았다')
        self.assertIn('cannot', o.failures[0]['error'])

    def test_a_genuine_zero_still_reports_zero(self):
        # tun 도 안 늘었다면 그것은 진짜로 아무것도 안 온 것이다 -- 값은 0 이 맞다.
        later = dict(BASE, observedAtMs=11_000.0,
                     payloadBytes=5_000_000, tunRxBytes=6_000_000)
        o = _observer(BASE, later)
        observed = o.sample()
        self.assertEqual(0.0, observed.get('dlGoodputMbps@ue3'))

    def test_normal_traffic_is_unaffected(self):
        later = dict(BASE, observedAtMs=11_000.0,
                     payloadBytes=5_000_000 + 12_500_000,
                     tunRxBytes=6_000_000 + 13_000_000)
        o = _observer(BASE, later)
        observed = o.sample()
        self.assertAlmostEqual(10.0, observed['dlGoodputMbps@ue3'], places=3)


class HeadOfLineWaitIsZeroThenCatchUp(unittest.TestCase):
    """2026-09-27 v5.2: ue3 under a PRB cap -- tun +250 kB per 0.5 s, payload 0 for 4-8 s,
    then +2.1-2.3 MB at once, same address and connection.  Seven windows went void on it."""

    def test_a_short_stall_counts_as_zero_and_the_catch_up_restores_the_mean(self):
        o = _observer(BASE, None)
        stall = dict(BASE, observedAtMs=5_000.0, tunRxBytes=6_000_000 + 2_000_000)
        o._snapshot = lambda ue, alias, lines=None: stall
        self.assertEqual(0.0, o.sample().get('dlGoodputMbps@ue3'))
        self.assertEqual([], o.failures, 'a stall is data, not a failure (failures are excised)')
        self.assertEqual(1, len(o.hol_stalls))
        jump = dict(BASE, observedAtMs=9_000.0, payloadBytes=5_000_000 + 4_000_000,
                    tunRxBytes=6_000_000 + 4_200_000)
        o._snapshot = lambda ue, alias, lines=None: jump
        self.assertAlmostEqual(8.0, o.sample()['dlGoodputMbps@ue3'], places=3)
        self.assertEqual({}, o.stall_since)

    def test_the_stall_clock_survives_the_gap_it_triggers(self):
        o = _observer(BASE, None)
        for at in (8_000.0, 16_500.0):   # 7 s then 15.5 s since the stall began
            row = dict(BASE, observedAtMs=at, tunRxBytes=6_000_000 + int(at) * 100)
            o._snapshot = lambda ue, alias, lines=None, r=row: r
            o.sample()
        self.assertIn('flow-payload-mismatch', [f.get('kind') for f in o.failures])
        o.previous = {'ue3': dict(BASE, observedAtMs=16_500.0, tunRxBytes=7_650_000)}
        row = dict(BASE, observedAtMs=18_000.0, tunRxBytes=8_000_000)
        o._snapshot = lambda ue, alias, lines=None: row
        self.assertNotIn('dlGoodputMbps@ue3', o.sample(), 'a long stall restarted its clock after the gap')

    def test_delivery_on_the_poll_after_a_gap_ends_the_stall(self):
        # Codex 2026-09-27: 1 s -> 8 s zero -> 16.5 s gap -> 17 s payload +1 MB -> 17.5 s stall
        o = _observer(BASE, None)
        seq = [dict(BASE, observedAtMs=8_000.0, tunRxBytes=6_700_000),
               dict(BASE, observedAtMs=16_500.0, tunRxBytes=7_550_000),
               dict(BASE, observedAtMs=17_000.0, payloadBytes=6_000_000, tunRxBytes=8_600_000),
               dict(BASE, observedAtMs=17_500.0, payloadBytes=6_000_000, tunRxBytes=8_850_000)]
        out = []
        for row in seq:
            o._snapshot = lambda ue, alias, lines=None, r=row: r
            out.append(o.sample())
        self.assertEqual(0.0, out[-1].get('dlGoodputMbps@ue3'), 'a fresh short stall was read as the old long one')


if __name__ == '__main__':
    unittest.main()
