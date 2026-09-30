"""P9: structured observability - `ci stats` JSON-first collection with
fallback to the legacy stdout-log regex path on unpatched gNBs.
"""

import types
import unittest
from unittest import mock

from collectors.gnb_collector import GNBCollector, GNBMetrics

CANNED = ('{"cell":{"dl_prb_cap":24,"mcs_cap":[0,28]},'
          '"ues":[{"rnti":"4601","dl_mcs":24,"ul_mcs":9,"dl_bler":0.00374,'
          '"pf_weight":1.000,"prb_cap":0},'
          '{"rnti":"9b3e","dl_mcs":12,"ul_mcs":7,"dl_bler":0.11000,'
          '"pf_weight":2.000,"prb_cap":12}]}')


def _collector():
    return GNBCollector(gnb_id="gnb1", hostname="203.0.113.1",
                        ssh_user="nobody")


class ParseCiStatsTest(unittest.TestCase):

    def test_parses_canned_json(self):
        data = GNBCollector.parse_ci_stats(CANNED)
        self.assertEqual(data["cell"]["dl_prb_cap"], 24)
        self.assertEqual(data["cell"]["mcs_cap"], [0, 28])
        self.assertEqual(len(data["ues"]), 2)
        self.assertEqual(data["ues"][1]["rnti"], "9b3e")

    def test_banner_and_echo_tolerant(self):
        noisy = ("softmodem_gnb> ci stats\n" + CANNED + "\nsoftmodem_gnb> ")
        data = GNBCollector.parse_ci_stats(noisy)
        self.assertIsNotNone(data)
        self.assertEqual(len(data["ues"]), 2)

    def test_rejects_garbage_and_wrong_shape(self):
        self.assertIsNone(GNBCollector.parse_ci_stats("no json here"))
        self.assertIsNone(GNBCollector.parse_ci_stats("{not json}"))
        self.assertIsNone(GNBCollector.parse_ci_stats('{"foo": 1}'))
        self.assertIsNone(GNBCollector.parse_ci_stats(""))
        self.assertIsNone(GNBCollector.parse_ci_stats(None))
        # right keys, wrong shapes: must fall back, not half-parse
        self.assertIsNone(GNBCollector.parse_ci_stats('{"cell":[],"ues":{}}'))
        self.assertIsNone(GNBCollector.parse_ci_stats('{"cell":{},"ues":{}}'))
        self.assertIsNone(GNBCollector.parse_ci_stats('{"cell":1,"ues":[]}'))


class FillFromCiStatsTest(unittest.TestCase):

    def test_fills_metrics_from_snapshot(self):
        m = GNBMetrics(gnb_id="gnb1")
        GNBCollector._fill_from_ci_stats(m, GNBCollector.parse_ci_stats(CANNED))
        self.assertAlmostEqual(m.dl_mcs_avg, 18.0)     # (24+12)/2
        self.assertAlmostEqual(m.ul_mcs_avg, 8.0)      # (9+7)/2
        self.assertAlmostEqual(m.dl_bler, (0.00374 + 0.11) / 2)
        self.assertEqual(m.ue_rntis, [0x4601, 0x9b3e])
        self.assertEqual(m.connected_ues, 2)

    def test_empty_ue_list_leaves_defaults(self):
        m = GNBMetrics(gnb_id="gnb1")
        GNBCollector._fill_from_ci_stats(
            m, {"cell": {"dl_prb_cap": 0}, "ues": []})
        self.assertEqual(m.dl_mcs_avg, 0.0)
        self.assertEqual(m.ue_rntis, [])


class JsonFirstFallbackTest(unittest.TestCase):

    def test_get_ci_stats_uses_telnet(self):
        c = _collector()
        c._telnet_cmd = types.MethodType(
            lambda self, cmd, timeout=2.0: (True, CANNED), c)
        data = c.get_ci_stats()
        self.assertIsNotNone(data)
        self.assertEqual(len(data["ues"]), 2)

    def test_get_ci_stats_none_when_command_missing(self):
        c = _collector()
        # unpatched gNB: command unknown -> unparseable text
        c._telnet_cmd = types.MethodType(
            lambda self, cmd, timeout=2.0: (True, "unknown command 'stats'"),
            c)
        self.assertIsNone(c.get_ci_stats())
        # or the telnet channel itself fails
        c._telnet_cmd = types.MethodType(
            lambda self, cmd, timeout=2.0: (False, "connection refused"), c)
        self.assertIsNone(c.get_ci_stats())

    def test_collect_prefers_json_and_falls_back(self):
        # patched path: ci stats answers -> log parsing NOT consulted
        c = _collector()
        c.is_running = lambda: True
        c.get_tx_gain = lambda: None
        c.get_rx_gain = lambda: None
        c.get_connected_ues = lambda: (0, [])
        c._telnet_cmd = types.MethodType(
            lambda self, cmd, timeout=2.0: (True, CANNED), c)
        c.parse_gnb_log = lambda *a, **k: self.fail("fallback used")
        m = c.collect()
        self.assertAlmostEqual(m.dl_mcs_avg, 18.0)

        # unpatched path: ci stats unavailable -> legacy log parsing
        c2 = _collector()
        c2.is_running = lambda: True
        c2.get_tx_gain = lambda: None
        c2.get_rx_gain = lambda: None
        c2.get_connected_ues = lambda: (0, [])
        c2._telnet_cmd = types.MethodType(
            lambda self, cmd, timeout=2.0: (False, "refused"), c2)
        c2.parse_gnb_log = lambda *a, **k: {"dl_mcs_avg": 21.0,
                                            "ul_mcs_avg": 5.0,
                                            "dl_bler": 0.02, "ul_bler": 0.0}
        m2 = c2.collect()
        self.assertAlmostEqual(m2.dl_mcs_avg, 21.0)
        self.assertAlmostEqual(m2.dl_bler, 0.02)


class UECollectorCiStatsWiringTest(unittest.TestCase):
    """Gate B [A2]: the structured `ci stats` path is wired into the MAIN
    closed-loop telemetry (UECollector), not just the standalone
    GNBCollector - JSON-first for MCS/BLER, stdout-log parsing kept for
    RSRP/SINR/CQI and as the full fallback."""

    LOG_TAIL = (
        "UE RNTI 4601 CU-UE-ID 1 in-sync PH 52 dB PCMAX 21 dBm, "
        "average RSRP -77 (16 meas)\n"
        "UE 4601: CQI 13, RI 1, PMI (0,0)\n"
        "UE 4601: dlsch_rounds 523/3/0/0, dlsch_errors 0, pucch0_DTX 1, "
        "BLER 0.09000 MCS (1) 20\n"
        "UE 4601: ulsch_rounds 597/0/0/0, ulsch_errors 0, NPRB 5  "
        "SNR 30.0 dB\n")

    def _ue_collector(self):
        from collectors.multi_ue_collector import UECollector
        return UECollector("ue1", "203.0.113.9", "nobody", gnb_id="gnb1")

    def test_apply_ci_stats_selects_row_by_rnti(self):
        from collectors.multi_ue_collector import UECollector, UEMetrics
        stats = GNBCollector.parse_ci_stats(CANNED)
        m = UEMetrics(ue_id="ue1")
        self.assertTrue(UECollector._apply_ci_stats(m, stats, "9b3e"))
        self.assertEqual(m.mcs, 12)
        self.assertAlmostEqual(m.dl_bler, 0.11)
        m2 = UEMetrics(ue_id="ue1")
        self.assertFalse(UECollector._apply_ci_stats(m2, stats, "ffff"))
        self.assertIsNone(m2.mcs)
        m3 = UEMetrics(ue_id="ue1")
        self.assertFalse(UECollector._apply_ci_stats(m3, stats, None))

    def test_log_parse_preserves_json_filled_fields(self):
        # JSON fills MCS/BLER; the subsequent log parse adds RSRP/SINR/CQI
        # WITHOUT overwriting them (parse only fills fields still None)
        from collectors.multi_ue_collector import (
            UECollector, UEMetrics, parse_gnb_mac_stats)
        stats = GNBCollector.parse_ci_stats(CANNED)
        m = UEMetrics(ue_id="ue1")
        self.assertTrue(UECollector._apply_ci_stats(m, stats, "4601"))
        parse_gnb_mac_stats(self.LOG_TAIL, m, pci=0, rnti="4601")
        self.assertEqual(m.mcs, 24)                    # JSON value kept
        self.assertAlmostEqual(m.dl_bler, 0.00374)     # not the log's 0.09
        self.assertAlmostEqual(m.rsrp, -77.0)          # log-only fields fill
        self.assertAlmostEqual(m.sinr, 30.0)
        self.assertEqual(m.cqi, 13)

    def test_partial_json_fills_only_the_missing_field(self):
        # the log's dlsch line carries BOTH bler and mcs: when the JSON
        # snapshot filled only ONE of them, the log must fill the other
        # WITHOUT overwriting the structured value
        from collectors.multi_ue_collector import (
            UECollector, UEMetrics, parse_gnb_mac_stats)
        only_mcs = {"cell": {}, "ues": [{"rnti": "4601", "dl_mcs": 24}]}
        m = UEMetrics(ue_id="ue1")
        self.assertTrue(UECollector._apply_ci_stats(m, only_mcs, "4601"))
        parse_gnb_mac_stats(self.LOG_TAIL, m, pci=0, rnti="4601")
        self.assertEqual(m.mcs, 24)                    # JSON kept
        self.assertAlmostEqual(m.dl_bler, 0.09)        # log filled the gap

        only_bler = {"cell": {}, "ues": [{"rnti": "4601", "dl_bler": 0.5}]}
        m2 = UEMetrics(ue_id="ue1")
        self.assertTrue(UECollector._apply_ci_stats(m2, only_bler, "4601"))
        parse_gnb_mac_stats(self.LOG_TAIL, m2, pci=0, rnti="4601")
        self.assertAlmostEqual(m2.dl_bler, 0.5)        # JSON kept
        self.assertEqual(m2.mcs, 20)                   # log filled the gap

    def test_log_only_path_unchanged_without_json(self):
        from collectors.multi_ue_collector import (
            UEMetrics, parse_gnb_mac_stats)
        m = UEMetrics(ue_id="ue1")
        parse_gnb_mac_stats(self.LOG_TAIL, m, pci=0, rnti="4601")
        self.assertEqual(m.mcs, 20)                    # legacy fallback
        self.assertAlmostEqual(m.dl_bler, 0.09)

    def test_transport_failure_returns_none(self):
        # connection refused (nothing listens on the loopback port) -> None
        from collectors import multi_ue_collector as muc
        c = self._ue_collector()
        with mock.patch.dict(muc.GNB_TELNET, {"gnb1": ("127.0.0.1", 9)}):
            self.assertIsNone(c._gnb_ci_stats("gnb1"))
        self.assertIsNone(c._gnb_ci_stats("no_such_gnb"))


if __name__ == "__main__":
    unittest.main()
