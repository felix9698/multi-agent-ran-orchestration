#!/usr/bin/env python3
"""Batch G (P0-18): scheduler KPI code-path efficacy qualification.

Real emulated SHARED cell (the current provisioned testbed's UE1+UE2 on gNB1 -
the scope is dynamic, not a fixed 3-UE assumption); ONE sched_priority change via
the REAL executor action/readback path; honest VERIFIED / FAILED / UNKNOWN
verdict. Compact counterexamples: real redistribution, no-op, bad readback,
invalid KPI, dynamic shared-cell scope. Emulated -> integration_only /
hardware_unverified (no OTA).
"""
import json
import unittest

from experiments.emulation import build_emulated_coordinator
from experiments.scheduler_qualification import qualify_scheduler_efficacy


def _coord():
    # the emulated default follows the provisioned real UE set: ue1+ue2 both on
    # gnb1 (a 2-UE SHARED cell). Do NOT start() - the metrics thread would consume
    # RNG draws.
    c, _channel = build_emulated_coordinator(seed=2024, tau_trial_s=0.5)
    return c


class SchedulerEfficacyTest(unittest.TestCase):

    def test_real_emulated_redistribution_is_verified(self):
        c = _coord()
        r = qualify_scheduler_efficacy(c, "ue1", 9.0)
        # dynamic scope from topology: ue1+ue2 share gnb1
        self.assertEqual(r["cell"], "gnb1")
        self.assertEqual(r["scoped_ues"], ["ue1", "ue2"])
        # readback-verified real action + correct redistribution direction
        self.assertTrue(r["applied"]["ok"])
        self.assertTrue(r["readback"]["matches_request"])
        self.assertEqual(r["readback"]["weight"], 9.0)
        self.assertEqual(r["verdict"], "VERIFIED")
        self.assertTrue(r["environment_held_constant"])   # honest A/B, same env
        self.assertTrue(r["neutral_baseline_verified"])   # neutral set+read for all
        self.assertGreater(r["delta_mbps"]["ue1"], 0.0)   # boosted UE up
        self.assertLess(r["delta_mbps"]["ue2"], 0.0)      # cell-mate down
        self.assertTrue(r["redistribution_ok"])           # required for VERIFIED
        # entry scheduler state restored + verified in finally
        self.assertTrue(r["scheduler_restore_verified"])
        self.assertIn("entry_weights", r)
        self.assertEqual(
            c.executor.get_sched_priority("gnb1", rnti=c.ue_rnti["ue1"]),
            r["entry_weights"]["ue1"])
        # classification: emulated code-path, never a paper/OTA claim
        self.assertEqual(r["paper_eligibility"], "integration_only")
        self.assertIs(r["excluded_from_paper"], True)
        self.assertIs(r["paper_ready"], False)
        self.assertIs(r["hardware_unverified"], True)
        self.assertEqual(r["kpi_unit"], "Mbps")
        json.dumps(r, allow_nan=False)

    def test_neutral_weight_no_effect_is_failed(self):
        c = _coord()
        r = qualify_scheduler_efficacy(c, "ue1", 1.0)      # neutral == no-op
        self.assertEqual(r["verdict"], "FAILED")
        self.assertEqual(r["status"], "no_effect")

    def test_readback_mismatch_is_failed(self):
        c = _coord()
        # the executor reports a DIFFERENT applied weight than requested
        c.executor.get_sched_priority = lambda gnb, rnti=None: 1.0
        r = qualify_scheduler_efficacy(c, "ue1", 9.0)
        self.assertFalse(r["readback"]["matches_request"])
        self.assertEqual(r["verdict"], "FAILED")
        self.assertEqual(r["status"], "readback_mismatch")

    def test_invalid_kpi_is_unknown(self):
        c = _coord()
        # a scoped UE returns a non-finite (bad) KPI sample
        c.ue_collector.get_throughput_all = lambda duration=2.0: {
            "ue1": float("nan"), "ue2": 5.0}
        r = qualify_scheduler_efficacy(c, "ue1", 9.0)
        self.assertEqual(r["verdict"], "UNKNOWN")
        self.assertEqual(r["status"], "invalid_kpi")

    def test_non_shared_cell_scope_is_unknown(self):
        # a UE ALONE on its cell cannot qualify (no redistribution). Uses the
        # explicit 3-UE OPTION topology (ue3 alone on gnb2) to exercise this - the
        # scope is derived dynamically from the topology, whatever the UE count.
        from experiments.topology import three_ue_shared_topology
        c, _ch = build_emulated_coordinator(seed=2024, tau_trial_s=0.5,
                                            topology=three_ue_shared_topology())
        r = qualify_scheduler_efficacy(c, "ue3", 9.0)      # ue3 alone on gnb2
        self.assertEqual(r["cell"], "gnb2")
        self.assertEqual(r["scoped_ues"], ["ue3"])         # dynamic, single UE
        self.assertEqual(r["verdict"], "UNKNOWN")
        self.assertEqual(r["status"], "not_shared_cell")

    def test_invalid_weight_or_threshold_fails_closed(self):
        c = _coord()
        for w in (0.0, -1.0, float("nan"), float("inf"), True):
            r = qualify_scheduler_efficacy(c, "ue1", w)
            self.assertEqual(r["verdict"], "UNKNOWN")
            self.assertEqual(r["status"], "invalid_input")
            self.assertNotIn("applied", r)                 # no action was taken
        for thr in (0.0, -0.1, float("nan")):
            r = qualify_scheduler_efficacy(c, "ue1", 9.0, effect_threshold_mbps=thr)
            self.assertEqual(r["status"], "invalid_input")

    def test_environment_not_held_is_unknown(self):
        c = _coord()
        c.ue_collector.channel = object()                  # no capture/restore
        r = qualify_scheduler_efficacy(c, "ue1", 9.0)
        self.assertEqual(r["verdict"], "UNKNOWN")
        self.assertEqual(r["status"], "environment_not_held")
        self.assertNotIn("delta_mbps", r)                  # never measured

    def test_wrong_redistribution_is_failed(self):
        c = _coord()
        # boosted UE rises but a contending UE ALSO rises -> not a redistribution
        seq = [{"ue1": 5.0, "ue2": 5.0}, {"ue1": 9.0, "ue2": 6.0}]
        state = {"i": 0}

        def _probe(duration=2.0):
            v = seq[min(state["i"], len(seq) - 1)]
            state["i"] += 1
            return dict(v)
        c.ue_collector.get_throughput_all = _probe
        r = qualify_scheduler_efficacy(c, "ue1", 9.0)
        self.assertGreater(r["delta_mbps"]["ue1"], 0.0)    # boosted UE up
        self.assertGreater(r["delta_mbps"]["ue2"], 0.0)    # but cell-mate ALSO up
        self.assertFalse(r["redistribution_ok"])
        self.assertEqual(r["verdict"], "FAILED")
        self.assertEqual(r["status"], "wrong_redistribution")

    def test_exception_returns_finite_unknown(self):
        c = _coord()
        # a capture/restore/readback error must NOT escape - it is a finite UNKNOWN
        def _boom(*a, **k):
            raise RuntimeError("capture boom")
        c.ue_collector.channel.capture_state = _boom
        r = qualify_scheduler_efficacy(c, "ue1", 9.0)
        self.assertEqual(r["verdict"], "UNKNOWN")
        self.assertEqual(r["status"], "qualification_exception")
        json.dumps(r, allow_nan=False)                     # finite

    def test_unverified_restore_downgrades_verified_to_failed(self):
        c = _coord()
        real_set = c.executor.set_sched_priority
        st = {"after_boost": False}

        def _set(gnb, weight, rnti=None):
            if st["after_boost"]:
                return True                    # restore no-op: leave boosted state
            ok = real_set(gnb, weight, rnti=rnti)
            if weight != 1.0:                  # the boost applied -> next = restore
                st["after_boost"] = True
            return ok
        c.executor.set_sched_priority = _set
        r = qualify_scheduler_efficacy(c, "ue1", 9.0)      # would be VERIFIED
        self.assertFalse(r["scheduler_restore_verified"])
        self.assertEqual(r["verdict"], "FAILED")           # downgraded, never VERIFIED
        self.assertEqual(r["status"], "restore_unverified")


if __name__ == "__main__":
    unittest.main()
