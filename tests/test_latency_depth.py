#!/usr/bin/env python3
"""Batch G P1-3: per-segment latency-depth threading branch matrix.

Each actually-executed coordinator segment (parse / inference / schema-admission /
executor-write / readback / validation / negotiation / rollback / recovery) is
threaded onto its OWN cycle in EpisodeRecord; a skipped segment stays None (never
0/fabricated); a measured 0 is valid; the episode total is one measurement
attributed once; the negotiation timer is exception-safe (retained on timeout);
and each value links to its cycle's id chain. Finite JSON.
"""
import json
import unittest

from experiments.runner import ExperimentRunner
from experiments.metrics import component_latency_breakdown

_SEGMENTS = ("inference_ms", "schema_admission_ms", "executor_write_ms",
             "readback_ms", "validation_ms", "rollback_ms", "recovery_ms",
             "negotiation_ms")


def _cyc(idx=0, **over):
    c = {"cycle": idx, "cycle_id": f"c{idx}", "episode_id": f"ep{idx}",
         "proposal_id": f"prop{idx}", "fsm_step_id": "fsm",
         "proposal_generated": True, "trial_success": True,
         "proposer_id": "A", "model_version": "A",
         "trial_stats": {"tput_before": 8.0, "tput_after": 9.0,
                         "tput_min": 7.0, "tau": 15.0},
         "current_intent": {"id": f"int{idx}", "type": "throughput_goal",
                            "target": {"target_value": 8.0},
                            "scope": {"ue_ids": []}}}
    c.update(over)
    return c


def _result(cycles, **over):
    r = {"success": True, "experiment_run_id": "run", "fsm_step_id": "fsm",
         "terminal_outcome": "commit_original", "terminal_reason": "commit_verified",
         "parse_ms": 3.0, "latency_ms": 90.0, "cycles": cycles,
         "pending_intent": {"id": "int0"}}
    r.update(over)
    return r


def _ep(**cyc_over):
    r = ExperimentRunner(coordinator=None)
    cyc = _cyc(**cyc_over)
    return r._episode_from_cycle("m", 1, "P", _result([cyc]), cyc, 8.0, phase_idx=0)


class LatencyDepthBranchMatrixTest(unittest.TestCase):

    def test_all_segments_success_are_threaded(self):
        seg = {"inference_ms": 5.0, "schema_admission_ms": 2.0,
               "executor_write_ms": 3.0, "readback_ms": 1.0, "validation_ms": 4.0,
               "rollback_ms": 6.0, "recovery_ms": 7.0, "negotiation_ms": 8.0}
        ep = _ep(**seg)
        for k, v in seg.items():
            self.assertEqual(getattr(ep, k), v)
        self.assertEqual(ep.parse_ms, 3.0)             # episode-level, on cycle 0
        self.assertEqual(ep.total_latency_ms, 90.0)    # one real total, cycle 0

    def test_skipped_segments_stay_none_not_zero(self):
        ep = _ep()                                     # no segment ran
        for k in _SEGMENTS:
            self.assertIsNone(getattr(ep, k))

    def test_measured_zero_is_valid(self):
        ep = _ep(executor_write_ms=0.0, readback_ms=0.0)
        self.assertEqual(ep.executor_write_ms, 0.0)    # measured 0, NOT None
        self.assertEqual(ep.readback_ms, 0.0)

    def test_negotiation_timeout_elapsed_retained(self):
        # the exception-safe cycle timer records elapsed even with NO nego_stats
        # (a timed-out/raised negotiation) - threaded verbatim, never lost.
        ep = _ep(negotiation_ms=12.0)                  # no nego_stats present
        self.assertEqual(ep.negotiation_ms, 12.0)

    def test_negotiation_falls_back_to_nego_stats_for_legacy(self):
        ep = _ep(nego_stats={"duration_s": 0.01})      # no cycle timer -> fallback
        self.assertAlmostEqual(ep.negotiation_ms, 10.0)   # 0.01 s -> 10.0 ms

    def test_linkage_each_ms_on_its_own_cycle_and_total_once(self):
        r = ExperimentRunner(coordinator=None)
        c0 = _cyc(0, inference_ms=5.0, rollback_ms=6.0)
        c1 = _cyc(1, inference_ms=9.0, recovery_ms=7.0)
        eps = r._episode_from_result("m", 1, "P", _result([c0, c1]), {}, 8.0)
        self.assertEqual(len(eps), 2)
        self.assertEqual(eps[0].inference_ms, 5.0)
        self.assertEqual(eps[0].evidence_cycle_id, "c0")
        self.assertEqual(eps[0].rollback_ms, 6.0)
        self.assertEqual(eps[1].inference_ms, 9.0)
        self.assertEqual(eps[1].evidence_cycle_id, "c1")
        self.assertEqual(eps[1].recovery_ms, 7.0)
        # the episode total is attributed ONCE (cycle 0), never split/double-counted
        self.assertEqual(eps[0].total_latency_ms, 90.0)
        self.assertIsNone(eps[1].total_latency_ms)

    def test_finite_json_over_component_breakdown(self):
        ep = _ep(inference_ms=5.0, negotiation_ms=8.0)
        cb = component_latency_breakdown([ep])
        json.dumps(cb, allow_nan=False)                # finite, no NaN/Infinity
        self.assertEqual(cb["inference_ms"]["mean_ms"], 5.0)
        self.assertEqual(cb["negotiation_ms"]["mean_ms"], 8.0)


if __name__ == "__main__":
    unittest.main()
