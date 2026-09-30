"""P7: verified actuation - write-then-read-back on every apply, and a full
device audit after every restore ("state equivalence verified on every
observed rollback").
"""

import hashlib
import tempfile
import types
import unittest

from experiments.emulation import (
    ChannelModel, SimCollector, SimExecutor, SimTelnetGNB,
    build_emulated_coordinator,
)


class _StockGNB(SimTelnetGNB):
    """A gNB WITHOUT the scheduler patches: only rfatt/reestab exist."""

    def exec_cmd(self, cmd):
        parts = cmd.split()
        if len(parts) > 1 and parts[1] in ("prbcap", "sched_prio", "mcs"):
            return f"unknown ci subcommand '{parts[1]}'"
        return super().exec_cmd(cmd)


def _make_executor(**gnb1_kw):
    sims = {"gnb1": SimTelnetGNB(pci=0, **gnb1_kw),
            "gnb2": SimTelnetGNB(pci=1)}
    sims["gnb1"].add_ue(0x4601)
    sims["gnb2"].add_ue(0x4602)
    return SimExecutor(sims), sims


class VerifyAxisTest(unittest.TestCase):

    def test_normal_round_trip_verifies(self):
        # Acceptance (a): write -> read-back matches on every axis
        ex, _ = _make_executor()
        self.assertTrue(ex.apply_axis("gnb1", "power_offset", 3.0))
        self.assertTrue(ex.apply_axis("gnb1", "prb", 40))
        self.assertTrue(ex.apply_axis("gnb1", "mcs_offset", -6))
        self.assertTrue(ex.apply_axis("gnb1", "sched_priority", 2.0))
        self.assertTrue(ex.apply_axis("gnb1", "prb", 20, rnti=0x4601))
        self.assertTrue(ex.apply_axis("gnb1", "sched_priority", 0.5,
                                      rnti=0x4601))
        # direct verify against the mirror
        self.assertTrue(ex.verify_axis("gnb1", "power_offset", 3.0))
        self.assertTrue(ex.verify_axis("gnb1", "prb", 20, rnti=0x4601))

    def test_silent_device_failure_fails_apply(self):
        # Acceptance (b): gNB ACKs but does not apply -> verify catches it
        ex, sims = _make_executor()
        sims["gnb1"].silent_fail_axes.add("rfatt")
        self.assertFalse(ex.apply_axis("gnb1", "power_offset", 3.0))
        sims["gnb1"].silent_fail_axes = {"prbcap"}
        self.assertFalse(ex.apply_axis("gnb1", "prb", 40))
        self.assertFalse(ex.apply_axis("gnb1", "prb", 24, rnti=0x4601))

    def test_verify_detects_mirror_drift(self):
        # device changed behind the executor's back
        ex, sims = _make_executor()
        self.assertTrue(ex.apply_axis("gnb1", "power_offset", 3.0))
        sims["gnb1"].att_db = 12.0   # drift: device back at neutral
        self.assertFalse(ex.verify_axis("gnb1", "power_offset", 3.0))

    def test_verify_false_on_unknown_gnb(self):
        ex, _ = _make_executor()
        self.assertFalse(ex.verify_axis("nope", "power_offset", 0.0))

    def test_apply_verify_can_be_disabled(self):
        ex, sims = _make_executor()
        sims["gnb1"].silent_fail_axes.add("rfatt")
        # verify=False restores the legacy trust-the-ACK behavior
        self.assertTrue(ex.apply_axis("gnb1", "power_offset", 3.0,
                                      verify=False))


class RestoreAuditTest(unittest.TestCase):

    def test_full_match_report(self):
        ex, _ = _make_executor()
        snap = ex.snapshot()
        ex.apply_axis("gnb1", "power_offset", 5.0)
        ex.apply_axis("gnb1", "prb", 30)
        ex.apply_axis("gnb1", "prb", 12, rnti=0x4601)
        self.assertTrue(ex.restore(snap))
        report = ex.last_restore_report
        self.assertIn("gnb1", report)
        for key, (target, readback, ok) in report["gnb1"].items():
            self.assertTrue(ok, f"{key}: {target} vs {readback}")
        # per-UE entry present in the audit
        self.assertIn("prb@4601", report["gnb1"])

    def test_audit_detects_mismatch(self):
        # Acceptance (c): a silently-failing axis makes the audit fail
        ex, sims = _make_executor()
        snap = ex.snapshot()
        ex.apply_axis("gnb1", "power_offset", 5.0)
        sims["gnb1"].silent_fail_axes.add("rfatt")   # restore write is lost
        self.assertFalse(ex.restore(snap))
        target, readback, ok = ex.last_restore_report["gnb1"]["power_offset"]
        self.assertFalse(ok)
        self.assertAlmostEqual(target, 0.0)
        self.assertAlmostEqual(readback, 5.0)   # device still at trial value

    def test_audit_covers_only_touched_axes(self):
        # only the axes the rollback re-applied are audited (the state the
        # trial moved); untouched axes are absent from the report
        ex, _ = _make_executor()
        snap = ex.snapshot()
        ex.apply_axis("gnb1", "power_offset", 5.0)
        self.assertTrue(ex.restore(snap))
        self.assertEqual(set(ex.last_restore_report["gnb1"]),
                         {"power_offset"})

    def test_cell_sched_skipped_when_per_ue_overrides_exist(self):
        # the device only has per-UE weights: with overrides in play the
        # cell-level mirror is fictional and must not fail the audit
        ex, _ = _make_executor()
        ex.apply_axis("gnb1", "sched_priority", 2.0, rnti=0x4601)
        snap = ex.snapshot()
        audit_ok = ex._audit_restore(snap, {"gnb1": {"sched_priority",
                                                     "sched_priority@4601"}})
        self.assertTrue(audit_ok)
        target, readback, ok = ex.last_restore_report["gnb1"]["sched_priority"]
        self.assertTrue(ok)
        self.assertIsNone(readback)   # marked unobservable, not compared
        # ... while the REAL per-UE weight was audited
        self.assertTrue(ex.last_restore_report["gnb1"]
                        ["sched_priority@4601"][2])

    def test_stock_gnb_power_only_rollback_still_verifies(self):
        # live-path safety: a gNB WITHOUT the scheduler patches only speaks
        # rfatt; a power-only rollback must audit clean (True), never freeze
        sims = {"gnb1": _StockGNB(pci=0), "gnb2": _StockGNB(pci=1)}
        ex = SimExecutor(sims)
        snap = ex.snapshot()
        self.assertTrue(ex.apply_axis("gnb1", "power_offset", 4.0))
        self.assertTrue(ex.restore(snap))
        self.assertEqual(set(ex.last_restore_report["gnb1"]),
                         {"power_offset"})

    def test_unknown_gnb_in_snapshot_fails_audit(self):
        ex, _ = _make_executor()
        self.assertFalse(ex.restore({"ghost": {"power_offset_db": 0.0}}))


class DeviceSnapshotTest(unittest.TestCase):
    """Gate A [C6]: the pre-trial snapshot must be the DEVICE state, not the
    Python mirror's belief - a stale mirror used to poison the snapshot and
    the 'verified' restore then faithfully restored the wrong state."""

    def test_report_reproduction_stale_mirror(self):
        # Verification-report scenario: device at +5 dB offset (att 7.0),
        # mirror still believes 0 dB. Pre-fix: snapshot 0 dB -> restore to
        # 0 dB -> audit "success" while the true pre-state 5 dB was lost.
        ex, sims = _make_executor()
        sims["gnb1"].att_db = 7.0            # device: offset +5 dB
        self.assertAlmostEqual(ex.states["gnb1"].power_offset_db, 0.0)
        snap = ex.snapshot()
        self.assertAlmostEqual(snap["gnb1"]["power_offset_db"], 5.0)
        self.assertEqual(snap["gnb1"]["snapshot_source"]["power_offset"],
                         "device")
        # mirror synchronized to the device
        self.assertAlmostEqual(ex.states["gnb1"].power_offset_db, 5.0)
        # trial + rollback: the TRUE pre-state comes back
        self.assertTrue(ex.apply_axis("gnb1", "power_offset", 2.0))
        self.assertTrue(ex.restore(snap))
        self.assertAlmostEqual(sims["gnb1"].att_db, 7.0)   # +5 dB restored

    def test_per_ue_device_drift_captured(self):
        # a per-UE override existing only on the device is snapshotted and
        # survives a trial that overwrites it
        ex, sims = _make_executor()
        sims["gnb1"].ues[0x4601]["pf_weight"] = 2.0     # device-only state
        snap = ex.snapshot()
        self.assertEqual(snap["gnb1"]["ue_sched_priority"], {0x4601: 2.0})
        self.assertEqual(snap["gnb1"]["snapshot_source"]["ue_sched"],
                         "device")
        self.assertTrue(ex.apply_axis("gnb1", "sched_priority", 0.5,
                                      rnti=0x4601))
        self.assertTrue(ex.restore(snap))
        self.assertAlmostEqual(sims["gnb1"].ues[0x4601]["pf_weight"], 2.0)

    def test_neutral_per_ue_device_state_clears_stale_mirror(self):
        # mirror believes an override exists; the device says neutral ->
        # the snapshot must record the DEVICE truth (no override)
        ex, sims = _make_executor()
        ex.states["gnb1"].ue_prb_cap[0x4601] = 30     # stale mirror entry
        snap = ex.snapshot()
        self.assertEqual(snap["gnb1"]["ue_prb_cap"], {})
        self.assertEqual(ex.states["gnb1"].ue_prb_cap, {})

    def test_unreadable_axes_fall_back_to_mirror(self):
        # stock gNB: only rfatt exists -> power comes from the device, the
        # scheduler axes keep the mirror (provenance says so)
        sims = {"gnb1": _StockGNB(pci=0), "gnb2": _StockGNB(pci=1)}
        ex = SimExecutor(sims)
        snap = ex.snapshot()
        src = snap["gnb1"]["snapshot_source"]
        self.assertEqual(src["power_offset"], "device")
        self.assertEqual(src["prb"], "mirror")
        self.assertEqual(src["mcs_offset"], "mirror")
        self.assertEqual(src["ue_prb"], "mirror")
        self.assertEqual(src["ue_sched"], "mirror")

    def test_partial_per_ue_read_failure_is_independent(self):
        # sched_prio unreadable but prbcap fine: the valid device PRB state
        # must still be snapshotted (per-axis independence), only the
        # PF-weight map falls back to the mirror
        class _NoSchedGNB(SimTelnetGNB):
            def exec_cmd(self, cmd):
                parts = cmd.split()
                if len(parts) > 1 and parts[1] == "sched_prio":
                    return "unknown ci subcommand 'sched_prio'"
                return super().exec_cmd(cmd)

        sims = {"gnb1": _NoSchedGNB(pci=0), "gnb2": _NoSchedGNB(pci=1)}
        sims["gnb1"].add_ue(0x4601)
        sims["gnb1"].ues[0x4601]["prb_cap"] = 30      # device-only PRB state
        ex = SimExecutor(sims)
        snap = ex.snapshot()
        src = snap["gnb1"]["snapshot_source"]
        self.assertEqual(src["ue_prb"], "device")
        self.assertEqual(src["ue_sched"], "mirror")
        self.assertEqual(snap["gnb1"]["ue_prb_cap"], {0x4601: 30})

    def test_zero_ue_empty_sched_output_is_valid(self):
        # the REAL patch prints one row per UE and nothing else: with zero
        # UEs the argless sched_prio output is legitimately EMPTY - that is
        # a valid "no overrides" reading, not a failure
        class _EmptyOutputGNB(SimTelnetGNB):
            def _cmd_sched_prio(self, args):
                if not args:
                    return ""      # patch-faithful zero-UE output
                return super()._cmd_sched_prio(args)

        sims = {"gnb1": _EmptyOutputGNB(pci=0), "gnb2": _EmptyOutputGNB(pci=1)}
        ex = SimExecutor(sims)
        snap = ex.snapshot()
        self.assertEqual(snap["gnb1"]["snapshot_source"]["ue_sched"],
                         "device")
        self.assertEqual(snap["gnb1"]["ue_sched_priority"], {})

    def test_zero_ue_simulator_text_is_valid(self):
        # the simulator prints "no UEs connected" for zero UEs - also valid
        ex, sims = _make_executor()
        sims["gnb1"].detach_ue(0x4601)
        snap = ex.snapshot()
        self.assertEqual(snap["gnb1"]["snapshot_source"]["ue_sched"],
                         "device")
        self.assertEqual(snap["gnb1"]["ue_sched_priority"], {})

    def test_transport_strips_prompt_from_empty_output(self):
        # the REAL telnet transport keeps reading until the OAI prompt; an
        # empty command output must come back as "" - not as the prompt
        # text, which would misclassify the valid zero-UE reading
        from executor.oai_executor import OAIExecutor
        clean = OAIExecutor._clean_response
        self.assertEqual(clean("ci sched_prio",
                               "ci sched_prio\r\nsoftmodem_gnb> "), "")
        self.assertEqual(
            clean("ci sched_prio",
                  "ci sched_prio\r\nUE 4601 PF weight 2.000\r\n"
                  "softmodem_gnb> "),
            "UE 4601 PF weight 2.000")
        self.assertEqual(clean("ci rfatt",
                               "ci rfatt\r\ncurrent TX attenuation 12.0 dB\r\n"
                               "softmodem_gnb> "),
                         "current TX attenuation 12.0 dB")
        # legitimate output lines ending in '>' (usage text) must survive -
        # only the single-token trailing prompt is framing
        self.assertEqual(
            clean("ci prbcap 300 zz",
                  "ci prbcap 300 zz\r\nusage: ci prbcap <n> <rnti>\r\n"
                  "softmodem_gnb> "),
            "usage: ci prbcap <n> <rnti>")

    def test_from_device_false_keeps_legacy_mirror_snapshot(self):
        ex, sims = _make_executor()
        sims["gnb1"].att_db = 7.0            # device drifted
        snap = ex.snapshot(from_device=False)
        self.assertAlmostEqual(snap["gnb1"]["power_offset_db"], 0.0)
        self.assertTrue(all(v == "mirror" for v in
                            snap["gnb1"]["snapshot_source"].values()))

    def test_restore_ignores_provenance_key(self):
        # snapshot_source must never be interpreted as an axis by restore
        ex, _ = _make_executor()
        snap = ex.snapshot()
        ex.apply_axis("gnb1", "power_offset", 3.0)
        self.assertTrue(ex.restore(snap))


class UnqualifiedAxisTest(unittest.TestCase):
    """Gate A follow-up (Codex acceptance #4): an axis whose pre-trial
    snapshot could not be read from the DEVICE (snapshot_source=mirror)
    has no provable pre-state - trials touching it must fail closed
    instead of proceeding on an unverifiable rollback target."""

    def _coordinator(self, ex):
        from config import ActionSpaceConfig
        from coordinator.intent_coordinator import IntentCoordinator
        c = IntentCoordinator.__new__(IntentCoordinator)
        c.gui = None
        c.executor = ex
        c.action_space = ActionSpaceConfig()
        c.ue_serving_gnb = {}
        c.ue_rnti = {}
        c.ue_collector = types.SimpleNamespace(simulation_mode=False)
        return c

    @staticmethod
    def _feas(proposed):
        from decision.intent_model import FeasibilityPrediction
        return FeasibilityPrediction(feasible=True, confidence=0.9,
                                     reasoning="", proposed_config=proposed)

    @staticmethod
    def _exec(c, feas):
        # authorize (bind canonical vector/hash) THEN execute - the
        # canonical-only _execute_trial requires a valid authorization first
        # (coordinator review C1).
        from coordinator.episode_types import ActuationTransaction
        from decision.intent_model import (
            Intent, IntentTarget, IntentType, ConstraintType)
        tx = ActuationTransaction()
        c._active_txn = tx
        # a real proposal always carries its prompt hash; supply one so the
        # pre-write S3 invariant (P1-6) sees a bound prompt hash.
        cycle = {"episode_id": "ep", "cycle_id": "cy", "proposal_id": "pr",
                 "prompt_hash": hashlib.sha256(b"verified-actuation").hexdigest()}
        intent = Intent(type=IntentType.THROUGHPUT_GOAL,
                        target=IntentTarget(kpi_name="throughput",
                                            constraint_type=ConstraintType.MIN,
                                            target_value=8.0, unit="Mbps"))
        c._authorize_action(feas, cycle, tx, intent)
        return c._execute_trial(feas)

    def test_unreadable_axis_trial_fails_closed(self):
        sims = {"gnb1": _StockGNB(pci=0), "gnb2": _StockGNB(pci=1)}
        ex = SimExecutor(sims)
        c = self._coordinator(ex)
        res = self._exec(c, self._feas({"bs1_prb": 40}))
        self.assertFalse(res["success"])
        # nothing was WRITTEN to the device (reads are argless)
        writes = [cmd for cmd in sims["gnb1"].commands
                  if len(cmd.split()) > 2]
        self.assertEqual(writes, [])

    def test_power_only_trial_still_qualifies_on_stock_gnb(self):
        # power stays device-readable on a stock gNB (ci rfatt): the d=1
        # power-only path must keep working
        sims = {"gnb1": _StockGNB(pci=0), "gnb2": _StockGNB(pci=1)}
        ex = SimExecutor(sims)
        c = self._coordinator(ex)
        res = self._exec(c, self._feas({"bs1_power_offset": 3.0}))
        self.assertTrue(res["success"])

    def test_patched_gnb_multi_axis_still_qualifies(self):
        ex, _ = _make_executor()
        c = self._coordinator(ex)
        res = self._exec(c, self._feas({"bs1_prb": 40,
                                           "bs1_power_offset": 2.0}))
        self.assertTrue(res["success"])

    def test_mixed_proposal_rejected_when_any_axis_unqualified(self):
        # even one unqualified axis in the vector rejects the whole trial
        # (partial application against an unverifiable pre-state is worse)
        sims = {"gnb1": _StockGNB(pci=0), "gnb2": _StockGNB(pci=1)}
        ex = SimExecutor(sims)
        c = self._coordinator(ex)
        res = self._exec(c, self._feas({"bs1_power_offset": 2.0,
                                           "bs1_mcs_offset": -6}))
        self.assertFalse(res["success"])
        writes = [cmd for cmd in sims["gnb1"].commands
                  if len(cmd.split()) > 2]
        self.assertEqual(writes, [])


class RollbackEpisodeVerificationTest(unittest.TestCase):

    def test_emulated_rollback_episodes_are_verified(self):
        # Acceptance: in an emulated run every rolled-back episode carries
        # restore_verified=True
        from experiments.runner import ExperimentRunner
        from experiments.topology import two_ue_topology   # legacy 2-UE (explicit)
        coordinator, channel = build_emulated_coordinator(
            seed=555, tau_trial_s=0.1, topology=two_ue_topology())
        try:
            with tempfile.TemporaryDirectory() as tmp:
                r = ExperimentRunner(coordinator=coordinator, output_dir=tmp,
                                     kpi_interval_s=0.0)
                r.channel_model = channel
                _, episodes = r.run_live(["llm_with_history"], trials=1)
        finally:
            coordinator.stop()
        rolled = [e for e in episodes if e.rolled_back]
        self.assertGreater(len(rolled), 0)   # the scenario does roll back
        for e in rolled:
            self.assertIs(e.restore_verified, True)

    def test_runner_passthrough_and_rollback_metric(self):
        from experiments.metrics import EpisodeRecord, rollback_rate
        from experiments.runner import ExperimentRunner
        trace = {"routed_to": "trial", "trial_executed": True,
                 "rolled_back": True, "entered_negotiation": True,
                 "sequence": ["S3", "S4", "S5", "S6"]}
        result = {"success": True, "trial_success": False,
                  "restore_verified": True, "feasibility": {}}
        with tempfile.TemporaryDirectory() as tmp:
            runner = ExperimentRunner(coordinator=None, output_dir=tmp)
            [ep] = runner._episode_from_result("m", 0, "P1", result, trace, 5.0)
        self.assertIs(ep.restore_verified, True)
        stats = rollback_rate([ep, EpisodeRecord(
            trial_id=1, method="m", phase="P1", trial_executed=True,
            rolled_back=True)])
        self.assertEqual(stats["n_rollbacks"], 2)
        self.assertEqual(stats["n_verified_rollbacks"], 1)


if __name__ == "__main__":
    unittest.main()
