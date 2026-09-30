"""A block reference is measured on UEs the boards will run on (2026-09-26, block 10): aged UEs
are re-attached first, by the maintenance daemon's own test and procedure."""
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

PATH = Path(__file__).resolve().parents[1] / "experiment_results/ota-20260911/ops/reference_dl.py"


def _mod():
    spec = importlib.util.spec_from_file_location("reference_dl_fresh_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class RefreshAgedUes(unittest.TestCase):
    def _run(self, stale, reattach_fails=False, dead=(), wrong_cell=()):
        mod = _mod()
        calls = []

        def sh(cmd, timeout=30):
            calls.append(cmd)
            if "ue_ageing_check" in cmd:
                return stale
            if "read_placement" in cmd:
                return "ue1=gnb2 ue2=gnb1 ue3=gnb1\n"
            if "force_reattach" in cmd and reattach_fails:
                raise RuntimeError("ssh died")
            if cmd.endswith("echo ok"):
                return "ok"
            if "oaitun_ue1" in cmd:
                return "" if cmd.split()[1] in dead else "12.1.1.130"
            return ""

        with mock.patch.object(mod, "sh", side_effect=sh), mock.patch.object(mod.time, "sleep"), \
                mock.patch.object(mod, "misplaced",
                                  return_value=[(h, "gnb1", "") for h in wrong_cell]):
            try:
                out = mod.refresh_aged_ues()
            except RuntimeError:
                out = "raised"
        return out, calls

    def test_nothing_aged_touches_nothing(self):
        out, calls = self._run("")
        self.assertEqual([], out)
        self.assertFalse([c for c in calls if "keeper" in c or "force_reattach" in c])

    def test_only_the_aged_ue_is_reattached_at_its_placement(self):
        out, calls = self._run("ue2\n")
        self.assertEqual(["ue2=gnb1"], out)
        self.assertTrue(any("force_reattach.py ue2=gnb1" in c for c in calls))

    def test_a_dead_ue_is_reattached_even_when_nothing_is_aged(self):
        # 2026-09-29 08:47: ue1 lost its address; the reference stopped the keeper and left it dead.
        out, calls = self._run("", dead=("ue1",))
        self.assertEqual(["ue1=gnb2"], out)
        self.assertTrue(any("force_reattach.py ue1=gnb2" in c for c in calls))

    def test_a_ue_left_on_the_other_cell_is_reattached_at_its_placement(self):
        # 2026-09-30 13:48: a failed recovery left ue3 on gnb2; the preflight refused every retry.
        out, calls = self._run("", wrong_cell=("ue3",))
        self.assertEqual(["ue3=gnb1"], out)
        self.assertTrue(any("force_reattach.py ue3=gnb1" in c for c in calls))

    def test_the_keeper_comes_back_even_if_the_reattach_fails(self):
        out, calls = self._run("ue2\n", reattach_fails=True)
        self.assertEqual("raised", out)
        self.assertTrue(any("systemctl --user start aic-keeper" in c for c in calls[-2:]))


class WeakOnPreviousBlock(unittest.TestCase):
    def test_a_ue_below_the_load_in_the_previous_block_is_named(self):
        import json, os, tempfile, time
        mod = _mod()
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            ref = {"loadMbps": 10.0, "raw": {"A": {"ue1": [9.9] * 5, "ue3": [4.5] * 5},
                                              "C": {"ue1": [9.9] * 5, "ue3": [6.7] * 5}}}
            (d / "c.block1.reference.json").write_text(json.dumps(ref))
            old = d / "c.block0.reference.json"
            old.write_text(json.dumps({"loadMbps": 10.0, "raw": {"A": {"ue1": [1.0] * 5}}}))
            os.utime(old, (time.time() - 99, time.time() - 99))
            (d / "c.block3.reference.json").write_text(json.dumps({"loadMbps": 10.0, "raw": {"A": {"ue2": [1.0] * 5}}}))
            self.assertEqual(["ue3"], mod.weak_on_previous_block(d / "c.block2.reference.json"))
            self.assertEqual(["ue1"], mod.weak_on_previous_block(d / "c.block1.reference.json"))
            self.assertEqual([], mod.weak_on_previous_block(d / "x.block0.reference.json"))


class WeakHosts(unittest.TestCase):
    def test_best_configuration_under_85_percent_is_weak(self):
        raw = {"A": {"ue2": [9.9] * 5, "ue3": [4.0] * 5}, "C": {"ue3": [7.25] * 5, "ue2": [0.2] * 5}}
        self.assertEqual(["ue3"], _mod().weak_hosts(raw, 10.0))


class DeadlineStarved(unittest.TestCase):
    def test_ue2_short_of_its_load_in_a_names_both_gnb1_ues(self):
        mod = _mod()
        self.assertEqual(["ue2", "ue3"], mod.deadline_starved({"A": {"ue2": [5.7] * 5}}, 10.0))
        self.assertEqual([], mod.deadline_starved({"A": {"ue2": [9.88] * 5, "ue3": [4.5] * 5}}, 10.0))
        self.assertEqual([], mod.deadline_starved({}, 10.0))


class FixedDeadline(unittest.TestCase):
    def test_v52_fixes_d_at_35_ms_and_an_explicit_value_wins(self):
        import os
        mod = _mod()
        with mock.patch.dict(os.environ, {"AIC_V52": "1", "AIC_FIXED_DEADLINE_MS": ""}):
            self.assertEqual(35.0, mod.fixed_deadline_ms())
        with mock.patch.dict(os.environ, {"AIC_V52": "", "AIC_FIXED_DEADLINE_MS": "40"}):
            self.assertEqual(40.0, mod.fixed_deadline_ms())
        with mock.patch.dict(os.environ, {"AIC_V52": "", "AIC_FIXED_DEADLINE_MS": ""}):
            self.assertIsNone(mod.fixed_deadline_ms())


if __name__ == "__main__":
    unittest.main()


class AUeThatMeasuredNothingIsNamed(unittest.TestCase):
    """2026-09-28 00:5x: ue1 lost its gnb2 context and gave no window; nothing named it, so no
    attempt reattached it and block 6 failed on it again and again."""

    def test_the_solo_reference_is_never_below_the_shared_one(self):
        """v54t block 1: ue2 5.93 in A, 0.26 alone in B -> the reference is 5.93, not 0.26."""
        mod = _mod()
        raw = {'A': {'ue2': [5.93] * 5, 'ue3': [5.9] * 5}, 'B': {'ue2': [0.26] * 5}, 'C': {'ue3': [6.2] * 5}}
        self.assertEqual(5.93, mod._solo(raw, 'B', 'ue2'))
        self.assertEqual(6.2, mod._solo(raw, 'C', 'ue3'))
        self.assertIsNone(mod._solo({'A': {'ue2': [5.9] * 5}}, 'B', 'ue2'))

    def test_a_solo_config_below_the_shared_one_is_unstable(self):
        """v54r block 1: ue2 alone (B) 2.38 Mbps, with ue3 loaded (A) 5.38 -- a collapsed sender."""
        mod = _mod()
        load = mod.LOAD
        raw = {'A': {'ue1': [load] * 5, 'ue2': [5.4] * 5, 'ue3': [5.4] * 5},
               'B': {'ue1': [load] * 5, 'ue2': [2.4] * 5, 'ue3': [0.2] * 5},
               'C': {'ue1': [load] * 5, 'ue2': [0.2] * 5, 'ue3': [5.9] * 5}}
        self.assertIn('solo-below-shared:ue2', mod._unstable(raw))
        raw['B']['ue2'] = [5.9] * 5
        self.assertNotIn('solo-below-shared:ue2', mod._unstable(raw))

    def test_empty_loaded_windows_are_unstable(self):
        mod = _mod()
        raw = {c: {h: ([] if h == 'ue1' else [9.0] * 5) for h in ('ue1', 'ue2', 'ue3')}
               for c in mod.CONFIGS}
        self.assertTrue(any(u.endswith(':ue1') for u in mod._unstable(raw)))
