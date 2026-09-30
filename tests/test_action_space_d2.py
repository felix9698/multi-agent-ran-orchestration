#!/usr/bin/env python3
"""
Offline unit tests for the d>=2 action-space expansion.

NO live RAN. The OAI telnet transport (OAIExecutor._telnet_cmd) is mocked to
emulate the patched gNB's responses, so these tests exercise the real clipping,
snapshot/restore, and multi-axis trial/rollback logic without any hardware.

Run:  python -m unittest tests.test_action_space_d2    (from the repo root)
  or:  python tests/test_action_space_d2.py
"""

import hashlib
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import ActionSpaceConfig
from executor.oai_executor import (
    OAIExecutor, BASE_MCS_CAP, PRB_UNCAPPED, PRIO_NEUTRAL,
)
from coordinator.intent_coordinator import (
    IntentCoordinator, profile_bounded_action_space,
)
from decision.intent_model import FeasibilityPrediction


# --------------------------------------------------------------------------- #
class TestProfileBoundedActionSpace(unittest.TestCase):
    """Gate A [H2] counter-example: on a 24-PRB profile, 106 PRB must not
    pass as an in-bounds action - the bound follows the ACTIVE profile."""

    @staticmethod
    def _network(num_prbs):
        gnbs = {f"gnb{i + 1}": types.SimpleNamespace(num_prb=n)
                for i, n in enumerate(num_prbs)}
        return types.SimpleNamespace(gnbs=gnbs,
                                     action_space=ActionSpaceConfig())

    def test_24_prb_profile_caps_the_prb_axis(self):
        space = profile_bounded_action_space(self._network([24, 24]))
        self.assertEqual(space.prb_cap_max, 24)
        self.assertEqual(space.ue_prb_cap_max, 24)
        self.assertEqual(space.clip("prb", 106), 24)          # counter-example
        self.assertEqual(space.clip("prb", 106, per_ue=True), 24)
        self.assertEqual(space.clip("prb", 0), 0)             # uncapped legal

    def test_mixed_profile_takes_the_tightest(self):
        space = profile_bounded_action_space(self._network([51, 24]))
        self.assertEqual(space.prb_cap_max, 24)

    def test_106_profile_keeps_full_bound(self):
        space = profile_bounded_action_space(self._network([106, 106]))
        self.assertEqual(space.prb_cap_max, 106)
        self.assertEqual(space.clip("prb", 106), 106)

    def test_no_gnbs_leaves_bounds_unchanged(self):
        space = profile_bounded_action_space(
            types.SimpleNamespace(gnbs={}, action_space=ActionSpaceConfig()))
        self.assertEqual(space.prb_cap_max, 106)

    def test_config_singleton_not_mutated(self):
        net = self._network([24])
        profile_bounded_action_space(net)
        self.assertEqual(net.action_space.prb_cap_max, 106)   # copy semantics

    def test_non_prb_axes_untouched(self):
        space = profile_bounded_action_space(self._network([24]))
        self.assertEqual(space.power_offset_max_db,
                         ActionSpaceConfig().power_offset_max_db)
        self.assertEqual(space.mcs_offset_min,
                         ActionSpaceConfig().mcs_offset_min)

    def test_system_prompt_advertises_clamped_bounds(self):
        # the LLM must be told the PROFILE bounds, not the largest profile:
        # advertising [6, 106] on a 24-PRB cell invites out-of-profile
        # proposals that only enforcement-time clipping would catch
        from decision.llm_backend import LLMBackendManager
        mgr = LLMBackendManager.__new__(LLMBackendManager)  # no discovery
        mgr.action_space = profile_bounded_action_space(self._network([24]))
        prompt = mgr.get_system_prompt()
        self.assertIn("[6, 24]", prompt)
        self.assertNotIn("[6, 106]", prompt)
        self.assertNotIn("<<", prompt)          # every token substituted
        mgr.action_space = None                  # default: full profile
        self.assertIn("[6, 106]", mgr.get_system_prompt())


# --------------------------------------------------------------------------- #
# Fake telnet transport: emulates the patched "ci" commands (rfatt/mcs/prbcap/  #
# sched_prio). Records every command sent for assertions.                       #
# --------------------------------------------------------------------------- #
def install_fake_telnet(ex, fail_cmd_prefix=None):
    """Monkeypatch ex._telnet_cmd; returns the list that records (host, cmd).

    STATEFUL per host: reads echo what was last written, so the P7
    write-then-read-back verification sees a consistent emulated device
    (mirroring the patched gNB's actual behavior).
    """
    sent = []
    devs = {}

    def dev_for(host):
        return devs.setdefault(host, {
            "att": 12.0, "prb": 0, "mcs_min": 0, "mcs_max": 28,
            "ue_prb": {}, "ue_w": {"4601": 1.0},
        })

    def fake(self, host, cmd, timeout=3.0):
        sent.append((host, cmd))
        dev = dev_for(host)
        parts = cmd.split()
        # Simulate a specific command failing (no "set to" in output).
        if fail_cmd_prefix and cmd.startswith(fail_cmd_prefix):
            return True, "error: simulated failure"
        # rfatt
        if cmd == "ci rfatt":
            return True, f"current TX attenuation {dev['att']:.1f} dB"
        if cmd.startswith("ci rfatt "):
            dev["att"] = float(parts[2])
            return True, f"TX attenuation set to {dev['att']:.1f} dB"
        # mcs
        if cmd == "ci mcs":
            return True, (f"DL MCS cap [{dev['mcs_min']}..{dev['mcs_max']}] "
                          f"UL MCS cap [0..28]")
        if cmd.startswith("ci mcs "):
            dev["mcs_max"] = int(parts[2])
            return True, f"DL MCS cap set to [0..{parts[2]}]"
        # prbcap (cell: "ci prbcap n"; per-UE: "ci prbcap n <rnti>")
        if cmd == "ci prbcap":
            if dev["prb"] == 0:
                lines = ["DL PRB cap 0 (uncapped)"]
            else:
                lines = [f"DL PRB cap {dev['prb']}"]
            lines += [f"UE {r} DL PRB cap {v}"
                      for r, v in sorted(dev["ue_prb"].items()) if v > 0]
            return True, "\n".join(lines)
        if cmd.startswith("ci prbcap "):
            n = parts[2]
            tail = " (uncapped)" if n == "0" else ""
            if len(parts) >= 4:                      # per-UE form
                dev["ue_prb"][parts[3]] = int(n)
                return True, f"UE {parts[3]} DL PRB cap set to {n}{tail}"
            dev["prb"] = int(n)
            return True, f"DL PRB cap set to {n}{tail}"
        # sched_prio (cell: "ci sched_prio w"; per-UE: "ci sched_prio w <rnti>")
        if cmd == "ci sched_prio":
            return True, "\n".join(f"UE {r} PF weight {w:.3f}"
                                   for r, w in sorted(dev["ue_w"].items()))
        if cmd.startswith("ci sched_prio "):
            rnti = parts[3] if len(parts) >= 4 else "4601"
            dev["ue_w"][rnti] = float(parts[2])
            return True, f"UE {rnti} PF weight set to {parts[2]}"
        # single-UE RNTI lookup (get_connected_rnti)
        if cmd == "ci get_single_rnti":
            return True, "single UE RNTI 4601"
        return True, "ok"

    ex._telnet_cmd = types.MethodType(fake, ex)
    return sent


def make_executor(fail_cmd_prefix=None):
    ex = OAIExecutor()
    sent = install_fake_telnet(ex, fail_cmd_prefix)
    return ex, sent


def make_coordinator(executor, simulation_mode=False):
    """A coordinator with just the attributes the trial/rollback path needs."""
    coord = IntentCoordinator.__new__(IntentCoordinator)  # skip heavy __init__
    coord.executor = executor
    coord.action_space = ActionSpaceConfig()
    coord.action_min_db = coord.action_space.power_offset_min_db
    coord.action_max_db = coord.action_space.power_offset_max_db
    # Per-UE addressing maps (mirror IntentCoordinator.__init__); the paper
    # topology is UE1/UE2 -> gnb1 (shared cell), UE3 -> gnb2.
    coord.ue_serving_gnb = {"ue1": "gnb1", "ue2": "gnb1", "ue3": "gnb2"}
    coord.ue_rnti = {}
    coord.ue_collector = types.SimpleNamespace(simulation_mode=simulation_mode)
    coord.gui = None
    coord.logs = []
    coord._log_gui = lambda m: coord.logs.append(m)
    return coord


def _authorized_exec(coord, feasibility):
    """Authorize the action (bind the canonical vector/hash) THEN execute - the
    canonical-only _execute_trial requires a valid authorization first
    (coordinator review C1)."""
    from coordinator.episode_types import ActuationTransaction
    from decision.intent_model import (
        Intent, IntentTarget, IntentType, ConstraintType)
    tx = ActuationTransaction()
    coord._active_txn = tx
    # a real proposal always carries its prompt hash; supply one so the pre-write
    # S3 invariant (P1-6) sees a bound prompt hash.
    cycle = {"episode_id": "ep-x", "cycle_id": "cy-x", "proposal_id": "pr-x",
             "prompt_hash": hashlib.sha256(b"action-space-d2").hexdigest()}
    intent = Intent(type=IntentType.THROUGHPUT_GOAL,
                    target=IntentTarget(kpi_name="throughput",
                                        constraint_type=ConstraintType.MIN,
                                        target_value=8.0, unit="Mbps"))
    coord._authorize_action(feasibility, cycle, tx, intent)
    return coord._execute_trial(feasibility)


# --------------------------------------------------------------------------- #
class TestActionSpaceConfig(unittest.TestCase):
    def setUp(self):
        self.U = ActionSpaceConfig()

    def test_power_clip(self):
        self.assertEqual(self.U.clip("power_offset", 15), 10.0)
        self.assertEqual(self.U.clip("power_offset", -15), -10.0)
        self.assertEqual(self.U.clip("power_offset", 3), 3.0)

    def test_prb_clip_and_uncapped(self):
        self.assertEqual(self.U.clip("prb", 0), PRB_UNCAPPED)      # uncapped sentinel
        self.assertEqual(self.U.clip("prb", -5), PRB_UNCAPPED)     # <=0 -> uncapped
        self.assertEqual(self.U.clip("prb", 3), 6)                 # below min -> min
        self.assertEqual(self.U.clip("prb", 200), 106)            # above max -> max
        self.assertEqual(self.U.clip("prb", 24), 24)

    def test_priority_and_mcs_clip(self):
        self.assertEqual(self.U.clip("sched_priority", 10), 4.0)
        self.assertEqual(self.U.clip("sched_priority", 0.1), 0.25)
        self.assertEqual(self.U.clip("mcs_offset", 5), 0.0)        # cannot exceed base
        self.assertEqual(self.U.clip("mcs_offset", -50), -20.0)

    def test_neutrals_are_backward_compatible(self):
        self.assertEqual(self.U.neutral("power_offset"), 0.0)
        self.assertEqual(self.U.neutral("prb"), 0)
        self.assertEqual(self.U.neutral("sched_priority"), 1.0)
        self.assertEqual(self.U.neutral("mcs_offset"), 0.0)

    def test_per_ue_bounds_and_clip(self):
        # per-UE PRB / scheduling-priority axes clip to their (own) bounds
        self.assertEqual(self.U.bounds("sched_priority", per_ue=True), (0.25, 4.0))
        self.assertEqual(self.U.bounds("prb", per_ue=True), (6, 106))
        self.assertEqual(self.U.clip("sched_priority", 10, per_ue=True), 4.0)
        self.assertEqual(self.U.clip("sched_priority", 0.1, per_ue=True), 0.25)
        self.assertEqual(self.U.clip("prb", 500, per_ue=True), 106)
        self.assertEqual(self.U.clip("prb", 0, per_ue=True), PRB_UNCAPPED)  # uncapped
        # power and MCS have no per-UE variant
        for axis in ("power_offset", "mcs_offset"):
            with self.assertRaises(KeyError):
                self.U.bounds(axis, per_ue=True)


# --------------------------------------------------------------------------- #
class TestExecutorPrimitives(unittest.TestCase):
    def test_set_prb_allocation(self):
        ex, sent = make_executor()
        self.assertTrue(ex.set_prb_allocation("gnb1", 24))
        self.assertEqual(sent[-1], ("127.0.0.1", "ci prbcap 24"))
        self.assertEqual(ex.states["gnb1"].prb_cap, 24)
        # uncapped
        self.assertTrue(ex.set_prb_allocation("gnb1", 0))
        self.assertEqual(sent[-1], ("127.0.0.1", "ci prbcap 0"))
        self.assertEqual(ex.states["gnb1"].prb_cap, 0)
        # negative -> uncapped
        self.assertTrue(ex.set_prb_allocation("gnb1", -3))
        self.assertEqual(sent[-1], ("127.0.0.1", "ci prbcap 0"))

    def test_set_sched_priority(self):
        ex, sent = make_executor()
        self.assertTrue(ex.set_sched_priority("gnb2", 2.0))
        self.assertEqual(sent[-1], ("192.168.0.51", "ci sched_prio 2.000"))
        self.assertAlmostEqual(ex.states["gnb2"].sched_priority, 2.0)
        # with explicit rnti (hex-formatted)
        self.assertTrue(ex.set_sched_priority("gnb1", 1.5, rnti=0x4601))
        self.assertEqual(sent[-1], ("127.0.0.1", "ci sched_prio 1.500 4601"))

    def test_set_mcs_offset_to_cap_conversion(self):
        ex, sent = make_executor()
        # offset -18 -> cap 28-18 = 10
        self.assertTrue(ex.set_mcs_offset("gnb1", -18))
        self.assertEqual(sent[-1], ("127.0.0.1", "ci mcs 10"))
        self.assertEqual(ex.states["gnb1"].mcs_cap, 10)
        self.assertEqual(ex.states["gnb1"].mcs_offset, -18)
        # offset 0 -> base cap (uncapped)
        self.assertTrue(ex.set_mcs_offset("gnb1", 0))
        self.assertEqual(sent[-1], ("127.0.0.1", f"ci mcs {BASE_MCS_CAP}"))

    def test_hardware_safety_clamps(self):
        ex, sent = make_executor()
        # MCS cap cannot exceed base or go below 0
        ex.set_mcs_offset("gnb1", +99)
        self.assertEqual(ex.states["gnb1"].mcs_cap, BASE_MCS_CAP)
        ex.set_mcs_offset("gnb1", -99)
        self.assertEqual(ex.states["gnb1"].mcs_cap, 0)

    def test_apply_axis_dispatch(self):
        ex, sent = make_executor()
        ex.apply_axis("gnb1", "power_offset", 3.0)
        ex.apply_axis("gnb1", "prb", 12)
        ex.apply_axis("gnb1", "sched_priority", 0.5)
        ex.apply_axis("gnb1", "mcs_offset", -8)
        cmds = [c for _, c in sent]
        self.assertIn("ci rfatt 9.0", cmds)          # 12 - 3 = 9 dB attenuation
        self.assertIn("ci prbcap 12", cmds)
        self.assertIn("ci sched_prio 0.500", cmds)
        self.assertIn("ci mcs 20", cmds)             # 28 - 8


# --------------------------------------------------------------------------- #
class TestSnapshotRestore(unittest.TestCase):
    def test_snapshot_shape(self):
        ex, _ = make_executor()
        snap = ex.snapshot()
        self.assertEqual(set(snap["gnb1"]),
                         {"power_offset_db", "prb_cap", "sched_priority", "mcs_offset",
                          "ue_sched_priority", "ue_prb_cap", "reestab_count",
                          "snapshot_source"})   # C6 per-axis provenance
        # per-UE override dicts start empty (neutral)
        self.assertEqual(snap["gnb1"]["ue_sched_priority"], {})
        self.assertEqual(snap["gnb1"]["ue_prb_cap"], {})

    def test_multiaxis_restore_roundtrip(self):
        ex, sent = make_executor()
        snap = ex.snapshot()                       # all neutral
        # Change every axis on gnb1
        ex.set_power_offset("gnb1", 3.0)
        ex.set_prb_allocation("gnb1", 24)
        ex.set_sched_priority("gnb1", 2.0)
        ex.set_mcs_offset("gnb1", -18)
        # Restore -> back to neutral on every axis
        self.assertTrue(ex.restore(snap))
        st = ex.states["gnb1"]
        self.assertEqual(st.power_offset_db, 0.0)
        self.assertEqual(st.prb_cap, 0)
        self.assertEqual(st.sched_priority, 1.0)
        self.assertEqual(st.mcs_offset, 0.0)

    def test_restore_is_diff_based_power_only(self):
        """A trial that changed ONLY power must roll back ONLY power (so an
        un-patched gNB that lacks prbcap/mcs/sched_prio is never touched)."""
        ex, sent = make_executor()
        snap = ex.snapshot()                       # all neutral
        ex.set_power_offset("gnb1", 5.0)
        sent.clear()
        self.assertTrue(ex.restore(snap))
        cmds = [c for _, c in sent]
        # only rfatt re-SENT (writes); the P7 restore audit adds argless
        # read commands for every axis, which are not writes
        writes = [c for c in cmds if len(c.split()) > 2]
        self.assertTrue(all(c.startswith("ci rfatt") for c in writes), cmds)
        self.assertTrue(any(c.startswith("ci rfatt ") for c in cmds))

    def test_restore_legacy_flat_form(self):
        """Old callers pass {gnb: offset_db}; restore must still work (power)."""
        ex, sent = make_executor()
        ex.set_power_offset("gnb1", 4.0)
        sent.clear()
        self.assertTrue(ex.restore({"gnb1": 0.0}))
        self.assertEqual(ex.states["gnb1"].power_offset_db, 0.0)
        self.assertTrue(any(c.startswith("ci rfatt") for _, c in sent))


# --------------------------------------------------------------------------- #
class TestCoordinatorParsing(unittest.TestCase):
    def setUp(self):
        ex, _ = make_executor()
        self.coord = make_coordinator(ex)

    def test_parse_axis_key(self):
        f = self.coord._parse_axis_key
        # per-cell keys -> gnb_id set, ue_id None
        self.assertEqual(f("bs1_power_offset"), ("gnb1", None, "power_offset", False))
        self.assertEqual(f("bs2_power_offset"), ("gnb2", None, "power_offset", False))
        self.assertEqual(f("bs1_prb"), ("gnb1", None, "prb", False))
        self.assertEqual(f("bs2_sched_priority"), ("gnb2", None, "sched_priority", False))
        self.assertEqual(f("bs1_mcs_offset"), ("gnb1", None, "mcs_offset", False))
        self.assertEqual(f("bs1_mcs_cap"), ("gnb1", None, "mcs_offset", True))   # absolute
        # per-UE keys -> ue_id set, gnb_id None (serving gNB resolved later)
        self.assertEqual(f("ue1_sched_priority"), (None, "ue1", "sched_priority", False))
        self.assertEqual(f("ue3_prb"), (None, "ue3", "prb", False))
        self.assertEqual(f("ue2_prb_alloc"), (None, "ue2", "prb", False))
        # per-UE addressing is rejected on the cell-only axes (power, MCS)
        self.assertIsNone(f("ue1_power_offset"))
        self.assertIsNone(f("ue1_mcs_offset"))
        self.assertIsNone(f("expected_kpi"))
        self.assertIsNone(f("reasoning"))

    def test_parse_action_vector_full(self):
        proposed = {
            "bs1_power_offset": 3.0,
            "bs2_power_offset": -2.0,
            "bs1_prb": 24,
            "bs2_sched_priority": 2.0,
            "bs1_mcs_offset": -8,
            "reasoning": "ignore me",
            "expected_kpi": {"ue1_throughput": 8.0},
        }
        actions = self.coord._parse_action_vector(proposed)
        triples = {(g, a): v for g, a, v, u in actions}
        self.assertEqual(triples[("gnb1", "power_offset")], 3.0)
        self.assertEqual(triples[("gnb2", "power_offset")], -2.0)
        self.assertEqual(triples[("gnb1", "prb")], 24)
        self.assertEqual(triples[("gnb2", "sched_priority")], 2.0)
        self.assertEqual(triples[("gnb1", "mcs_offset")], -8)
        self.assertEqual(len(actions), 5)   # non-axis keys ignored
        # all per-cell -> ue_id is None on every action
        self.assertTrue(all(u is None for _, _, _, u in actions))

    def test_parse_action_vector_per_ue(self):
        # ue1/ue2 -> gnb1, ue3 -> gnb2 (from ue_serving_gnb)
        proposed = {"ue1_sched_priority": 3.0, "ue2_prb": 20, "ue3_sched_priority": 0.5}
        actions = self.coord._parse_action_vector(proposed)
        by_ue = {u: (g, a, v) for g, a, v, u in actions}
        self.assertEqual(by_ue["ue1"], ("gnb1", "sched_priority", 3.0))
        self.assertEqual(by_ue["ue2"], ("gnb1", "prb", 20.0))
        self.assertEqual(by_ue["ue3"], ("gnb2", "sched_priority", 0.5))

    def test_parse_mcs_cap_converts_to_offset(self):
        actions = self.coord._parse_action_vector({"bs1_mcs_cap": 10})
        # absolute cap 10 -> offset 10 - 28 = -18; per-cell so ue_id None
        self.assertEqual(actions, [("gnb1", "mcs_offset", 10 - BASE_MCS_CAP, None)])

    def test_is_clipped(self):
        c = self.coord._is_clipped
        self.assertFalse(c("power_offset", 3.0, 3.0))
        self.assertTrue(c("power_offset", 15.0, 10.0))
        self.assertFalse(c("prb", 0, 0))            # uncapped sentinel, not a clip
        self.assertFalse(c("prb", 24, 24))
        self.assertTrue(c("prb", 200, 106))
        self.assertFalse(c("prb", 23.7, 24))        # rounding is not a clip


# --------------------------------------------------------------------------- #
class TestExecuteTrialAndRollback(unittest.TestCase):
    def _feasibility(self, proposed):
        return FeasibilityPrediction(feasible=True, confidence=0.9,
                                     reasoning="", proposed_config=proposed)

    def test_simulation_mode_multiaxis(self):
        ex, sent = make_executor()
        coord = make_coordinator(ex, simulation_mode=True)
        res = _authorized_exec(coord, self._feasibility({
            "bs1_power_offset": 3.0, "bs1_prb": 24,
            "bs1_sched_priority": 2.0, "bs1_mcs_offset": -8,
        }))
        self.assertTrue(res["success"])
        self.assertEqual(len(res["applied"]), 4)
        # simulation must not WRITE to the executor telnet; the C6 snapshot
        # readback + P8 counter read (argless read commands) do run because
        # the snapshot precedes the simulation shortcut
        writes = [c for _, c in sent if len(c.split()) > 2]
        self.assertEqual(writes, [], sent)

    def test_hardware_apply_all_axes(self):
        ex, sent = make_executor()
        coord = make_coordinator(ex, simulation_mode=False)
        res = _authorized_exec(coord, self._feasibility({
            "bs1_power_offset": 3.0, "bs1_prb": 24,
            "bs1_sched_priority": 2.0, "bs1_mcs_offset": -8,
        }))
        self.assertTrue(res["success"])
        cmds = [c for _, c in sent]
        self.assertIn("ci rfatt 9.0", cmds)
        self.assertIn("ci prbcap 24", cmds)
        self.assertIn("ci sched_prio 2.000", cmds)
        self.assertIn("ci mcs 20", cmds)

    def test_clipping_counted(self):
        ex, _ = make_executor()
        coord = make_coordinator(ex, simulation_mode=False)
        res = _authorized_exec(coord, self._feasibility({
            "bs1_power_offset": 99.0,   # -> clipped to +10
            "bs1_prb": 500,             # -> clipped to 106
            "bs1_mcs_offset": -8,       # in bounds, not clipped
        }))
        axes_clipped = {c["axis"] for c in res["clipped"]}
        self.assertEqual(axes_clipped, {"power_offset", "prb"})
        self.assertEqual(ex.states["gnb1"].power_offset_db, 10.0)
        self.assertEqual(ex.states["gnb1"].prb_cap, 106)

    def test_rollback_on_apply_failure_restores_all(self):
        # sched_prio fails -> partial application must be fully restored
        ex, sent = make_executor(fail_cmd_prefix="ci sched_prio")
        coord = make_coordinator(ex, simulation_mode=False)
        res = _authorized_exec(coord, self._feasibility({
            "bs1_power_offset": 3.0, "bs1_prb": 24,
            "bs1_sched_priority": 2.0, "bs1_mcs_offset": -8,
        }))
        self.assertFalse(res["success"])
        # every axis is back to neutral after the automatic restore
        st = ex.states["gnb1"]
        self.assertEqual(st.power_offset_db, 0.0)
        self.assertEqual(st.prb_cap, 0)
        self.assertEqual(st.mcs_offset, 0.0)

    def test_backward_compat_power_only(self):
        """A power-only proposal behaves exactly like the d=1 baseline: only
        rfatt is sent, other axes stay neutral."""
        ex, sent = make_executor()
        coord = make_coordinator(ex, simulation_mode=False)
        res = _authorized_exec(coord, self._feasibility({
            "bs1_power_offset": 3.0, "bs2_power_offset": -2.0,
        }))
        self.assertTrue(res["success"])
        # only rfatt WRITES are sent; the C6 device-readback snapshot and
        # the P8 counter read are argless read commands, not writes
        writes = [c for _, c in sent if len(c.split()) > 2]
        self.assertTrue(all(c.startswith("ci rfatt") for c in writes), writes)
        self.assertTrue(writes)   # the power writes did happen
        self.assertEqual(res["clipped"], [])
        # untouched axes remain neutral
        self.assertEqual(ex.states["gnb1"].prb_cap, 0)
        self.assertEqual(ex.states["gnb1"].sched_priority, 1.0)
        self.assertEqual(ex.states["gnb1"].mcs_offset, 0.0)

    def test_no_applicable_config(self):
        ex, sent = make_executor()
        coord = make_coordinator(ex, simulation_mode=False)
        res = _authorized_exec(coord, self._feasibility({"reasoning": "nothing"}))
        self.assertFalse(res["success"])
        # nothing APPLIED: the C6 device-readback snapshot + P8 counter read
        # are argless READ commands; no write may be sent
        writes = [c for _, c in sent if len(c.split()) > 2]
        self.assertEqual(writes, [], sent)


# --------------------------------------------------------------------------- #
# Per-UE (per-RNTI) addressing: intra-cell fairness (UE1/UE2 share BS1).       #
# --------------------------------------------------------------------------- #
class TestExecutorPerUE(unittest.TestCase):
    def test_set_prb_allocation_per_ue(self):
        ex, sent = make_executor()
        self.assertTrue(ex.set_prb_allocation("gnb1", 20, rnti=0x4601))
        self.assertEqual(sent[-1], ("127.0.0.1", "ci prbcap 20 4601"))
        self.assertEqual(ex.states["gnb1"].ue_prb_cap[0x4601], 20)
        self.assertEqual(ex.states["gnb1"].prb_cap, 0)   # cell cap untouched

    def test_set_sched_priority_per_ue_records_state(self):
        ex, sent = make_executor()
        self.assertTrue(ex.set_sched_priority("gnb1", 2.0, rnti=0x4601))
        self.assertEqual(sent[-1], ("127.0.0.1", "ci sched_prio 2.000 4601"))
        self.assertAlmostEqual(ex.states["gnb1"].ue_sched_priority[0x4601], 2.0)
        self.assertEqual(ex.states["gnb1"].sched_priority, 1.0)  # cell weight untouched

    def test_apply_axis_per_ue_and_reject_cell_only(self):
        ex, sent = make_executor()
        self.assertTrue(ex.apply_axis("gnb1", "prb", 15, rnti=0x4601))
        # last WRITE (a P7 verification read follows every apply)
        self.assertIn(("127.0.0.1", "ci prbcap 15 4601"), sent)
        self.assertTrue(ex.apply_axis("gnb1", "sched_priority", 0.5, rnti=0x4602))
        self.assertIn(("127.0.0.1", "ci sched_prio 0.500 4602"), sent)
        # power/MCS are cell-wide only: a per-UE target is rejected
        self.assertFalse(ex.apply_axis("gnb1", "power_offset", 3.0, rnti=0x4601))
        self.assertFalse(ex.apply_axis("gnb1", "mcs_offset", -5, rnti=0x4601))

    def test_per_ue_snapshot_restore_roundtrip(self):
        ex, sent = make_executor()
        snap = ex.snapshot()                          # neutral: empty per-UE dicts
        ex.set_sched_priority("gnb1", 3.0, rnti=0x4601)
        ex.set_prb_allocation("gnb1", 18, rnti=0x4601)
        ex.set_sched_priority("gnb1", 0.5, rnti=0x4602)
        self.assertTrue(ex.restore(snap))
        st = ex.states["gnb1"]
        # every trial-introduced per-UE override is reset to neutral
        self.assertAlmostEqual(st.axis_value("sched_priority", 0x4601), 1.0)
        self.assertEqual(st.axis_value("prb", 0x4601), 0)
        self.assertAlmostEqual(st.axis_value("sched_priority", 0x4602), 1.0)

    def test_restore_diff_based_leaves_unchanged_ue_axis(self):
        """restore re-sends ONLY the per-UE axes that actually changed."""
        ex, sent = make_executor()
        ex.set_sched_priority("gnb1", 2.0, rnti=0x4601)
        snap = ex.snapshot()                          # ue1 priority 2.0 captured
        ex.set_prb_allocation("gnb1", 12, rnti=0x4601)   # change a DIFFERENT axis
        sent.clear()
        self.assertTrue(ex.restore(snap))
        cmds = [c for _, c in sent]
        # write-form commands have arguments (reads are argless audits)
        self.assertTrue(any(c.startswith("ci prbcap ") for c in cmds), cmds)
        self.assertFalse(any(c.startswith("ci sched_prio ") for c in cmds), cmds)

    def test_reset_all_axes_clears_per_ue(self):
        ex, sent = make_executor()
        ex.set_sched_priority("gnb1", 3.0, rnti=0x4601)
        ex.set_prb_allocation("gnb2", 20, rnti=0x4603)
        self.assertTrue(ex.reset_all_axes())
        self.assertAlmostEqual(ex.states["gnb1"].axis_value("sched_priority", 0x4601), 1.0)
        self.assertEqual(ex.states["gnb2"].axis_value("prb", 0x4603), 0)


# --------------------------------------------------------------------------- #
class TestCoordinatorPerUE(unittest.TestCase):
    def _feasibility(self, proposed):
        return FeasibilityPrediction(feasible=True, confidence=0.9,
                                     reasoning="", proposed_config=proposed)

    def test_resolve_ue_rnti_registered(self):
        ex, _ = make_executor()
        coord = make_coordinator(ex, simulation_mode=False)
        coord.register_ue_rnti("ue1", 0x4601)
        self.assertEqual(coord._resolve_ue_rnti("ue1"), 0x4601)

    def test_resolve_ue_rnti_via_single_rnti_lookup(self):
        # UE3 is alone on gnb2 -> get_connected_rnti resolves it (fake -> 0x4601)
        ex, _ = make_executor()
        coord = make_coordinator(ex, simulation_mode=False)
        self.assertEqual(coord._resolve_ue_rnti("ue3"), 0x4601)
        self.assertEqual(coord.ue_rnti["ue3"], 0x4601)   # cached

    def test_hardware_apply_per_ue(self):
        ex, sent = make_executor()
        coord = make_coordinator(ex, simulation_mode=False)
        coord.register_ue_rnti("ue1", 0x4601)
        coord.register_ue_rnti("ue2", 0x4602)
        res = _authorized_exec(coord, self._feasibility({
            "ue1_sched_priority": 3.0,   # favor UE1
            "ue2_sched_priority": 0.5,   # starve UE2
            "ue1_prb": 20,
        }))
        self.assertTrue(res["success"])
        cmds = [c for _, c in sent]
        self.assertIn("ci sched_prio 3.000 4601", cmds)
        self.assertIn("ci sched_prio 0.500 4602", cmds)
        self.assertIn("ci prbcap 20 4601", cmds)
        st = ex.states["gnb1"]   # both UEs are on gnb1
        self.assertAlmostEqual(st.ue_sched_priority[0x4601], 3.0)
        self.assertAlmostEqual(st.ue_sched_priority[0x4602], 0.5)
        self.assertEqual(st.ue_prb_cap[0x4601], 20)

    def test_per_ue_clip_uses_per_ue_bounds(self):
        ex, _ = make_executor()
        coord = make_coordinator(ex, simulation_mode=False)
        coord.register_ue_rnti("ue1", 0x4601)
        res = _authorized_exec(coord, self._feasibility({
            "ue1_sched_priority": 99.0,   # -> clipped to 4.0
            "ue1_prb": 500,               # -> clipped to 106
        }))
        clipped = {(c["ue_id"], c["axis"]) for c in res["clipped"]}
        self.assertEqual(clipped, {("ue1", "sched_priority"), ("ue1", "prb")})
        self.assertAlmostEqual(ex.states["gnb1"].ue_sched_priority[0x4601], 4.0)
        self.assertEqual(ex.states["gnb1"].ue_prb_cap[0x4601], 106)

    def test_per_ue_rollback_to_neutral(self):
        ex, sent = make_executor()
        coord = make_coordinator(ex, simulation_mode=False)
        coord.register_ue_rnti("ue1", 0x4601)
        res = _authorized_exec(coord, self._feasibility({
            "ue1_sched_priority": 3.0, "ue1_prb": 20,
        }))
        self.assertTrue(res["success"])
        # roll back to the pre-trial snapshot -> UE1 back to neutral on both axes
        self.assertTrue(ex.restore(res["snapshot"]))
        self.assertAlmostEqual(ex.states["gnb1"].axis_value("sched_priority", 0x4601), 1.0)
        self.assertEqual(ex.states["gnb1"].axis_value("prb", 0x4601), 0)

    def test_per_ue_apply_failure_rolls_back_all(self):
        # prbcap fails -> the already-applied sched_prio must be fully restored
        ex, sent = make_executor(fail_cmd_prefix="ci prbcap")
        coord = make_coordinator(ex, simulation_mode=False)
        coord.register_ue_rnti("ue1", 0x4601)
        res = _authorized_exec(coord, self._feasibility({
            "ue1_sched_priority": 3.0, "ue1_prb": 20,
        }))
        self.assertFalse(res["success"])
        self.assertAlmostEqual(
            ex.states["gnb1"].axis_value("sched_priority", 0x4601), 1.0)

    def test_unresolved_rnti_fails_trial_safely(self):
        # UE sharing a cell with no registration: get_connected_rnti can't
        # disambiguate (returns None) -> trial fails safe, nothing applied.
        ex, sent = make_executor()
        coord = make_coordinator(ex, simulation_mode=False)
        coord.executor.get_connected_rnti = lambda gnb: None
        res = _authorized_exec(coord, self._feasibility({"ue1_sched_priority": 3.0}))
        self.assertFalse(res["success"])
        # no sched_prio WRITE ever sent (argless audit reads are fine)
        self.assertFalse(any(c.startswith("ci sched_prio ") for _, c in sent))

    def test_mixed_per_cell_and_per_ue(self):
        ex, sent = make_executor()
        coord = make_coordinator(ex, simulation_mode=False)
        coord.register_ue_rnti("ue1", 0x4601)
        res = _authorized_exec(coord, self._feasibility({
            "bs1_power_offset": 3.0,      # per-cell
            "ue1_sched_priority": 2.0,    # per-UE on gnb1
        }))
        self.assertTrue(res["success"])
        cmds = [c for _, c in sent]
        self.assertIn("ci rfatt 9.0", cmds)                 # 12 - 3
        self.assertIn("ci sched_prio 2.000 4601", cmds)

    def test_ue_overrides_surfaced_in_network_state(self):
        ex, _ = make_executor()
        coord = make_coordinator(ex, simulation_mode=False)
        coord.register_ue_rnti("ue1", 0x4601)
        ex.set_sched_priority("gnb1", 2.5, rnti=0x4601)
        ov = coord._ue_overrides_for("gnb1", ex.states["gnb1"])
        self.assertEqual(ov, {"ue1": {"sched_priority": 2.5}})
        # neutral overrides are not surfaced
        ex.set_sched_priority("gnb1", 1.0, rnti=0x4601)
        self.assertEqual(coord._ue_overrides_for("gnb1", ex.states["gnb1"]), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
