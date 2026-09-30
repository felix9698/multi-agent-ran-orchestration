"""v5 UE2 deadline d (owner 2026-09-26, revised 05:4x from block 15): the time by which 75 % of
ALL echo requests sent in config A completed; a missing reply or one past the declared timeout stays in the
denominator; if 75 % never complete, d is invalid (never widened).  A's success at d is
recorded as the start state, not used as a gate."""
import importlib.util
import json
import unittest
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "experiment_results/ota-20260911/ops/reference_dl.py"


def _mod():
    spec = importlib.util.spec_from_file_location("reference_dl_v5_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _log(rtts, settle_ms=15000, period_ms=200):
    """issued/reply lines: one request every ``period_ms``; the first ``settle_ms`` are warm-up."""
    lines, t = [], 0.0
    warm = int(settle_ms / period_ms)
    for seq, rtt in enumerate([10.0] * warm + list(rtts)):
        lines.append(json.dumps({"event": "issued", "seq": seq, "issuedAtMs": t}))
        if rtt is not None:
            lines.append(json.dumps({"event": "reply", "seq": seq, "rttMs": rtt}))
        t += period_ms
    return lines


class Deadline(unittest.TestCase):
    def test_only_requests_after_the_settle_are_counted(self):
        mod = _mod()
        rtts = mod.echo_rtts(_log([100.0] * 5), span_s=1.0)
        self.assertEqual([100.0] * 5, rtts)

    def test_a_source_that_stopped_early_is_no_measurement(self):
        mod = _mod()
        # Codex review 2026-09-26: five requests in a 50 s span at 5 Hz is not a measurement.
        self.assertEqual([], mod.echo_rtts(_log([100.0] * 5)))

    def test_d_is_the_75th_percentile_over_all_requests(self):
        mod = _mod()
        rtts = [float(r) for r in range(10, 110, 10)]          # 10 requests, all answered
        self.assertEqual(80.0, mod.echo_deadline_ms(rtts))     # ceil(7.5) = 8th fastest
        # 250 requests: the 188th fastest (owner's worked example).
        self.assertEqual(188.0, mod.echo_deadline_ms([float(r) for r in range(1, 251)]))

    def test_d_comes_from_config_a(self):
        src = PATH.read_text()
        self.assertIn("d = echo_deadline_ms(echoes.get('A'))", src)
        self.assertEqual('A-p75', _mod().DEADLINE_RULE)

    def test_missing_replies_stay_in_the_denominator(self):
        mod = _mod()
        rtts = [50.0] * 7 + [None] * 3                            # only 70 % answered
        self.assertIsNone(mod.echo_deadline_ms(rtts))
        self.assertEqual(50.0, mod.echo_deadline_ms([50.0] * 8 + [None, None]))

    def test_replies_past_the_timeout_are_incomplete(self):
        mod = _mod()
        late = [mod.ECHO_TIMEOUT_MS + 1.0] * 3
        self.assertIsNone(mod.echo_deadline_ms([50.0] * 7 + late))

    def test_success_at_d_counts_misses(self):
        mod = _mod()
        self.assertEqual(0.5, mod.echo_success([100.0, 300.0, None, 90.0], 100.0))
        self.assertIsNone(mod.echo_success([100.0], None))

    def test_the_timeout_is_one_named_constant(self):
        src = PATH.read_text()
        self.assertIn("'echoTimeoutMs': ECHO_TIMEOUT_MS", src)
        self.assertNotIn("power_sweep", src)


if __name__ == "__main__":
    unittest.main()
