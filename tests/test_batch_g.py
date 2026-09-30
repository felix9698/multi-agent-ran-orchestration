#!/usr/bin/env python3
"""Batch G tests: 3-UE topology (P0-18), environment/action separation (P0-19),
paired baselines + raw evidence (P0-20), trained RL, and the review-blocker
contracts. Offline: no hardware / network / OTA."""

import json
import math
import re
import tempfile
import unittest

from experiments.topology import (
    ExperimentTopology, three_ue_shared_topology, two_ue_topology,
    from_network_config, add_adversarial_ue,
)
from experiments.environment import (
    ACTION_AXES, ENVIRONMENT_AXES, axis_intersection, assert_axes_disjoint,
    AxisOverlapError, EnvironmentEvent, ActionEvent, EnvironmentTrace,
    generate_environment_trace, ExogenousEnvironmentDriver,
    validate_exogenous_driver, require_driver_capability,
    validate_applied_environment,
)
from experiments.paired_runner import (
    PairedBlockRunner, DataOrigin, PaperExportError, PaperGateEvidence,
    PairedResetError, RawEvidenceRecord, deterministic_method_order,
    dumps_finite, sanitize_finite,
)
from experiments.metrics import (
    EpisodeRecord, ClipEvent, IntentConfig, executor_enforcement_statistics,
)
import config as cfgmod

# P0-20 canonical required comparison method set (fixed_threshold + adaptive are
# the theta*-ablation pair; llm_with_history is NOT one of the required six).
ALL_METHODS = ["rule_based", "score_heuristic", "rl_controller",
               "llm_no_history", "fixed_threshold", "adaptive"]


# --------------------------------------------------------------------------- #
# P0-18 topology                                                              #
# --------------------------------------------------------------------------- #

class ThreeUETopologyTest(unittest.TestCase):
    def test_three_ue_topology_is_constructed(self):
        t = three_ue_shared_topology()
        self.assertEqual(set(t.ue_ids()), {"ue1", "ue2", "ue3"})
        self.assertEqual(t.n_ues(), 3)
        self.assertEqual(t.n_bs(), 2)

    def test_two_ues_share_primary_cell(self):
        t = three_ue_shared_topology()
        self.assertEqual(t.primary_shared_cell(), "gnb1")
        self.assertEqual(set(t.contending_ues()), {"ue1", "ue2"})
        self.assertTrue(t.is_shared("gnb1"))
        self.assertFalse(t.is_shared("gnb2"))
        self.assertEqual(t.ues_on("gnb2"), ("ue3",))

    def test_config_default_is_three_ue_shared(self):
        c = cfgmod.get_default_config()
        t = from_network_config(c.network)
        self.assertEqual(set(t.ue_ids()), {"ue1", "ue2", "ue3"})
        self.assertEqual(t.serving_gnb("ue1"), "gnb1")
        self.assertEqual(t.serving_gnb("ue2"), "gnb1")
        self.assertEqual(t.serving_gnb("ue3"), "gnb2")

    def test_topology_is_deeply_immutable(self):
        t = three_ue_shared_topology()
        with self.assertRaises(TypeError):
            t.ue_serving["ue1"] = "evil"          # read-only mapping
        with self.assertRaises(TypeError):
            t.ue_rnti["ue1"] = 0                   # read-only mapping

    def test_topology_does_not_alias_input(self):
        d = {"ue1": "gnb1", "ue2": "gnb1"}
        t = ExperimentTopology(ue_serving=d, ue_rnti={})
        d["ue1"] = "gnbX"
        self.assertEqual(t.serving_gnb("ue1"), "gnb1")  # not aliased

    def test_from_network_config_validates_and_preserves_bs(self):
        c = cfgmod.get_default_config()
        t = from_network_config(c.network)
        # preserves configured BS ids (config order), no last-char guess
        self.assertEqual(t.bs_ids(), tuple(c.network.gnbs.keys()))
        # live/config topology carries NO guessed RNTIs (resolved live)
        self.assertIsNone(t.rnti("ue3"))

    def test_from_network_config_rejects_unconfigured_gnb(self):
        c = cfgmod.get_default_config()
        c.network.ues["ue3"].initial_serving_gnb = "gnbZ"
        with self.assertRaises(ValueError):
            from_network_config(c.network)

    def test_adversarial_fourth_ue_is_enumerated(self):
        t = add_adversarial_ue(three_ue_shared_topology(), "ue4")
        self.assertIn("ue4", t.ue_ids())
        self.assertEqual(t.serving_gnb("ue4"), "gnb1")  # joins the shared cell

    def test_scoped_intents_support_ue3(self):
        # IntentConfig scope can name ue3 and evaluate it independently.
        cfg = IntentConfig(throughput_target_mbps=8.0,
                           throughput_ue_ids=("ue3",))
        self.assertEqual(cfg.i2_ue_scope(["ue1", "ue2", "ue3"]), ["ue3"])
        self.assertTrue(cfg.i2_satisfied_ue(9.0))
        self.assertFalse(cfg.i2_satisfied_ue(3.0))


class LiveTopologyFailClosedTest(unittest.TestCase):
    def test_unprovisioned_ue3_is_excluded_not_fatal(self):
        c = cfgmod.get_default_config()
        # UE3 physical identity is not guessed -> unprovisioned by default. It is a
        # logical EXPANSION SLOT: excluded from the live path, NOT a fatal error
        # (ue1+ue2 are provisioned). The gate returns the active set.
        self.assertFalse(c.network.ues["ue3"].is_physically_provisioned())
        active = c.validate_live_topology()       # must NOT raise
        self.assertEqual(active, ["ue1", "ue2"])   # ue3 excluded
        act, excl = c.live_ue_partition()
        self.assertEqual(act, ["ue1", "ue2"])
        self.assertIn("ue3", excl)

    def test_zero_provisioned_still_fails_closed(self):
        c = cfgmod.get_default_config()
        # strip every physical identity -> nothing real to run -> genuine error
        for ue in c.network.ues.values():
            ue.hostname = ue.ip = ue.ssh_user = ue.imsi = ""
        with self.assertRaises(ValueError):
            c.validate_live_topology()

    def test_provisioned_ue3_passes(self):
        c = cfgmod.get_default_config()
        u = c.network.ues["ue3"]
        u.hostname = u.ip = "10.0.0.9"
        u.ssh_user = "op"
        u.imsi = "208950000000033"
        active = c.validate_live_topology()  # no raise
        self.assertEqual(active, ["ue1", "ue2", "ue3"])


class ConfigDrivenConsumersTest(unittest.TestCase):
    """P0-18: the prompt, collector, system controller and dashboard derive UE
    and BS membership from CONFIGURATION - never a hardcoded ue1/ue2 pair -
    including a dynamically-added 4th UE."""

    def _cfg(self, n_ue=3, provision_ue3=False):
        c = cfgmod.get_default_config()
        if provision_ue3:
            u = c.network.ues["ue3"]
            u.hostname = u.ip = "10.0.0.9"
            u.ssh_user = "op"
            u.imsi = "208950000000033"
        if n_ue >= 4:
            from config import UEConfig
            c.network.ues["ue4"] = UEConfig(
                id="ue4", hostname="", ip="", ssh_user="",
                usrp_type="b206mini", initial_serving_gnb="gnb1", imsi="")
        return c

    @staticmethod
    def _freq(cmd):
        m = re.search(r"-C (\d+)", cmd)
        return m.group(1) if m else None

    # -- collector ----------------------------------------------------------- #
    def test_collector_from_config_enumerates_all_ues(self):
        from collectors.multi_ue_collector import MultiUECollector
        mc = MultiUECollector.from_config(self._cfg(), simulation_mode=True)
        self.assertEqual(set(mc._ue_configs), {"ue1", "ue2", "ue3"})
        self.assertEqual(mc._ue_configs["ue3"]["gnb"], "gnb2")   # serving cell

    def test_collector_from_config_enumerates_fourth_ue(self):
        from collectors.multi_ue_collector import MultiUECollector
        mc = MultiUECollector.from_config(self._cfg(n_ue=4),
                                          simulation_mode=True)
        self.assertIn("ue4", mc._ue_configs)

    # -- system controller --------------------------------------------------- #
    def test_controller_ue2_uses_gnb1_carrier(self):
        from executor.system_controller import SystemController
        sc = SystemController(network_config=self._cfg())
        # config says UE1 AND UE2 both serve gNB1 -> same carrier (NOT gNB2).
        self.assertEqual(self._freq(sc.config.ues["ue1"].start_cmd),
                         self._freq(sc.config.ues["ue2"].start_cmd))

    def test_controller_ue3_uses_gnb2_carrier_when_provisioned(self):
        from executor.system_controller import SystemController
        sc = SystemController(network_config=self._cfg(provision_ue3=True))
        f1 = self._freq(sc.config.ues["ue1"].start_cmd)   # gNB1 carrier
        f3 = self._freq(sc.config.ues["ue3"].start_cmd)   # gNB2 carrier
        self.assertIsNotNone(f3)
        self.assertNotEqual(f1, f3)

    def test_controller_configured_ue_ids_uses_injected_config(self):
        from executor.system_controller import SystemController
        sc = SystemController(network_config=self._cfg(n_ue=4))
        self.assertEqual(set(sc.configured_ue_ids()),
                         {"ue1", "ue2", "ue3", "ue4"})

    def test_controller_fourth_ue_enumerates(self):
        from executor.system_controller import SystemController
        sc = SystemController(network_config=self._cfg(n_ue=4))
        self.assertIn("ue4", sc.config.ues)

    def test_controller_unprovisioned_ue_cannot_report_start_success(self):
        from executor.system_controller import SystemController
        sc = SystemController(network_config=self._cfg())   # ue3 unprovisioned
        cmd = sc.config.ues["ue3"].start_cmd
        self.assertIn("exit 1", cmd)               # explicitly non-startable
        self.assertNotIn("nr-uesoftmodem", cmd)    # never launches a UE
        self.assertIn("UNPROVISIONED", cmd)
        self.assertFalse(sc.config.ues["ue3"].startable)   # non-startable flag

    def test_controller_start_component_refuses_unprovisioned_synchronously(self):
        from executor.system_controller import SystemController
        sc = SystemController(network_config=self._cfg())   # ue3 unprovisioned
        calls = []
        sc._exec_cmd = lambda *a, **k: (calls.append(1), (True, "", ""))[1]
        # BOTH async modes must reject SYNCHRONOUSLY: return False, never spawn a
        # thread or call _exec_cmd (no SSH attempt over a guessed identity).
        self.assertFalse(sc.start_component("ue3", async_start=False))
        self.assertFalse(sc.start_component("ue3", async_start=True))
        self.assertEqual(calls, [])                        # ZERO exec attempts

    # -- dashboard ----------------------------------------------------------- #
    def test_dashboard_configured_ue_ids(self):
        try:
            import gui.dashboard as dash
        except Exception as e:                      # headless / no tk
            self.skipTest(f"dashboard import unavailable: {e}")
        self.assertEqual(dash.configured_ue_ids(self._cfg()),
                         ("UE1", "UE2", "UE3"))

    def test_dashboard_enumerates_fourth_ue_with_color(self):
        try:
            import gui.dashboard as dash
        except Exception as e:
            self.skipTest(f"dashboard import unavailable: {e}")
        self.assertEqual(dash.configured_ue_ids(self._cfg(n_ue=4)),
                         ("UE1", "UE2", "UE3", "UE4"))
        colors = dash.configured_ue_colors(self._cfg(n_ue=4))
        self.assertEqual(set(colors), {"UE1", "UE2", "UE3", "UE4"})
        self.assertEqual(len(set(colors.values())), 4)   # distinct per UE

    # -- prompt -------------------------------------------------------------- #
    def _prompt(self, cfg):
        from decision.llm_backend import LLMBackendManager
        m = LLMBackendManager.__new__(LLMBackendManager)
        m.action_space = None
        m.ue_ids = list(cfg.network.ues.keys())
        m.ue_serving = {u: ue.initial_serving_gnb
                        for u, ue in cfg.network.ues.items()}
        return m.get_system_prompt()

    def test_prompt_json_example_enumerates_configured_ues(self):
        sp = self._prompt(self._cfg())
        for tok in ("ue1_sched_priority", "ue2_sched_priority",
                    "ue3_prb", "ue3_throughput", "bs2_power_offset"):
            self.assertIn(tok, sp)
        self.assertNotIn("<<PROPOSED_CONFIG_FIELDS>>", sp)   # token substituted
        self.assertNotIn("<<EXPECTED_KPI_FIELDS>>", sp)

    def test_prompt_json_example_enumerates_fourth_ue(self):
        sp = self._prompt(self._cfg(n_ue=4))
        self.assertIn("ue4_sched_priority", sp)              # action field
        self.assertIn("ue4_throughput", sp)                  # expected_kpi field


# --------------------------------------------------------------------------- #
# P0-19 environment / action axes                                            #
# --------------------------------------------------------------------------- #

class EnvironmentAxesTest(unittest.TestCase):
    def test_environment_and_action_axes_are_disjoint(self):
        self.assertEqual(axis_intersection(), frozenset())
        assert_axes_disjoint()  # no raise
        # rfatt is an ACTION axis, never an environment axis
        self.assertIn("rfatt", ACTION_AXES)
        self.assertNotIn("rfatt", ENVIRONMENT_AXES)

    def test_environment_event_rejects_action_axis(self):
        # overlap counterexample: an env event writing an action knob is rejected
        with self.assertRaises(AxisOverlapError):
            EnvironmentEvent("e", 0.0, "rfatt", 1.0)
        with self.assertRaises(AxisOverlapError):
            ActionEvent("a", 0.0, "offered_load_mbps", 1.0, target="bs1")

    def test_environment_event_rejects_bad_timings(self):
        for bad in (float("nan"), float("inf"), True, -1.0):
            with self.assertRaises(ValueError):
                EnvironmentEvent("e", bad, "offered_load_mbps", 12.0)
        with self.assertRaises(ValueError):
            EnvironmentEvent("e", 0.0, "offered_load_mbps", float("nan"))

    def test_trace_generation_rejects_bad_timings(self):
        phases = [{"name": "N", "serving_gain": 3, "neighbor_gain": 0}]
        for kw in ({"kpi_interval_s": float("inf")},
                   {"kpi_interval_s": 0.0},
                   {"offered_load_mbps": -1.0},
                   {"steps_per_phase": True}):
            with self.assertRaises(ValueError):
                generate_environment_trace(phases, "b", seed=1, **kw)

    def test_hash_includes_phase_label_and_content(self):
        p1 = [{"name": "Nominal", "serving_gain": 3, "neighbor_gain": 0}]
        p2 = [{"name": "RENAMED", "serving_gain": 3, "neighbor_gain": 0}]
        t1 = generate_environment_trace(p1, "b", seed=1)
        t2 = generate_environment_trace(p2, "b", seed=1)
        self.assertNotEqual(t1.canonical_hash(), t2.canonical_hash())

    def test_scheduled_vs_applied_time_distinct(self):
        # a TRACE requires a full unique phase identity on every event (P0-19).
        ev = EnvironmentEvent("e", 0.0, "offered_load_mbps", 12.0,
                              phase_idx=0, phase_name="Nominal",
                              phase_label="p0:Nominal")
        self.assertIsNone(ev.applied_monotonic_s)
        ev2 = ev.with_applied(1234.5)
        self.assertEqual(ev2.scheduled_offset_s, 0.0)
        self.assertEqual(ev2.applied_monotonic_s, 1234.5)
        # applied time is EXCLUDED from the canonical hash
        self.assertEqual(EnvironmentTrace("b", 1, (ev,)).canonical_hash(),
                         EnvironmentTrace("b", 1, (ev2,)).canonical_hash())

    def test_all_methods_use_same_environment_trace(self):
        r = PairedBlockRunner(ALL_METHODS, n_blocks=2, master_seed=5,
                              trials_per_block=1, steps_per_phase=2)
        res = r.run()
        for blk in res["blocks"]:
            hashes = {rec["environment_trace_hash"] for rec in res["records"]
                      if rec["block_id"] == blk["block_id"]}
            self.assertEqual(len(hashes), 1)   # one trace, every method

    def test_environment_trace_hash_is_recorded(self):
        r = PairedBlockRunner(ALL_METHODS, n_blocks=1, master_seed=5,
                              trials_per_block=1, steps_per_phase=2)
        res = r.run()
        blk = res["blocks"][0]
        self.assertTrue(blk["environment_trace_hash"].startswith("envtrace-"))
        self.assertEqual(blk["environment_trace"]["hash"],
                         blk["environment_trace_hash"])

    def test_order_reversal_and_unequal_rng_same_trace(self):
        # method order is derived from the master seed only; the trace from the
        # block seed only. Reversing methods / consuming unequal RNG cannot change
        # the per-block trace hash.
        a = PairedBlockRunner(ALL_METHODS, n_blocks=2, master_seed=5,
                              trials_per_block=1, steps_per_phase=2).run()
        b = PairedBlockRunner(list(reversed(ALL_METHODS)), n_blocks=2,
                              master_seed=5, trials_per_block=3,
                              steps_per_phase=2).run()
        ah = [blk["environment_trace_hash"] for blk in a["blocks"]]
        bh = [blk["environment_trace_hash"] for blk in b["blocks"]]
        self.assertEqual(ah, bh)


class _ExoDriver(ExogenousEnvironmentDriver):
    """A configurable APPROVED exogenous environment driver for the P0-19
    counterexamples: it mirrors the runner's requested {axis: value}, with
    optional injected faults (drop / wrong value / extra / action axis)."""
    def __init__(self, supported=None, drop=None, extra=None, wrong=None,
                 action_axis=False, approved=True):
        self.calls = []
        self.approved = approved
        self.supported_axes = (ENVIRONMENT_AXES if supported is None
                               else frozenset(supported))
        self._drop, self._extra = drop, extra
        self._wrong, self._action_axis = wrong, action_axis
        self.name = "test-exo-driver"

    @staticmethod
    def _requested(phase):
        from experiments.runner import ExperimentRunner
        req = {}
        for pkey, ax in ExperimentRunner._PHASE_TO_ENV_AXIS.items():
            v = phase.get(pkey)
            if v is not None:
                req[ax] = float(v)
        req.setdefault("channel_serving_gain_db",
                       float(phase.get("serving_gain", 0)))
        req.setdefault("channel_neighbor_gain_db",
                       float(phase.get("neighbor_gain", 0)))
        return req

    def apply(self, phase, phase_idx, phase_label):
        self.calls.append(phase_label)
        applied = self._requested(phase)
        if self._drop:
            applied.pop(self._drop, None)
        if self._wrong:
            applied[self._wrong] = applied.get(self._wrong, 0.0) + 99.0
        if self._extra:
            applied[self._extra[0]] = self._extra[1]
        if self._action_axis:
            applied["rfatt"] = 1.0            # forbidden coordinator action knob
        return applied


class _CountingExec:
    def __init__(self):
        self.writes = []

    def reset_all_axes(self):
        self.writes.append("reset")

    def set_power_offset(self, *a, **k):
        self.writes.append(("power",) + a)
        return True

    def apply_axis(self, *a, **k):
        self.writes.append(("apply",) + a)
        return True


class _MinCoord:
    def __init__(self):
        self.executor = _CountingExec()

    def set_probe_config(self, pc):
        pass


class P0_19EnvironmentSeparationTest(unittest.TestCase):
    """P0-19: environment vs coordinator-action separation, with fail-closed
    counterexamples (positive + negative)."""

    PHASE = {"name": "Nominal", "serving_gain": 3, "neighbor_gain": 0,
             "offered_load_mbps": 12.0, "external_disturbance_db": 0.2}

    def _live_runner(self, driver=None):
        from experiments.runner import ExperimentRunner
        r = ExperimentRunner(coordinator=None)
        r.channel_model = None          # LIVE path (no emulated channel)
        r.env_driver = driver
        r.phases = [dict(self.PHASE)]
        return r

    # -- startup axis overlap fails BEFORE any method / action -------------- #
    def test_startup_overlap_fails_before_any_method_or_action(self):
        import experiments.environment as envmod
        from experiments.runner import ExperimentRunner
        c = _MinCoord()
        r = ExperimentRunner(coordinator=c)
        r.channel_model = object()      # emulated (no driver needed)
        r.phases = [dict(self.PHASE)]
        orig = envmod.ENVIRONMENT_AXES
        try:
            envmod.ENVIRONMENT_AXES = frozenset(orig | {"rfatt"})   # overlap!
            with self.assertRaises(AxisOverlapError):
                r.run_live(["llm_with_history"], trials=1)
        finally:
            envmod.ENVIRONMENT_AXES = orig
        self.assertEqual(c.executor.writes, [])          # ZERO writes

    # -- live run WITHOUT an approved driver -> zero executor writes -------- #
    def test_live_without_driver_fails_zero_writes(self):
        from experiments.runner import ExperimentRunner
        c = _MinCoord()
        r = ExperimentRunner(coordinator=c)
        r.channel_model = None          # LIVE
        r.env_driver = None             # no driver
        r.phases = [dict(self.PHASE)]
        with self.assertRaises(AxisOverlapError):
            r.run_live(["llm_with_history"], trials=1)
        self.assertEqual(c.executor.writes, [])          # ZERO reset/action

    def test_live_unapproved_driver_fails_zero_writes(self):
        from experiments.runner import ExperimentRunner
        c = _MinCoord()
        r = ExperimentRunner(coordinator=c)
        r.channel_model = None
        r.env_driver = _ExoDriver(approved=1)   # truthy but NOT exactly True
        r.phases = [dict(self.PHASE)]
        with self.assertRaises(AxisOverlapError):
            r.run_live(["llm_with_history"], trials=1)
        self.assertEqual(c.executor.writes, [])

    # -- run_live preflight fails BEFORE any reset (zero writes + empty log) - #
    def _run_live_expect_fail(self, channel_model, env_driver, phases):
        from experiments.runner import ExperimentRunner
        c = _MinCoord()
        r = ExperimentRunner(coordinator=c)
        r.channel_model = channel_model
        r.env_driver = env_driver
        r.phases = phases
        with self.assertRaises((AxisOverlapError, ValueError)):
            r.run_live(["llm_with_history"], trials=1)
        self.assertEqual(c.executor.writes, [])      # ZERO reset/action writes
        self.assertEqual(r._env_apply_log, [])       # NO fabricated env evidence

    def test_run_live_insufficient_live_driver_zero_reset(self):
        # approved live driver that LACKS capability for a requested axis ->
        # preflight fails before any reset.
        d = _ExoDriver(supported=(ENVIRONMENT_AXES - {"offered_load_mbps"}))
        self._run_live_expect_fail(None, d, [dict(self.PHASE)])

    def test_run_live_emulated_unsupported_axis_zero_reset(self):
        class _Chan:
            def set_environment(self, *a, **k):
                pass
        ph = dict(self.PHASE)
        ph["ue_position"] = 1.0          # ChannelModel cannot apply this
        self._run_live_expect_fail(_Chan(), None, [ph])

    def test_run_live_nonfinite_or_bool_phase_value_zero_reset(self):
        class _Chan:
            def set_environment(self, *a, **k):
                pass
        for bad in (float("nan"), float("inf"), True):
            ph = dict(self.PHASE)
            ph["serving_gain"] = bad     # bool / NaN / Inf rejected at preflight
            self._run_live_expect_fail(_Chan(), None, [ph])

    # -- approved driver applies ONLY environment axes --------------------- #
    def test_approved_driver_applies_only_environment_axes(self):
        d = _ExoDriver()
        r = self._live_runner(d)
        r._apply_phase_environment(dict(self.PHASE), 0, "p0:Nominal")
        self.assertEqual(d.calls, ["p0:Nominal"])
        self.assertEqual(len(r._env_apply_log), 1)
        rec = r._env_apply_log[0]
        self.assertEqual(rec["phase_label"], "p0:Nominal")
        self.assertGreater(rec["applied_monotonic_s"], 0)

    def test_driver_writing_action_axis_is_rejected(self):
        d = _ExoDriver(action_axis=True)     # applies rfatt (an action knob)
        r = self._live_runner(d)
        with self.assertRaises(AxisOverlapError):
            r._apply_phase_environment(dict(self.PHASE), 0, "p0:Nominal")
        self.assertEqual(r._env_apply_log, [])   # NO record on a failed apply

    def test_partial_driver_cannot_silently_drop_axis(self):
        d = _ExoDriver(drop="offered_load_mbps")
        r = self._live_runner(d)
        with self.assertRaises(AxisOverlapError):
            r._apply_phase_environment(dict(self.PHASE), 0, "p0:Nominal")
        self.assertEqual(r._env_apply_log, [])

    def test_insufficient_capability_causes_zero_driver_apply(self):
        # driver does NOT support offered_load_mbps -> capability check fails
        # BEFORE apply -> the driver's apply() is NEVER called (zero apply).
        d = _ExoDriver(supported=(ENVIRONMENT_AXES - {"offered_load_mbps"}))
        r = self._live_runner(d)
        with self.assertRaises(AxisOverlapError):
            r._apply_phase_environment(dict(self.PHASE), 0, "p0:Nominal")
        self.assertEqual(d.calls, [])            # zero apply
        self.assertEqual(r._env_apply_log, [])

    def test_wrong_reported_value_is_rejected(self):
        d = _ExoDriver(wrong="offered_load_mbps")
        r = self._live_runner(d)
        with self.assertRaises(AxisOverlapError):
            r._apply_phase_environment(dict(self.PHASE), 0, "p0:Nominal")
        self.assertEqual(r._env_apply_log, [])

    def test_extra_applied_axis_is_rejected(self):
        d = _ExoDriver(extra=("ue_position", 1.0))   # unrequested env axis
        r = self._live_runner(d)
        with self.assertRaises(AxisOverlapError):
            r._apply_phase_environment(dict(self.PHASE), 0, "p0:Nominal")
        self.assertEqual(r._env_apply_log, [])

    # -- emulated capability: unsupported axis fails (not silently ignored) - #
    def test_emulated_unsupported_axis_fails(self):
        from experiments.runner import ExperimentRunner
        r = ExperimentRunner(coordinator=None)

        class _Chan:
            def set_environment(self, *a, **k):
                pass
        r.channel_model = _Chan()
        ph = dict(self.PHASE)
        ph["ue_position"] = 2.0          # ChannelModel cannot apply this
        with self.assertRaises(AxisOverlapError):
            r._apply_phase_environment(ph, 0, "p0:Nominal")
        self.assertEqual(r._env_apply_log, [])

    def test_emulated_non_callable_set_environment_fails(self):
        # a ChannelModel whose set_environment is NOT callable must fail closed,
        # never silently no-op.
        from experiments.runner import ExperimentRunner
        r = ExperimentRunner(coordinator=None)

        class _BadChan:
            set_environment = None       # present but NOT callable
        r.channel_model = _BadChan()
        with self.assertRaises(AxisOverlapError):
            r._apply_phase_environment(dict(self.PHASE), 0, "p0:Nominal")
        self.assertEqual(r._env_apply_log, [])

    # -- action-axis key in a phase dict is rejected (never silently ignored) #
    def test_generator_rejects_action_axis_phase_key(self):
        with self.assertRaises(AxisOverlapError):
            generate_environment_trace(
                [{"name": "N", "serving_gain": 0, "neighbor_gain": 0,
                  "rfatt": 5.0}], "b", 1)          # rfatt is an ACTION knob

    def test_run_live_action_axis_phase_key_zero_reset(self):
        class _Chan:
            def set_environment(self, *a, **k):
                pass
        ph = {"name": "N", "serving_gain": 0, "neighbor_gain": 0,
              "power_offset": 3.0}                 # action knob in a phase
        self._run_live_expect_fail(_Chan(), None, [ph])

    # -- stream forgery is rejected ---------------------------------------- #
    def test_environment_event_forged_stream_rejected(self):
        with self.assertRaises(AxisOverlapError):
            EnvironmentEvent("e", 0.0, "offered_load_mbps", 1.0,
                             phase_idx=0, phase_name="N", phase_label="p0:N",
                             stream="action")

    def test_action_event_forged_stream_rejected(self):
        with self.assertRaises(AxisOverlapError):
            ActionEvent("a", 0.0, "power_offset", 1.0, target="bs1",
                        stream="environment")

    # -- lossless hash: a sub-rounding difference changes the hash --------- #
    def test_subrounding_difference_changes_hash(self):
        def _tr(val):
            ev = EnvironmentEvent("e", 0.0, "offered_load_mbps", val,
                                  phase_idx=0, phase_name="N",
                                  phase_label="p0:N")
            return EnvironmentTrace("b", 1, (ev,))
        # 1e-10 was collapsed by the old round(value, 9); lossless content keeps
        # it distinct.
        self.assertNotEqual(_tr(1.0).canonical_hash(),
                            _tr(1.0 + 1e-10).canonical_hash())

    # -- same seed/phase, different block label -> same semantic hash ------ #
    def test_same_seed_different_block_label_same_hash(self):
        p = [{"name": "Nominal", "serving_gain": 3, "neighbor_gain": 0}]
        self.assertEqual(
            generate_environment_trace(p, "blockA", seed=1).canonical_hash(),
            generate_environment_trace(p, "blockB", seed=1).canonical_hash())

    # -- duplicate Nominal phases have DISTINCT identity + apply evidence --- #
    def test_duplicate_nominal_phases_have_distinct_identity(self):
        phases = [
            {"name": "Nominal", "serving_gain": 3, "neighbor_gain": 0},
            {"name": "Degradation", "serving_gain": -2, "neighbor_gain": 1},
            {"name": "Impairment", "serving_gain": -5, "neighbor_gain": 2},
            {"name": "Recovery", "serving_gain": 0, "neighbor_gain": 0},
            {"name": "Nominal", "serving_gain": 3, "neighbor_gain": 0},
        ]
        tr = generate_environment_trace(phases, "blk", 1)
        labels = {e.phase_label for e in tr.events}
        self.assertIn("p0:Nominal", labels)
        self.assertIn("p4:Nominal", labels)             # DISTINCT from p0
        # and the emulated driver stamps DISTINCT apply evidence per identity
        from experiments.runner import ExperimentRunner
        from experiments.paired_runner import PairedBlockRunner

        class _Chan:
            def set_environment(self, *a, **k):
                pass
        r = ExperimentRunner(coordinator=None)
        r.channel_model = _Chan()
        for i, ph in enumerate(PairedBlockRunner._phases_from_trace(tr)):
            r._apply_phase_environment(ph, ph["phase_idx"], ph["phase_label"])
        applied_labels = [rec["phase_label"] for rec in r._env_apply_log]
        self.assertIn("p0:Nominal", applied_labels)
        self.assertIn("p4:Nominal", applied_labels)
        self.assertEqual(len(applied_labels), len(set(applied_labels)))  # distinct

    # -- missing / duplicate apply evidence fails (never fabricates time) --- #
    def _real_paired(self):
        from experiments.paired_runner import PairedBlockRunner
        return PairedBlockRunner(["llm_with_history"], n_blocks=1, master_seed=1,
                                 trials_per_block=1, steps_per_phase=1,
                                 data_origin=DataOrigin.EMULATED_PIPELINE)

    def test_missing_phase_apply_evidence_fails(self):
        import types
        r = self._real_paired()
        tr = generate_environment_trace(
            [{"name": "Nominal", "serving_gain": 3, "neighbor_gain": 0}], "b", 1)
        r._pr = types.SimpleNamespace(_env_apply_log=[])   # NO evidence
        with self.assertRaises(PaperExportError):
            r._stamp_env_stream(tr)

    def test_duplicate_phase_apply_evidence_fails(self):
        import types
        r = self._real_paired()
        tr = generate_environment_trace(
            [{"name": "Nominal", "serving_gain": 3, "neighbor_gain": 0}], "b", 1)
        dup = [{"phase_label": "p0:Nominal", "applied_monotonic_s": 1.0},
               {"phase_label": "p0:Nominal", "applied_monotonic_s": 2.0}]
        r._pr = types.SimpleNamespace(_env_apply_log=dup)
        with self.assertRaises(PaperExportError):
            r._stamp_env_stream(tr)

    def test_invalid_apply_timestamp_fails(self):
        import types
        r = self._real_paired()
        tr = generate_environment_trace(
            [{"name": "Nominal", "serving_gain": 3, "neighbor_gain": 0}], "b", 1)
        bad = [{"phase_label": "p0:Nominal", "applied_monotonic_s": 0.0}]  # <= 0
        r._pr = types.SimpleNamespace(_env_apply_log=bad)
        with self.assertRaises(PaperExportError):
            r._stamp_env_stream(tr)

    def test_pipeline_multi_trial_evidence_fails_closed(self):
        from experiments.paired_runner import PairedBlockRunner
        import types
        r = PairedBlockRunner(["llm_with_history"], n_blocks=1, master_seed=1,
                              trials_per_block=2, steps_per_phase=1,
                              data_origin=DataOrigin.EMULATED_PIPELINE)
        tr = generate_environment_trace(
            [{"name": "Nominal", "serving_gain": 3, "neighbor_gain": 0}], "b", 1)
        r._pr = types.SimpleNamespace(_env_apply_log=[
            {"phase_label": "p0:Nominal", "applied_monotonic_s": 1.0}])
        with self.assertRaises(PaperExportError):
            r._stamp_env_stream(tr)          # trials_per_block>1 -> fail closed

    # -- full SHA-256 format + semantic sensitivity ------------------------ #
    def test_full_sha256_format_and_sensitivity(self):
        p = [{"name": "Nominal", "serving_gain": 3, "neighbor_gain": 0}]
        h = generate_environment_trace(p, "b", 1).canonical_hash()
        self.assertTrue(h.startswith("envtrace-sha256-"))
        self.assertEqual(len(h[len("envtrace-sha256-"):]), 64)   # full SHA-256
        int(h[len("envtrace-sha256-"):], 16)                     # valid hex
        # identical content -> identical hash; a changed phase name -> different.
        h2 = generate_environment_trace(p, "b", 1).canonical_hash()
        self.assertEqual(h, h2)
        p3 = [{"name": "RENAMED", "serving_gain": 3, "neighbor_gain": 0}]
        self.assertNotEqual(
            h, generate_environment_trace(p3, "b", 1).canonical_hash())

    def test_trace_rejects_optional_phase_identity(self):
        # a canonical TRACE requires a full phase identity on every event.
        ev = EnvironmentEvent("e", 0.0, "offered_load_mbps", 12.0)  # no identity
        with self.assertRaises(ValueError):
            EnvironmentTrace("b", 1, (ev,))

    def test_cross_event_trace_identity_inconsistency_fails(self):
        # two events share phase_idx 0 but carry DIFFERENT identities: each event
        # is self-valid, but the TRACE itself REJECTS the inconsistency at
        # construction (never collapses two phases under one index).
        e0 = EnvironmentEvent("x0", 0.0, "channel_serving_gain_db", 3.0,
                              phase_idx=0, phase_name="Nominal",
                              phase_label="p0:Nominal")
        e1 = EnvironmentEvent("x1", 0.0, "channel_neighbor_gain_db", 0.0,
                              phase_idx=0, phase_name="Other",
                              phase_label="p0:Other")
        with self.assertRaises(ValueError):
            EnvironmentTrace("b", 1, (e0, e1))

    # -- all methods share ONE trace; env/action streams stay disjoint ----- #
    def test_all_methods_share_trace_streams_disjoint(self):
        r = PairedBlockRunner(ALL_METHODS, n_blocks=1, master_seed=9,
                              trials_per_block=1, steps_per_phase=2)
        res = r.run()
        hashes = {rec["environment_trace_hash"] for rec in res["records"]}
        self.assertEqual(len(hashes), 1)                 # one trace, every method
        for rec in res["records"]:
            for ev in rec["environment_stream"]:
                self.assertIn(ev["axis"], ENVIRONMENT_AXES)
                self.assertNotIn(ev["axis"], ACTION_AXES)
            for ev in rec["action_stream"]:
                self.assertIn(ev["axis"], ACTION_AXES)
                self.assertNotIn(ev["axis"], ENVIRONMENT_AXES)

    def test_paired_runner_injects_env_driver_identity(self):
        # P0-19: the injected exogenous driver reaches the shared pipeline runner
        # (a live paired run would otherwise fail closed with driver=None), and
        # the SAME driver identity is used for the new AND the reused runner.
        d = _ExoDriver()
        r = PairedBlockRunner(["llm_with_history"], n_blocks=1, master_seed=1,
                              trials_per_block=1, steps_per_phase=1,
                              data_origin=DataOrigin.EMULATED_PIPELINE,
                              env_driver=d)

        class _C:
            executor = object()
        c = _C()
        pr1 = r._pipeline_runner(c)
        self.assertIs(pr1.env_driver, d)          # injected into the new runner
        pr2 = r._pipeline_runner(c)
        self.assertIs(pr2, pr1)                    # runner reused
        self.assertIs(pr2.env_driver, d)          # SAME driver on reuse


# --------------------------------------------------------------------------- #
# P0-20 paired baselines + raw evidence                                      #
# --------------------------------------------------------------------------- #

class PairedRunnerTest(unittest.TestCase):
    def _run(self, **kw):
        return PairedBlockRunner(ALL_METHODS, n_blocks=kw.pop("n_blocks", 2),
                                 master_seed=7, trials_per_block=1,
                                 steps_per_phase=2, **kw).run()

    def test_live_methods_are_not_silently_skipped(self):
        res = self._run()
        for blk in res["blocks"]:
            self.assertTrue(blk["all_methods_present"])
            self.assertEqual(set(blk["n_records_by_method"]), set(ALL_METHODS))

    def test_synthetic_results_are_tagged_non_ota(self):
        res = self._run()
        self.assertEqual(res["data_origin"], "synthetic_harness")
        self.assertTrue(all(r["data_origin"] == "synthetic_harness"
                            for r in res["records"]))

    def test_paired_block_uses_identical_trace(self):
        res = self._run()
        for blk in res["blocks"]:
            hs = {r["environment_trace_hash"] for r in res["records"]
                  if r["block_id"] == blk["block_id"]}
            self.assertEqual(len(hs), 1)

    def test_deterministic_method_order_reproducible(self):
        o1 = deterministic_method_order(ALL_METHODS, 7, 0)
        o2 = deterministic_method_order(ALL_METHODS, 7, 0)
        self.assertEqual(o1, o2)
        self.assertEqual(sorted(o1), sorted(ALL_METHODS))

    def test_supports_twenty_blocks(self):
        res = PairedBlockRunner(ALL_METHODS, n_blocks=20, master_seed=1,
                                trials_per_block=1, steps_per_phase=1).run()
        self.assertEqual(res["n_blocks"], 20)
        self.assertEqual(len(res["blocks"]), 20)


class RawSchemaTest(unittest.TestCase):
    def _records(self):
        return PairedBlockRunner(ALL_METHODS, n_blocks=2, master_seed=3,
                                 trials_per_block=1, steps_per_phase=2).run()

    def test_repeated_episodes_have_distinct_ids(self):
        recs = self._records()["records"]
        ids = [r["episode_id"] for r in recs]
        self.assertEqual(len(ids), len(set(ids)))

    def test_raw_run_is_integration_only_and_excluded(self):
        # P1-6 (item 3): EVERY raw run() result is explicitly integration_only /
        # excluded_from_paper / not paper_ready in the artifact itself - the origin
        # label alone never opts a raw run into the paper performance table.
        res = self._records()
        self.assertEqual(res["paper_eligibility"], "integration_only")
        self.assertIs(res["excluded_from_paper"], True)
        self.assertIs(res["paper_ready"], False)

    def test_actuation_trial_id_exists_only_for_s3(self):
        recs = self._records()["records"]
        for r in recs:
            if r["terminal_outcome"] == "negotiation_only":
                self.assertIsNone(r["actuation_trial_id"])
                self.assertEqual(r["action_stream"], [])
            if r["actuation_trial_id"]:
                self.assertTrue(r["action_stream"])   # non-empty for S3

    def test_raw_record_contains_provenance_chain(self):
        recs = self._records()["records"]
        chain = ["experiment_run_id", "method_block_id", "fsm_step_id",
                 "episode_id", "pending_intent_id", "cycle_id", "proposal_id",
                 "master_seed", "block_seed", "method_order",
                 "environment_trace_hash", "data_origin", "model_id"]
        for r in recs:
            for k in chain:
                self.assertIn(k, r)
                self.assertIsNotNone(r[k])
            # separate typed streams present
            self.assertIn("environment_stream", r)
            self.assertIn("action_stream", r)

    def test_action_stream_has_applied_timestamps(self):
        recs = self._records()["records"]
        for r in recs:
            for a in r["action_stream"]:
                self.assertIsNotNone(a["applied_monotonic_s"])

    def test_finite_json_no_nan(self):
        res = self._records()
        s = dumps_finite(res)          # allow_nan=False internally
        json.loads(s)
        self.assertNotIn("NaN", s)
        self.assertNotIn("Infinity", s)
        # NaN sanitised to null, never a fake zero
        self.assertIsNone(sanitize_finite(float("nan")))
        self.assertIsNone(sanitize_finite(float("inf")))


def _valid_raw(**over):
    """A FULLY VALID committed-proposal RawEvidenceRecord (explicit consistent
    facts). Overridable per test to exercise each invariant boundary."""
    base = dict(
        experiment_run_id="r", method_block_id="m", fsm_step_id="f",
        episode_id="e", pending_intent_id="pi", cycle_id="c",
        proposal_id="p", actuation_trial_id=None, method="m", block_id="b",
        block_index=0, master_seed=1, block_seed=1, method_order=["m"],
        method_order_index=0, monotonic_seq=1, model_id="m",
        model_version="m", prompt_hash="h", intent_type="throughput_goal",
        intent_target={"target_value": 8.0},
        intent_scope={"ue_ids": ["ue1"], "bs_ids": []}, raw_confidence=0.5,
        calibrated_probability=None, schema_verdict=True,
        proposed_action=None, clipped_action=None, enforced_action=None,
        readback_action=None, trial_trajectory=[], nego_trajectory=[],
        budget_before=None, budget_reserved=None, budget_debited=None,
        budget_after=None, terminal_outcome="commit_original",
        terminal_reason=None,
        proposal_generated=True, was_parsed_intent=True,
        phase_idx=0, phase_name="Nominal", phase_label="p0:Nominal",
        episode_monotonic_s=1.0)
    base.update(over)
    return RawEvidenceRecord(**base)


class ActionStreamContractTest(unittest.TestCase):
    def _rec(self, **over):
        return _valid_raw(**over)

    def test_actuation_without_action_stream_is_rejected(self):
        with self.assertRaises(ValueError):
            self._rec(actuation_trial_id="e:act", action_stream=[])

    def test_action_event_without_applied_time_is_rejected(self):
        ev = {"event_id": "w", "applied_monotonic_s": None, "axis": "power_offset"}
        with self.assertRaises(ValueError):
            self._rec(actuation_trial_id="e:act", action_stream=[ev])

    def test_action_stream_without_actuation_is_rejected(self):
        ev = {"event_id": "w", "applied_monotonic_s": 1.0, "axis": "power_offset"}
        with self.assertRaises(ValueError):
            self._rec(actuation_trial_id=None, action_stream=[ev])

    def test_applied_monotonic_must_be_real_finite_positive(self):
        # P1-6 (item 5): applied_monotonic_s is REAL monotonic evidence - a
        # non-bool real finite value > 0. bool/string/NaN/inf/0/negative (or
        # anything that would sanitise to null) FAILS CLOSED.
        for bad in (None, True, "5.0", float("nan"), float("inf"), 0.0, -1.0):
            ev = {"event_id": "w", "applied_monotonic_s": bad,
                  "axis": "power_offset"}
            with self.assertRaises(ValueError):
                self._rec(actuation_trial_id="e:act", action_stream=[ev])
        # a real finite > 0 timestamp is accepted
        ok = {"event_id": "w", "applied_monotonic_s": 5.0, "axis": "power_offset"}
        self._rec(actuation_trial_id="e:act", action_stream=[ok])


class RawEvidenceInvariantTest(unittest.TestCase):
    # ---- valid baselines: committed / pre-proposal / pre-parse ------------- #
    def test_valid_committed_record(self):
        r = _valid_raw()
        self.assertTrue(r.proposal_generated)
        self.assertTrue(r.was_parsed_intent)
        self.assertEqual(r.phase_label, "p0:Nominal")

    def test_valid_pre_proposal_record(self):
        # a cycle ran but NO proposal was generated: proposal_id/schema_verdict
        # are None (cycle_id MAY exist); the intent was still parsed.
        r = _valid_raw(proposal_generated=False, proposal_id=None,
                       schema_verdict=None, prompt_hash=None)
        self.assertFalse(r.proposal_generated)
        self.assertIsNone(r.proposal_id)
        self.assertIsNone(r.schema_verdict)
        self.assertEqual(r.cycle_id, "c")       # a cycle_id may still exist

    def test_valid_pre_parse_record(self):
        # a pre-parse terminal: no intent parsed -> all four intent fields None
        # (and no proposal either).
        r = _valid_raw(was_parsed_intent=False, pending_intent_id=None,
                       intent_type=None, intent_target=None, intent_scope=None,
                       proposal_generated=False, proposal_id=None,
                       schema_verdict=None, prompt_hash=None)
        self.assertFalse(r.was_parsed_intent)
        self.assertIsNone(r.pending_intent_id)
        self.assertIsNone(r.intent_target)

    # ---- phase invariants -------------------------------------------------- #
    def test_phase_idx_bool_rejected(self):
        with self.assertRaises(ValueError):
            _valid_raw(phase_idx=True, phase_label="pTrue:Nominal")

    def test_phase_idx_negative_rejected(self):
        with self.assertRaises(ValueError):
            _valid_raw(phase_idx=-1, phase_label="p-1:Nominal")

    def test_phase_idx_non_int_rejected(self):
        with self.assertRaises(ValueError):
            _valid_raw(phase_idx=1.0, phase_label="p1.0:Nominal")

    def test_phase_name_empty_rejected(self):
        with self.assertRaises(ValueError):
            _valid_raw(phase_name="", phase_label="p0:")

    def test_phase_label_must_match_exactly(self):
        with self.assertRaises(ValueError):
            _valid_raw(phase_label="Nominal")          # missing p{idx}:
        with self.assertRaises(ValueError):
            _valid_raw(phase_label="p1:Nominal")        # wrong index

    # ---- episode_monotonic_s invariants ------------------------------------ #
    def test_episode_monotonic_none_rejected(self):
        with self.assertRaises(ValueError):
            _valid_raw(episode_monotonic_s=None)

    def test_episode_monotonic_zero_rejected(self):
        with self.assertRaises(ValueError):
            _valid_raw(episode_monotonic_s=0.0)

    def test_episode_monotonic_negative_rejected(self):
        with self.assertRaises(ValueError):
            _valid_raw(episode_monotonic_s=-1.0)

    def test_episode_monotonic_nan_inf_rejected(self):
        with self.assertRaises(ValueError):
            _valid_raw(episode_monotonic_s=float("nan"))
        with self.assertRaises(ValueError):
            _valid_raw(episode_monotonic_s=float("inf"))

    # ---- proposal_generated invariants ------------------------------------- #
    def test_proposal_generated_must_be_bool(self):
        with self.assertRaises(ValueError):
            _valid_raw(proposal_generated=None)
        with self.assertRaises(ValueError):
            _valid_raw(proposal_generated="true")

    def test_generated_requires_prompt_hash(self):
        with self.assertRaises(ValueError):
            _valid_raw(prompt_hash=None)
        with self.assertRaises(ValueError):
            _valid_raw(prompt_hash="")

    def test_generated_requires_cycle_id(self):
        with self.assertRaises(ValueError):
            _valid_raw(cycle_id=None)

    def test_generated_requires_proposal_id(self):
        with self.assertRaises(ValueError):
            _valid_raw(proposal_id=None)

    def test_generated_requires_bool_schema_verdict(self):
        with self.assertRaises(ValueError):
            _valid_raw(schema_verdict=None)

    def test_not_generated_requires_none_proposal_id(self):
        with self.assertRaises(ValueError):
            _valid_raw(proposal_generated=False, proposal_id="p",
                       schema_verdict=None, prompt_hash=None)

    def test_not_generated_requires_none_schema_verdict(self):
        with self.assertRaises(ValueError):
            _valid_raw(proposal_generated=False, proposal_id=None,
                       schema_verdict=True, prompt_hash=None)

    # ---- was_parsed_intent invariants -------------------------------------- #
    def test_was_parsed_must_be_bool(self):
        with self.assertRaises(ValueError):
            _valid_raw(was_parsed_intent=None)

    def test_parsed_requires_pending_intent_id(self):
        with self.assertRaises(ValueError):
            _valid_raw(pending_intent_id=None)

    def test_parsed_requires_intent_type(self):
        with self.assertRaises(ValueError):
            _valid_raw(intent_type=None)

    def test_parsed_requires_dict_target(self):
        with self.assertRaises(ValueError):
            _valid_raw(intent_target="x")               # string, not dict
        with self.assertRaises(ValueError):
            _valid_raw(intent_target=None)

    def test_parsed_requires_dict_scope(self):
        with self.assertRaises(ValueError):
            _valid_raw(intent_scope=["ue1"])            # list, not dict
        with self.assertRaises(ValueError):
            _valid_raw(intent_scope=None)

    def test_unparsed_requires_all_four_none(self):
        # any one of the four intent fields present with was_parsed_intent=False
        # is inconsistent and rejected (never silently normalized).
        for over in ({"pending_intent_id": "pi"}, {"intent_type": "t"},
                     {"intent_target": {"x": 1}}, {"intent_scope": {"x": 1}}):
            kw = dict(was_parsed_intent=False, pending_intent_id=None,
                      intent_type=None, intent_target=None, intent_scope=None,
                      proposal_generated=False, proposal_id=None,
                      schema_verdict=None, prompt_hash=None)
            kw.update(over)
            with self.assertRaises(ValueError):
                _valid_raw(**kw)


class PaperGateTest(unittest.TestCase):
    def _res(self):
        return PairedBlockRunner(ALL_METHODS, n_blocks=1, master_seed=1,
                                 trials_per_block=1, steps_per_phase=1).run()

    def test_non_ota_data_is_excluded_from_paper_export(self):
        r = PairedBlockRunner(ALL_METHODS, n_blocks=1, master_seed=1,
                              trials_per_block=1, steps_per_phase=1)
        res = r.run()
        full = PaperGateEvidence(**{g: True for g in PaperGateEvidence.REQUIRED})
        with self.assertRaises(PaperExportError):
            r.paper_export(gate=full, result=res)   # synthetic origin rejected

    def test_paper_export_requires_explicit_gate(self):
        r = PairedBlockRunner(ALL_METHODS, n_blocks=1, master_seed=1,
                              trials_per_block=1, steps_per_phase=1)
        res = r.run()
        with self.assertRaises(PaperExportError):
            r.paper_export(gate=None, result=res)
        with self.assertRaises(PaperExportError):
            r.paper_export(gate=PaperGateEvidence(), result=res)  # empty gate

    def test_origin_does_not_prove_provisioning(self):
        res = self._res()
        for rec in res["records"]:
            self.assertFalse(
                rec["hardware_state"].get("provisioning_proven_by_origin"))

    def test_live_ota_routing_contract_with_full_bound_gate_admits(self):
        # POSITIVE path (P0-20-F): a TEST-ONLY live-contract SEAM (channel_model
        # =None, approved env_driver; NOT real live/OTA/hardware data) with a
        # full, ref-backed, run-BOUND gate admits. The result is bound to THIS
        # runner via its immutable attestation and is passed UNMODIFIED. This
        # exercises the export gating/routing contract only.
        r, res, gate = _live_contract_seam_fixture()
        try:
            out = r.paper_export(gate=gate, result=res)
            self.assertEqual(out["data_origin"], "live_ota")
            self.assertGreaterEqual(out["n_blocks"], 20)
            self.assertGreater(out["n_admitted"], 0)
            # P1-6 (item 3): ONLY the successful export payload is a paper
            # PERFORMANCE artifact; the raw run() result (even live_ota) stayed
            # integration_only/excluded/not-ready.
            self.assertEqual(out["paper_eligibility"], "paper_performance")
            self.assertIs(out["excluded_from_paper"], False)
            self.assertIs(out["paper_ready"], True)
            self.assertEqual(res["paper_eligibility"], "integration_only")
            self.assertIs(res["excluded_from_paper"], True)
            self.assertIs(res["paper_ready"], False)
        finally:
            _shutdown_fixture(r)


# --------------------------------------------------------------------------- #
# P0-20 method-config, state-reset and paper-export counterexamples           #
# --------------------------------------------------------------------------- #

class _CfgCal:
    def __init__(self, mode="online"):
        self.mode = mode

    def reset(self):
        pass


class _CfgMgr:
    def __init__(self):
        self.active = "llm-x"

    def active_backend_name(self):
        return self.active


class _CfgCoord:
    """Minimal coordinator for _configure_method unit tests (proposer identity +
    calibrator mode) - no heavy emulation."""
    def __init__(self, noop_setter=False):
        self.calibrator = _CfgCal()
        self.llm_manager = _CfgMgr()
        self._noop = noop_setter

    def set_llm_backend(self, name):
        if self._noop:
            return True                # truthy but does NOT change active
        self.llm_manager.active = name
        return True

    def _active_model_id(self):
        return self.llm_manager.active


def _cfg_runner(coord, entry="llm-x", entry_mode="online"):
    from experiments.runner import ExperimentRunner
    r = ExperimentRunner(coordinator=coord)
    r._llm_backend_name = entry
    r._entry_cal_mode = entry_mode
    r._adaptive_cal_mode = entry_mode if entry_mode != "fixed" else "online"
    r._apply_history_mode = lambda c, m: None      # stub history for unit test
    return r


# -- test-only QUALIFIED LIVE environment driver + live collector ----------- #
class _QualifiedLiveEnvDriver(ExogenousEnvironmentDriver):
    """A TEST-ONLY approved env driver providing the LIVE-path state-driver
    capability (capture_state/restore_state/reseed) + apply. It is NOT a real
    attenuator/load/position rig and NOT an emulated ChannelModel - it exists only
    to close the LIVE routing contract (channel_model=None) for unit tests; it
    produces NO real/OTA measurement."""
    supported_axes = ENVIRONMENT_AXES
    approved = True
    name = "test-qualified-live-env-driver"

    def __init__(self):
        self._seed = 1
        self._applied = None

    def apply(self, phase, phase_idx, phase_label):
        applied = _ExoDriver._requested(phase)
        self._applied = tuple(sorted(applied.items()))
        return applied

    def capture_state(self):
        return (self._seed, self._applied)

    def restore_state(self, s):
        self._seed, self._applied = s

    def reseed(self, seed):
        self._seed = int(seed)
        self._applied = None

    def measured_throughput(self):
        # deterministic, and BELOW the I2 target so the coordinator triggers a
        # coordination episode each method run (a NONZERO pipeline-contract
        # record per method - NOT a real/OTA measurement); stable after restore
        # so paired methods observe the SAME start state. This is a LIVE_OTA
        # fixture, so the target it must stay under is the LIVE one
        # (config.LIVE_I2_TARGET_MBPS), NOT the 8.0 offline default - derived
        # from the constant so a re-measured hardware target cannot silently
        # turn these runs into zero-episode (zero-record) runs.
        base = cfgmod.LIVE_I2_TARGET_MBPS * (0.3 + 0.1 * (self._seed % 3))
        for k, v in (self._applied or ()):
            if k == "channel_serving_gain_db":
                base += v * 0.05
        return base


class _LiveDriverCollector:
    """A TEST-ONLY collector whose KPIs are derived from the test env-driver's
    state - NOT a real/OTA measurement and NOT an emulated ChannelModel. It only
    closes the LIVE routing contract so the export gating can be exercised."""
    simulation_mode = False
    emulated = False

    def __init__(self, driver, ue_ids):
        self._drv = driver
        self._ue_ids = list(ue_ids)
        self.probe_config = None

    def _metrics(self):
        from collectors.multi_ue_collector import UEMetrics
        tp = self._drv.measured_throughput()
        return {ue: UEMetrics(ue_id=ue, attached=True,
                              throughput_mbps=round(tp, 3), rsrp=-80.0,
                              sinr=20.0) for ue in self._ue_ids}

    def collect_all(self):
        return self._metrics()

    def get_throughput_all(self, duration=2.0):
        tp = round(self._drv.measured_throughput(), 3)
        return {ue: tp for ue in self._ue_ids}

    def set_probe_config(self, pc):
        self.probe_config = pc

    def set_scheduler(self, s):
        pass

    def shutdown(self):
        pass


def _live_contract_seam_fixture(n_blocks=20):
    """A TEST-ONLY live-contract SEAM (NOT genuine live/OTA/hardware, NOT real
    performance evidence, never stored or claimed as such). It exercises ONLY the
    paper_export ORIGIN-ROUTING + attestation contract for the LIVE_OTA path:
    channel_model=None with an approved env_driver as the state driver (so the
    emulated ChannelModel is NEVER used and never relabeled as live). Records come
    from the pipeline wiring, not the synthetic profile generator; the coordinator
    plumbing is a build_emulated_coordinator + test doubles. Returns
    (runner, result, gate)."""
    from experiments.emulation import build_emulated_coordinator
    from experiments.topology import three_ue_shared_topology
    t = three_ue_shared_topology()
    c, _ch = build_emulated_coordinator(seed=7, tau_trial_s=0.0, topology=t)
    driver = _QualifiedLiveEnvDriver()
    # a test double collector driven by the env_driver state (NOT a real/OTA
    # measurement and NOT the emulated channel) - it only closes the LIVE routing
    # contract so the export gating can be exercised.
    c.ue_collector = _LiveDriverCollector(driver, t.ue_ids())
    r = PairedBlockRunner(ALL_METHODS, topology=t, n_blocks=n_blocks,
                          master_seed=7, trials_per_block=1, steps_per_phase=1,
                          data_origin=DataOrigin.LIVE_OTA, coordinator=c,
                          env_driver=driver)
    r.channel_model = None                          # LIVE routing: no channel
    res = r.run()
    att = res["run_attestation"]
    refs = {g: f"attested:{g}" for g in PaperGateEvidence.REQUIRED}
    gate = PaperGateEvidence(
        evidence_refs=refs,
        attested_experiment_run_id=att["experiment_run_id"],
        attested_run_digest=att["digest"],
        **{g: True for g in PaperGateEvidence.REQUIRED})
    return r, res, gate


def _shutdown_fixture(r):
    try:
        c = getattr(r, "coordinator", None)
        if c is not None and hasattr(c, "stop"):
            c.stop()
    except Exception:
        pass


class P0_20MethodConfigTest(unittest.TestCase):
    # -- (A) canonical method set --------------------------------------------
    def test_unknown_method_rejected(self):
        from experiments.paired_runner import validate_method_set
        with self.assertRaises(ValueError):
            validate_method_set(["rule_based", "not_a_method"])
        with self.assertRaises(ValueError):
            PairedBlockRunner(["mystery_method"], n_blocks=1)

    def test_duplicate_method_rejected(self):
        from experiments.paired_runner import validate_method_set
        with self.assertRaises(ValueError):
            validate_method_set(["adaptive", "adaptive"])

    def test_synthetic_profile_rejects_unknown_and_aliases_ablation(self):
        from experiments.synthetic import profile_for_method, METHOD_PROFILES
        self.assertIs(profile_for_method("fixed_threshold"),
                      METHOD_PROFILES["llm_with_history"])
        self.assertIs(profile_for_method("adaptive"),
                      METHOD_PROFILES["llm_with_history"])
        with self.assertRaises(ValueError):
            profile_for_method("nonsense")

    class _WriteCoord:
        def __init__(self):
            self.writes = []
            self.executor = self

        def reset_all_axes(self):
            self.writes.append("reset")

        def set_probe_config(self, pc):
            pass

    def test_direct_run_live_rejects_unknown_method_zero_write(self):
        # a non-canonical method is rejected BEFORE any executor write.
        from experiments.runner import ExperimentRunner
        c = self._WriteCoord()
        r = ExperimentRunner(coordinator=c)
        with self.assertRaises(ValueError):
            r.run_live(["definitely_unknown"], trials=1)
        self.assertEqual(c.writes, [])

    def test_direct_run_live_rejects_duplicate_method_zero_write(self):
        from experiments.runner import ExperimentRunner
        c = self._WriteCoord()
        r = ExperimentRunner(coordinator=c)
        with self.assertRaises(ValueError):
            r.run_live(["adaptive", "adaptive"], trials=1)   # duplicate
        self.assertEqual(c.writes, [])

    # -- (B) calibrator mode --------------------------------------------------
    def test_adaptive_uses_online_learning_mode(self):
        c = _CfgCoord()
        _cfg_runner(c)._configure_method("adaptive")
        self.assertEqual(c.calibrator.mode, "online")   # NOT invalid 'adaptive'

    def test_fixed_threshold_uses_fixed_mode(self):
        c = _CfgCoord()
        _cfg_runner(c)._configure_method("fixed_threshold")
        self.assertEqual(c.calibrator.mode, "fixed")

    def test_no_calibrator_mode_leak_across_methods(self):
        c = _CfgCoord()
        r = _cfg_runner(c)
        r._configure_method("fixed_threshold")
        r._configure_method("rule_based")               # reset to entry, no leak
        self.assertEqual(c.calibrator.mode, "online")
        r._configure_method("adaptive")
        self.assertEqual(c.calibrator.mode, "online")

    # -- (C) proposer identity verification -----------------------------------
    def test_baseline_active_proposer_verified(self):
        c = _CfgCoord()
        _cfg_runner(c)._configure_method("rule_based")
        self.assertEqual(c.llm_manager.active, "baseline:rule_based")

    def test_baseline_truthy_noop_setter_rejected(self):
        c = _CfgCoord(noop_setter=True)
        with self.assertRaises(RuntimeError):
            _cfg_runner(c)._configure_method("rule_based")

    def test_llm_reset_leaked_baseline_rejected(self):
        c = _CfgCoord(noop_setter=True)
        c.llm_manager.active = "baseline:rl_controller"   # leaked baseline
        with self.assertRaises(RuntimeError):
            _cfg_runner(c)._configure_method("llm_no_history")


class P0_20StateResetTest(unittest.TestCase):
    # -- (2) ChannelModel lossless environment state -------------------------
    def test_channel_model_state_is_lossless(self):
        from experiments.emulation import ChannelModel
        ch = ChannelModel(seed=3)
        ch.set_environment(2.0, -1.0, external_disturbance_db=0.7,
                           offered_load_mbps=9.0)
        snap = ch.capture_state()
        ch.set_environment(-5.0, 4.0, external_disturbance_db=0.0,
                           offered_load_mbps=None)   # mutate ALL env fields
        ch.throughput("gnb1", "gnb2", 0.0, 0.0)      # advance RNG too
        ch.restore_state(snap)
        self.assertEqual(ch.env_gain["gnb1"], 2.0)
        self.assertEqual(ch.env_gain["gnb2"], -1.0)
        self.assertEqual(ch.external_disturbance_db, 0.7)
        self.assertEqual(ch.offered_load_mbps, 9.0)
        self.assertEqual(ch.capture_state(), snap)   # full state restored

    # -- (G) executor sharing -------------------------------------------------
    def test_synthetic_executor_shared_vacuous(self):
        r = PairedBlockRunner(ALL_METHODS, n_blocks=1, master_seed=1,
                              trials_per_block=1, steps_per_phase=1)
        res = r.run()
        self.assertEqual(res["shared_executor_ids"], [])
        self.assertTrue(res["executor_shared"])      # vacuous, synthetic only
        self.assertIsNone(res["block_independence_verified"])

    # -- (1) a no-op / mismatching restore fails closed ----------------------
    def test_verified_reset_fails_on_noop_restore(self):
        # a state driver whose restore_state does NOT actually restore must be
        # caught by the re-capture equality check (not a name-only 'verified').
        from experiments.paired_runner import PairedResetError
        r = PairedBlockRunner(ALL_METHODS, n_blocks=1, master_seed=1,
                              data_origin=DataOrigin.EMULATED_PIPELINE)

        class _Cal:
            pass

        class _Mgr:
            def active_selection_snapshot(self):
                return ("a",)

            def restore_active_selection(self, s):
                pass
            dynamic_backends = {}

        class _Ex:
            def snapshot(self, from_device=True):
                return {}

            def restore(self, s):
                return True

        class _Chan:
            def __init__(self):
                self.s = 0

            def capture_state(self):
                return self.s

            def restore_state(self, s):
                pass                     # NO-OP restore (does not set self.s)

            def reseed(self, x):
                self.s = x

        class _Coord:
            def __init__(self):
                self.llm_manager = _Mgr()
                self.executor = _Ex()
                self.calibrator = _Cal()
                self.history_mode = None

            def history_snapshot(self):
                return ()

            def restore_history_snapshot(self, s):
                pass
        c = _Coord()
        r.coordinator = c
        ch = _Chan()
        r.channel_model = ch
        snap = r._capture_block_state()
        ch.s = 999                       # mutate; restore is a no-op -> mismatch
        with self.assertRaises(PairedResetError):
            r._restore_block_state_verified(snap)

    # -- (B) a NO-OP reseed fails BEFORE any method executes -----------------
    def test_noop_reseed_fails_closed_before_any_method(self):
        # P0-20 Codex hole B: block_independent must not merely trust that reseed
        # was CALLED. A driver whose reseed() is a no-op leaves every block at the
        # SAME env realization, so it FAILS CLOSED with PairedResetError BEFORE a
        # single method runs (ZERO measurements collected).
        from experiments.emulation import build_emulated_coordinator
        from experiments.topology import three_ue_shared_topology
        t = three_ue_shared_topology()
        c, _ch = build_emulated_coordinator(seed=7, tau_trial_s=0.0, topology=t)
        try:
            class _NoOpReseedDriver(_QualifiedLiveEnvDriver):
                def reseed(self, seed):
                    pass                 # NO-OP: blocks would not be independent
            driver = _NoOpReseedDriver()
            c.ue_collector = _LiveDriverCollector(driver, t.ue_ids())
            r = PairedBlockRunner(ALL_METHODS, topology=t, n_blocks=20,
                                  master_seed=7, trials_per_block=1,
                                  steps_per_phase=1,
                                  data_origin=DataOrigin.LIVE_OTA, coordinator=c,
                                  env_driver=driver)
            r.channel_model = None                   # LIVE routing: no channel
            # DIRECT evidence: wrap the per-method executor and count invocations.
            calls = {"n": 0}
            _orig = r._run_method_in_block

            def _counted(*a, **k):
                calls["n"] += 1
                return _orig(*a, **k)
            r._run_method_in_block = _counted
            with self.assertRaises(PairedResetError):
                r.run()
            self.assertEqual(calls["n"], 0)          # NO method was executed
        finally:
            c.stop()

    # -- (C) paired-reset verdict rejects a None readback flag ---------------
    def test_paired_reset_verified_rejects_none_readback(self):
        # P0-20 Codex hole C: paired_reset_verified is True ONLY when every method
        # has an IDENTICAL start digest AND every readback flag is EXACTLY True.
        # The old `all(f is not False)` wrongly admitted a None flag.
        f = PairedBlockRunner._paired_reset_verified
        methods = ["a", "b"]
        same = {"a": "d", "b": "d"}
        self.assertIs(f(methods, same, [True, True]), True)
        self.assertIs(f(methods, same, [True, None]), False)   # None NOT a pass
        self.assertIs(f(methods, same, [None, None]), False)
        self.assertIs(f(methods, {"a": "d"}, [True, True]), False)  # missing meth
        self.assertIs(f(methods, same, [True]), False)         # length mismatch
        self.assertIs(f(methods, {"a": "d", "b": "e"}, [True, True]), False)  # div
        self.assertIsNone(f(methods, {}, []))                  # synthetic basis


# --------------------------------------------------------------------------- #
# P0-20: proposer-backend state contract (stateless remote LLM backends +      #
# the in-scope snapshot set)                                                   #
# --------------------------------------------------------------------------- #

class _FakeMsg:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMsg(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]
        self.usage = None


class _FakeCompletions:
    """The fake SERVER's completion endpoint.

    Its sampler state lives HERE - server side - exactly as in a real
    LiteLLM/Ollama deployment. That separation is precisely why the backend
    ADAPTER object is stateless and has nothing local to restore (P0-20 / (A)).
    """

    def __init__(self):
        from decision.llm_backend import DeterministicMockBackend
        self._server = DeterministicMockBackend(seed=4242)

    def create(self, model=None, messages=None, **kwargs):
        prompt = "\n".join(str(m.get("content", "")) for m in (messages or []))
        return _FakeResponse(self._server.generate(prompt).content)


class _FakeChat:
    def __init__(self):
        self.completions = _FakeCompletions()


class _FakeOpenAIClient:
    """Offline stand-in for the openai SDK handle (NO network, NO proxy)."""

    def __init__(self):
        self.chat = _FakeChat()


class _StatefulBackendNoContract:
    """A proposer backend WITH REAL MUTABLE STATE and NO capture/restore - the
    exact thing the P0-20 guard must keep refusing (regression guard)."""

    def __init__(self, name="local:stateful-no-contract"):
        self._name = name
        self.calls = 0

    @property
    def name(self):
        return self._name

    def is_available(self):
        return True

    def generate(self, prompt, system_prompt=""):
        self.calls += 1                       # mutable proposer state
        return None


def _fake_local_backend(model="unit-test-model"):
    """A LiteLLMBackend bound to an offline fake client (never touches a proxy)."""
    from decision.llm_backend import LiteLLMBackend
    b = LiteLLMBackend(model=model, base_url="http://198.51.100.7:4000/v1",
                       api_key="sk-unit-test-not-a-real-key")
    b.client = _FakeOpenAIClient()            # offline: no proxy, no network
    return b


class P0_20ProposerBackendStateTest(unittest.TestCase):
    """(A) remote LLM backends carry an EXPLICIT, ENFORCED stateless contract;
    (B) only the backends a run can actually exercise are snapshotted."""

    # -- (c) approach invariant: statelessness is CODE-ENFORCED, not assumed --
    def test_remote_backend_state_is_not_a_noop_snapshot(self):
        from decision.llm_backend import BackendStateError
        b = _fake_local_backend()
        snap = b.capture_state()
        self.assertTrue(snap)                        # NOT None / not empty
        b.restore_state(snap)                        # unchanged -> accepted
        b.temperature = 0.99                         # mutate repro-relevant cfg
        with self.assertRaises(BackendStateError):
            b.restore_state(snap)

    def test_new_mutable_attribute_fails_closed(self):
        # default-INCLUDE: an attribute that did not exist at capture time (e.g.
        # a future cache/counter added to generate()) changes the fingerprint,
        # so leaked proposer state can never pass silently.
        from decision.llm_backend import BackendStateError
        b = _fake_local_backend()
        snap = b.capture_state()
        b._n_calls = 1                               # simulated future state
        with self.assertRaises(BackendStateError):
            b.restore_state(snap)
        with self.assertRaises(BackendStateError):
            b.restore_state(snap)                    # still closed on retry

    def test_generate_does_not_mutate_the_backend(self):
        # the claim approach (A) rests on: LiteLLMBackend.generate() is PURE with
        # respect to the backend object (the keep-warm state lives in a separate
        # ModelWarmer). Verified against the real generate() path, offline.
        b = _fake_local_backend()
        before = b.capture_state()
        resp = b.generate("Parse this network intent", "sys")
        self.assertTrue(resp.success)
        self.assertEqual(b.capture_state(), before)
        b.restore_state(before)                      # no-mutation -> accepted

    def test_state_fingerprint_never_discloses_secrets(self):
        # the snapshot is compared/logged/embedded in errors: it must not carry
        # the API key or the server address in the clear.
        from decision.llm_backend import BackendStateError
        b = _fake_local_backend()
        snap = b.capture_state()
        blob = repr(snap)
        self.assertNotIn("sk-unit-test-not-a-real-key", blob)
        self.assertNotIn("198.51.100.7", blob)
        b.api_key = "sk-rotated-key"                 # force a fail-closed error
        with self.assertRaises(BackendStateError) as cm:
            b.restore_state(snap)
        msg = str(cm.exception)
        self.assertIn("api_key", msg)                # names the attribute...
        self.assertNotIn("sk-unit-test-not-a-real-key", msg)   # ...not the value
        self.assertNotIn("sk-rotated-key", msg)
        self.assertNotIn("198.51.100.7", msg)

    def test_every_remote_backend_class_has_the_contract(self):
        # the guard used to check ONLY dynamic backends, so a FIXED enum backend
        # could be the active proposer without any state contract at all.
        from decision.llm_backend import (ClaudeBackend, OpenAIBackend,
                                          GeminiBackend, OllamaBackend,
                                          LiteLLMBackend)
        for cls in (ClaudeBackend, OpenAIBackend, GeminiBackend, OllamaBackend,
                    LiteLLMBackend):
            self.assertTrue(callable(getattr(cls, "capture_state", None)), cls)
            self.assertTrue(callable(getattr(cls, "restore_state", None)), cls)

    # -- (b) REGRESSION: mutable state without a contract still fails closed --
    def test_stateful_backend_without_contract_still_fails_closed(self):
        from experiments.emulation import build_emulated_coordinator
        t = three_ue_shared_topology()
        c, _ch = build_emulated_coordinator(seed=7, tau_trial_s=0.0, topology=t)
        try:
            mgr = c.llm_manager
            bad = _StatefulBackendNoContract()
            mgr.dynamic_backends[bad.name] = bad
            self.assertTrue(mgr.set_backend(bad.name))   # ACTIVE proposer
            driver = _QualifiedLiveEnvDriver()
            c.ue_collector = _LiveDriverCollector(driver, t.ue_ids())
            r = PairedBlockRunner(ALL_METHODS, topology=t, n_blocks=1,
                                  master_seed=7, trials_per_block=1,
                                  steps_per_phase=1,
                                  data_origin=DataOrigin.LIVE_OTA,
                                  coordinator=c, env_driver=driver)
            r.channel_model = None
            calls = {"n": 0}
            _orig = r._run_method_in_block

            def _counted(*a, **k):
                calls["n"] += 1
                return _orig(*a, **k)
            r._run_method_in_block = _counted
            with self.assertRaises(PairedResetError) as cm:
                r.run()
            self.assertIn("capture/restore_state", str(cm.exception))
            self.assertEqual(calls["n"], 0)              # ZERO methods executed
        finally:
            c.stop()

    # -- (a) a discovered dynamic REMOTE backend no longer blocks a paired run -
    def test_paired_run_proceeds_with_dynamic_remote_backend_registered(self):
        # the reported bug: a local LiteLLM proxy advertising models registered
        # 'local:*' backends and every paired/live run refused to start.
        from experiments.emulation import build_emulated_coordinator
        t = three_ue_shared_topology()
        c, _ch = build_emulated_coordinator(seed=7, tau_trial_s=0.0, topology=t)
        try:
            mgr = c.llm_manager
            unused = _fake_local_backend("discovered-but-unused")
            mgr.dynamic_backends[unused.name] = unused
            driver = _QualifiedLiveEnvDriver()
            c.ue_collector = _LiveDriverCollector(driver, t.ue_ids())
            r = PairedBlockRunner(ALL_METHODS, topology=t, n_blocks=1,
                                  master_seed=7, trials_per_block=1,
                                  steps_per_phase=1,
                                  data_origin=DataOrigin.LIVE_OTA,
                                  coordinator=c, env_driver=driver)
            r.channel_model = None
            res = r.run()
            self.assertEqual(res["n_blocks"], 1)
            self.assertTrue(res["blocks"][0]["all_methods_present"])
            # (B) invariant: discovered-but-unused is OUT of scope; the wired
            # baselines + the ACTIVE proposer are IN scope.
            scope = r._proposer_scope(mgr)
            self.assertNotIn(unused.name, scope)
            self.assertIn("mock:deterministic", scope)
            for m in ("rule_based", "score_heuristic", "rl_controller"):
                self.assertIn(f"baseline:{m}", scope)
        finally:
            c.stop()

    def test_active_remote_backend_is_in_scope_and_paired(self):
        # the Stage-D case: the LLM proposer IS a dynamic 'local:*' backend. It
        # must be IN scope (captured + restored + read-back verified), not
        # skipped - and a FULL paired run (baseline methods swap the active
        # proposer mid-block) must still complete.
        from experiments.emulation import build_emulated_coordinator
        t = three_ue_shared_topology()
        c, _ch = build_emulated_coordinator(seed=7, tau_trial_s=0.0, topology=t)
        try:
            mgr = c.llm_manager
            local = _fake_local_backend("stage-d-model")
            mgr.dynamic_backends[local.name] = local
            self.assertTrue(mgr.set_backend(local.name))
            driver = _QualifiedLiveEnvDriver()
            c.ue_collector = _LiveDriverCollector(driver, t.ue_ids())
            r = PairedBlockRunner(ALL_METHODS, topology=t, n_blocks=1,
                                  master_seed=7, trials_per_block=1,
                                  steps_per_phase=1,
                                  data_origin=DataOrigin.LIVE_OTA,
                                  coordinator=c, env_driver=driver)
            r.channel_model = None
            snap = r._capture_block_state()
            self.assertIn(local.name, snap["backends"])
            self.assertIn(local.name, r._proposer_scope(mgr))
            self.assertEqual(snap["backends"][local.name], local.capture_state())
            res = r.run()                       # full paired run, no exception
            self.assertEqual(res["n_blocks"], 1)
            self.assertTrue(res["blocks"][0]["all_methods_present"])
            # and a MUTATION of that in-scope backend fails the verified restore
            local.temperature = 0.99
            with self.assertRaises(PairedResetError) as cm:
                r._restore_block_state_verified(snap)
            self.assertIn("restore FAILED", str(cm.exception))
        finally:
            mgr.shutdown()                       # stop the keep-warm thread
            c.stop()

    # -- (c) the narrowed scope cannot be exploited ---------------------------
    def test_backend_outside_snapshot_becoming_active_fails_closed(self):
        # narrowing the snapshot SET is only safe because the restore re-derives
        # it: a proposer that appears (or disappears) mid-block is un-pairable.
        from experiments.emulation import build_emulated_coordinator
        t = three_ue_shared_topology()
        c, _ch = build_emulated_coordinator(seed=7, tau_trial_s=0.0, topology=t)
        try:
            mgr = c.llm_manager
            driver = _QualifiedLiveEnvDriver()
            c.ue_collector = _LiveDriverCollector(driver, t.ue_ids())
            r = PairedBlockRunner(ALL_METHODS, topology=t, n_blocks=1,
                                  master_seed=7, trials_per_block=1,
                                  steps_per_phase=1,
                                  data_origin=DataOrigin.LIVE_OTA,
                                  coordinator=c, env_driver=driver)
            r.channel_model = None
            snap = r._capture_block_state()
            sneaky = _fake_local_backend("swapped-in-mid-block")
            mgr.dynamic_backends[sneaky.name] = sneaky
            self.assertTrue(mgr.set_backend(sneaky.name))   # NOT in the snapshot
            with self.assertRaises(PairedResetError) as cm:
                r._restore_block_state_verified(snap)
            self.assertIn("became reachable after the paired capture",
                          str(cm.exception))
        finally:
            mgr.shutdown()                       # stop the keep-warm thread
            c.stop()

    def test_snapshotted_backend_that_vanishes_fails_closed(self):
        # the complementary direction: a proposer that was snapshotted but is
        # gone at restore time cannot be restored -> fail closed (unchanged).
        from experiments.emulation import build_emulated_coordinator
        t = three_ue_shared_topology()
        c, _ch = build_emulated_coordinator(seed=7, tau_trial_s=0.0, topology=t)
        try:
            mgr = c.llm_manager
            driver = _QualifiedLiveEnvDriver()
            c.ue_collector = _LiveDriverCollector(driver, t.ue_ids())
            r = PairedBlockRunner(ALL_METHODS, topology=t, n_blocks=1,
                                  master_seed=7, trials_per_block=1,
                                  steps_per_phase=1,
                                  data_origin=DataOrigin.LIVE_OTA,
                                  coordinator=c, env_driver=driver)
            r.channel_model = None
            snap = r._capture_block_state()
            del mgr.dynamic_backends["baseline:rule_based"]
            with self.assertRaises(PairedResetError) as cm:
                r._restore_block_state_verified(snap)
            self.assertIn("vanished", str(cm.exception))
        finally:
            c.stop()


class P0_20PaperExportAttestationTest(unittest.TestCase):
    def _synth_run(self):
        r = PairedBlockRunner(ALL_METHODS, n_blocks=1, master_seed=1,
                              trials_per_block=1, steps_per_phase=1)
        return r, r.run()

    def _full_gate(self, res):
        att = res["run_attestation"]
        refs = {g: f"ref:{g}" for g in PaperGateEvidence.REQUIRED}
        return PaperGateEvidence(
            evidence_refs=refs,
            attested_experiment_run_id=att["experiment_run_id"],
            attested_run_digest=att["digest"],
            **{g: True for g in PaperGateEvidence.REQUIRED})

    def test_rejects_relabeled_synthetic(self):
        r, res = self._synth_run()
        gate = self._full_gate(res)
        for rec in res["records"]:
            rec["data_origin"] = "live_ota"
        res["data_origin"] = "live_ota"              # relabel -> digest mismatch
        with self.assertRaises(PaperExportError):
            r.paper_export(gate=gate, result=res)

    def test_rejects_mutated_result(self):
        r, res = self._synth_run()
        gate = self._full_gate(res)
        res["n_blocks"] = 999                        # mutate -> digest mismatch
        with self.assertRaises(PaperExportError):
            r.paper_export(gate=gate, result=res)

    def test_rejects_foreign_result(self):
        r1, res1 = self._synth_run()
        r2, res2 = self._synth_run()
        with self.assertRaises(PaperExportError):     # res1 not r2's attested run
            r2.paper_export(gate=self._full_gate(res1), result=res1)

    def test_rejects_missing_result(self):
        r, res = self._synth_run()
        with self.assertRaises(PaperExportError):
            r.paper_export(gate=self._full_gate(res), result=None)

    def test_rejects_non_true_gate(self):
        r, res = self._synth_run()
        att = res["run_attestation"]
        refs = {g: f"ref:{g}" for g in PaperGateEvidence.REQUIRED}
        kw = {k: True for k in PaperGateEvidence.REQUIRED}
        kw["hardware_provisioned"] = 1                # truthy non-bool
        g = PaperGateEvidence(
            evidence_refs=refs,
            attested_experiment_run_id=att["experiment_run_id"],
            attested_run_digest=att["digest"], **kw)
        with self.assertRaises(PaperExportError):
            r.paper_export(gate=g, result=res)

    def test_rejects_gate_without_refs(self):
        r, res = self._synth_run()
        att = res["run_attestation"]
        g = PaperGateEvidence(
            attested_experiment_run_id=att["experiment_run_id"],
            attested_run_digest=att["digest"],
            **{k: True for k in PaperGateEvidence.REQUIRED})   # no refs
        self.assertFalse(g.is_paper_ready())
        with self.assertRaises(PaperExportError):
            r.paper_export(gate=g, result=res)

    def test_rejects_gate_not_bound_to_run(self):
        r, res = self._synth_run()
        att = res["run_attestation"]
        refs = {g: f"ref:{g}" for g in PaperGateEvidence.REQUIRED}
        g = PaperGateEvidence(                        # digest NOT this run's
            evidence_refs=refs,
            attested_experiment_run_id=att["experiment_run_id"],
            attested_run_digest="deadbeef",
            **{k: True for k in PaperGateEvidence.REQUIRED})
        with self.assertRaises(PaperExportError):
            r.paper_export(gate=g, result=res)

    def test_synthetic_genuine_result_still_not_paper(self):
        # even the UNMUTATED genuine synthetic result is rejected: origin is not
        # live_ota, so it can never be paper data.
        r, res = self._synth_run()
        with self.assertRaises(PaperExportError):
            r.paper_export(gate=self._full_gate(res), result=res)

    def test_paper_export_rejects_block_with_zero_record_method(self):
        # a self-consistent attested live_ota result whose block has a method with
        # ZERO records is rejected: 0 records is NOT "executed" (P0-20 Codex-3).
        r = PairedBlockRunner(list(ALL_METHODS), n_blocks=20, master_seed=1,
                              data_origin=DataOrigin.LIVE_OTA)
        blocks = []
        for i in range(20):
            nrec = {m: 1 for m in ALL_METHODS}
            if i == 0:
                nrec["rule_based"] = 0                 # 0 records -> not executed
            blocks.append({
                "block_id": f"b{i}", "block_index": i, "block_seed": 1000 + i,
                "environment_trace_hash": "envtrace-sha256-" + "a" * 64,
                "paired_reset_verified": True, "all_methods_present": True,
                "block_independent": True, "n_records_by_method": nrec})
        result = {
            "experiment_run_id": "r", "data_origin": "live_ota", "master_seed": 1,
            "n_blocks": 20, "methods": list(ALL_METHODS),
            "required_methods_present": True, "topology": {}, "blocks": blocks,
            "records": [{"data_origin": "live_ota"}], "n_records": 1,
            "shared_executor_ids": ["executor@0x1"], "executor_shared": True,
            "block_independence_verified": True}
        digest = r._attestation_digest(result)
        r._run_attestation = {"digest": digest, "experiment_run_id": "r",
                              "data_origin": "live_ota", "runner_id": id(r)}
        result["run_attestation"] = dict(r._run_attestation)
        refs = {g: f"ref:{g}" for g in PaperGateEvidence.REQUIRED}
        gate = PaperGateEvidence(
            evidence_refs=refs, attested_experiment_run_id="r",
            attested_run_digest=digest,
            **{g: True for g in PaperGateEvidence.REQUIRED})
        with self.assertRaises(PaperExportError):
            r.paper_export(gate=gate, result=result)

    def test_attestation_rejects_none_to_nan_mutation(self):
        # P0-20 Codex hole A: a None field mutated to NaN must be REJECTED. Under a
        # NaN/inf-sanitising digest the swap is INVISIBLE (None and NaN both
        # serialise to null -> SAME digest); the TAMPER-EVIDENT digest serialises
        # with allow_nan=False, so a non-finite value FAILS CLOSED at recompute.
        r = PairedBlockRunner(list(ALL_METHODS), n_blocks=20, master_seed=1,
                              data_origin=DataOrigin.LIVE_OTA)
        blocks = [{
            "block_id": f"b{i}", "block_index": i, "block_seed": 1000 + i,
            "environment_trace_hash": "envtrace-sha256-" + "a" * 64,
            "paired_reset_verified": True, "all_methods_present": True,
            "block_independent": True,
            "n_records_by_method": {m: 1 for m in ALL_METHODS}}
            for i in range(20)]
        result = {
            "experiment_run_id": "r", "data_origin": "live_ota", "master_seed": 1,
            "n_blocks": 20, "methods": list(ALL_METHODS),
            "required_methods_present": True, "topology": {}, "blocks": blocks,
            "records": [{"data_origin": "live_ota"}], "n_records": 1,
            "shared_executor_ids": ["executor@0x1"], "executor_shared": True,
            "block_independence_verified": True,
            "optional_none_field": None}       # a GENUINE None in the attested run
        digest = r._attestation_digest(result)
        r._run_attestation = {"digest": digest, "experiment_run_id": "r",
                              "data_origin": "live_ota", "runner_id": id(r)}
        result["run_attestation"] = dict(r._run_attestation)
        refs = {g: f"ref:{g}" for g in PaperGateEvidence.REQUIRED}
        gate = PaperGateEvidence(
            evidence_refs=refs, attested_experiment_run_id="r",
            attested_run_digest=digest,
            **{g: True for g in PaperGateEvidence.REQUIRED})
        # UNMUTATED, the finite result recomputes to the SAME digest (isolates the
        # NaN as the only fault).
        self.assertEqual(r._attestation_digest(result), digest)
        # None -> NaN tamper: the digest refuses to serialise the non-finite value
        result["optional_none_field"] = float("nan")
        with self.assertRaises(ValueError):
            r._attestation_digest(result)
        # ... and paper_export rejects it (fail-closed, NOT a silent equal digest).
        with self.assertRaises(PaperExportError):
            r.paper_export(gate=gate, result=result)


# --------------------------------------------------------------------------- #
# P1-1 clip metric                                                            #
# --------------------------------------------------------------------------- #

class ClipMetricTest(unittest.TestCase):
    def test_clip_metric_never_mixes_native_units(self):
        eps = [
            EpisodeRecord(1, "m", "P", schema_valid=True, trial_executed=True,
                          clips=[ClipEvent("bs2_power", 5.0, 8.0)]),      # dB mag 3
            EpisodeRecord(1, "m", "P", schema_valid=True, trial_executed=True,
                          clips=[ClipEvent("bs1_prb", 30.0, 20.0)]),      # PRB mag 10
            # point 4: non-finite and bool magnitudes are EXPLICIT UNKNOWN,
            # quarantined from every aggregate (never averaged in, never a fake 0).
            EpisodeRecord(1, "m", "P", schema_valid=True, trial_executed=True,
                          clips=[ClipEvent("bs2_power", float("inf"), 8.0)]),
            EpisodeRecord(1, "m", "P", schema_valid=True, trial_executed=True,
                          clips=[ClipEvent("bs1_power", True, False)]),   # bool
            # blocker 1: two DIFFERENT unrecognised axes must land in SEPARATE
            # per-label buckets, never merged into one "other" average (that would
            # re-mix distinct unknown native units).
            EpisodeRecord(1, "m", "P", schema_valid=True, trial_executed=True,
                          clips=[ClipEvent("weird_alpha", 1.0, 4.0)]),    # mag 3
            EpisodeRecord(1, "m", "P", schema_valid=True, trial_executed=True,
                          clips=[ClipEvent("weird_beta", 2.0, 9.0)]),     # mag 7
        ]
        r = executor_enforcement_statistics(eps)
        self.assertNotIn("mean_clip_magnitude_db", r)
        self.assertNotIn("max_clip_magnitude_db", r)
        # power and prb are reported SEPARATELY, in their own units
        pa = r["per_axis_native_clip"]
        self.assertEqual(pa["power_offset"]["unit"], "dB")
        self.assertEqual(pa["prb"]["unit"], "PRB")
        # the inf/bool clips did NOT pollute the native mean (stays the finite
        # 3.0 from the single valid dB clip) nor inflate its count.
        self.assertEqual(pa["power_offset"]["n"], 1)
        self.assertAlmostEqual(pa["power_offset"]["mean"], 3.0)
        self.assertTrue(math.isfinite(pa["power_offset"]["mean"]))
        # blocker 1: the two unknown axes are in SEPARATE per-label buckets, NOT
        # averaged into a single 'other' (their magnitudes 3 and 7 stay distinct).
        self.assertNotIn("other", pa)
        self.assertAlmostEqual(pa["other:weird_alpha"]["mean"], 3.0)
        self.assertAlmostEqual(pa["other:weird_beta"]["mean"], 7.0)
        # normalized is unit-free AND finite (not dragged to NaN by the inf clip);
        # only the 2 KNOWN-span axes normalize, the 2 unknown-axis clips have no
        # span -> EXPLICIT unnormalized, never a fake zero.
        self.assertTrue(math.isfinite(r["normalized_clip"]["mean"]))
        self.assertEqual(r["normalized_clip"]["n"], 2)
        self.assertEqual(r["n_unnormalized_clip"], 2)
        # blocker 2: counts reconcile - 6 total clip events = 4 finite-magnitude
        # (2 known + 2 unknown-axis) + 2 unknown-magnitude (inf/bool).
        self.assertEqual(r["n_unknown_magnitude"], 2)
        self.assertEqual(r["n_valid_clip_events"], 4)
        self.assertEqual(r["n_clip_events"], 6)
        # blocker 2 (span): a bool/NaN/inf/<=0 axis span must NOT fake-normalize
        # (magnitude/inf == 0.0). The clip stays EXPLICIT unnormalized; the native
        # magnitude is untouched.
        bad = executor_enforcement_statistics(
            [EpisodeRecord(1, "m", "P", schema_valid=True, trial_executed=True,
                           clips=[ClipEvent("bs2_power", 5.0, 8.0)])],
            axis_span={"power_offset": float("inf")})
        self.assertEqual(bad["normalized_clip"]["n"], 0)
        self.assertIsNone(bad["normalized_clip"]["mean"])          # no fake 0.0
        self.assertEqual(bad["n_unnormalized_clip"], 1)
        self.assertAlmostEqual(
            bad["per_axis_native_clip"]["power_offset"]["mean"], 3.0)

    def test_clip_denominator_uses_applied_proposals(self):
        eps = [
            EpisodeRecord(1, "m", "P", schema_valid=True, trial_executed=True,
                          clips=[ClipEvent("bs2_power", 5.0, 8.0)]),   # applied+clip
            EpisodeRecord(1, "m", "P", schema_valid=True, trial_executed=True,
                          clips=[]),                                    # applied, no clip
            EpisodeRecord(1, "m", "P", schema_valid=True,
                          trial_executed=False),   # schema-valid, NO applied proposal
        ]
        r = executor_enforcement_statistics(eps)
        self.assertEqual(r["n_applied_proposals"], 2)   # excludes no-proposal
        self.assertAlmostEqual(r["clip_fraction"], 0.5)  # 1 of 2 applied clipped


if __name__ == "__main__":
    unittest.main()
