#!/usr/bin/env python3
"""§1-4 (Sec I-IV) alignment tests: the honest offered-load exogenous driver,
the §1-4 load-driven phase structure, the shared phase->axis request helper, and
the runner CLI wiring. Offline: no hardware / network / OTA (mock/emulation only).

Basis: docs/paper_alignment_sec1_4.md. The paper's Sec V (§5) is an unrevised
draft and is NOT a design basis; these tests exercise the §1-4-grounded
substitute - resource-competition (offered-load) environment driving, channel
gains held at zero (no attenuator on this testbed), fail-closed operator gating.
"""

import argparse
import os
import unittest

from experiments.environment import (
    ENVIRONMENT_AXES, ACTION_AXES, AxisOverlapError,
    PHASE_KEY_TO_ENV_AXIS, phase_environment_request,
    OfferedLoadEnvironmentDriver, APPROVAL_ENV_VAR,
    validate_exogenous_driver, require_driver_capability,
    validate_applied_environment,
)
from config import sec1_4_load_phases, REFERENCE_CEILING_MBPS


# --------------------------------------------------------------------------- #
# Minimal live-coordinator doubles (mirror tests/test_batch_g.py)             #
# --------------------------------------------------------------------------- #

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


def _args(**kw):
    """A minimal argparse.Namespace mirroring the runner CLI defaults."""
    d = dict(offered_load_mbps=12.0, probe_protocol="udp",
             probe_direction="downlink", probe_duration_s=2.0,
             probe_mode="goodput", capacity_floor_mbps=100.0,
             phase_profile="legacy-5phase", env_driver="none")
    d.update(kw)
    return argparse.Namespace(**d)


# --------------------------------------------------------------------------- #
# 1. Shared phase -> environment-axis request helper                          #
# --------------------------------------------------------------------------- #

class PhaseRequestHelperTest(unittest.TestCase):
    def test_channel_gains_always_present_default_zero(self):
        req = phase_environment_request({"name": "N"})
        self.assertEqual(req, {"channel_serving_gain_db": 0.0,
                               "channel_neighbor_gain_db": 0.0})

    def test_optional_axes_present_only_when_supplied(self):
        req = phase_environment_request(
            {"name": "L", "serving_gain": 0, "neighbor_gain": 0,
             "offered_load_mbps": 20.0})
        self.assertEqual(set(req), {"channel_serving_gain_db",
                                    "channel_neighbor_gain_db",
                                    "offered_load_mbps"})
        self.assertEqual(req["offered_load_mbps"], 20.0)

    def test_rejects_action_axis_in_phase(self):
        with self.assertRaises(AxisOverlapError):
            phase_environment_request({"name": "bad", "rfatt": 3.0})

    def test_rejects_nonfinite_value(self):
        for bad in (float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                phase_environment_request({"name": "x", "serving_gain": bad})

    def test_runner_delegates_to_shared_helper(self):
        # the runner's private method and the module helper must never diverge
        from experiments.runner import ExperimentRunner
        r = ExperimentRunner.__new__(ExperimentRunner)
        for ph in ({"name": "N", "serving_gain": 3, "neighbor_gain": 0},
                   {"name": "L", "serving_gain": 0, "neighbor_gain": 0,
                    "offered_load_mbps": 12.0, "external_disturbance_db": 0.3}):
            self.assertEqual(ExperimentRunner._phase_requested_env(r, ph),
                             phase_environment_request(ph))

    def test_runner_class_mapping_is_the_shared_mapping(self):
        from experiments.runner import ExperimentRunner
        self.assertEqual(dict(ExperimentRunner._PHASE_TO_ENV_AXIS),
                         dict(PHASE_KEY_TO_ENV_AXIS))


# --------------------------------------------------------------------------- #
# 2. §1-4 load-driven phase structure                                         #
# --------------------------------------------------------------------------- #

class Sec14LoadPhasesTest(unittest.TestCase):
    def test_three_phases_channel_gains_zero(self):
        phases = sec1_4_load_phases()
        self.assertEqual([p["name"] for p in phases],
                         ["Nominal", "Congestion", "Recovery"])
        for p in phases:
            self.assertEqual(p["serving_gain"], 0)
            self.assertEqual(p["neighbor_gain"], 0)
            self.assertIn("offered_load_mbps", p)

    def test_default_load_levels_from_reference_ceiling(self):
        phases = sec1_4_load_phases()
        self.assertEqual(phases[0]["offered_load_mbps"], REFERENCE_CEILING_MBPS)
        self.assertEqual(phases[1]["offered_load_mbps"],
                         2.0 * REFERENCE_CEILING_MBPS)
        self.assertEqual(phases[2]["offered_load_mbps"], REFERENCE_CEILING_MBPS)

    def test_congestion_load_exceeds_nominal(self):
        phases = sec1_4_load_phases(8.0, 30.0)
        self.assertLess(phases[0]["offered_load_mbps"],
                        phases[1]["offered_load_mbps"])

    def test_phases_carry_no_action_axis(self):
        for p in sec1_4_load_phases():
            self.assertFalse(set(p) & ACTION_AXES)
            # every phase is a valid environment request (channel gains 0)
            req = phase_environment_request(p)
            self.assertEqual(req["channel_serving_gain_db"], 0.0)
            self.assertEqual(req["channel_neighbor_gain_db"], 0.0)

    def test_rejects_nonpositive_load(self):
        for bad in (0.0, -1.0, float("nan"), float("inf")):
            with self.assertRaises(Exception):
                sec1_4_load_phases(bad, 20.0)


# --------------------------------------------------------------------------- #
# 3. OfferedLoadEnvironmentDriver                                             #
# --------------------------------------------------------------------------- #

class OfferedLoadDriverTest(unittest.TestCase):
    def _phase(self, load=21.0, serving=0.0, neighbor=0.0):
        return {"name": "Congestion", "serving_gain": serving,
                "neighbor_gain": neighbor, "offered_load_mbps": load}

    def test_supported_axes_are_environment_only(self):
        d = OfferedLoadEnvironmentDriver()
        self.assertTrue(d.supported_axes <= ENVIRONMENT_AXES)
        self.assertIn("offered_load_mbps", d.supported_axes)
        self.assertIn("channel_serving_gain_db", d.supported_axes)
        self.assertIn("channel_neighbor_gain_db", d.supported_axes)
        self.assertFalse(d.supported_axes & ACTION_AXES)

    def test_default_is_unapproved_and_rejected(self):
        d = OfferedLoadEnvironmentDriver(load_setter=lambda m: None)
        self.assertIs(d.approved, False)
        with self.assertRaises(AxisOverlapError):
            validate_exogenous_driver(d)

    def test_code_never_self_approves_truthy_is_not_true(self):
        # a stray truthy non-bool (1, "yes") is NOT a qualification
        self.assertIs(OfferedLoadEnvironmentDriver(approved=1).approved, False)
        self.assertIs(OfferedLoadEnvironmentDriver(approved="yes").approved,
                      False)

    def test_explicit_arg_approval_validates(self):
        d = OfferedLoadEnvironmentDriver(load_setter=lambda m: None,
                                         approved=True)
        self.assertIs(d.approved, True)
        validate_exogenous_driver(d)          # no raise

    def test_from_env_approves_only_on_exact_1(self):
        mk = OfferedLoadEnvironmentDriver.from_env
        self.assertIs(mk(lambda m: None,
                         environ={APPROVAL_ENV_VAR: "1"}).approved, True)
        for v in ("0", "true", "yes", "", "01"):
            self.assertIs(mk(lambda m: None,
                             environ={APPROVAL_ENV_VAR: v}).approved, False)
        self.assertIs(mk(lambda m: None, environ={}).approved, False)

    def test_apply_sets_load_and_reports_exact_map(self):
        calls = []
        d = OfferedLoadEnvironmentDriver(load_setter=calls.append,
                                         approved=True)
        ph = self._phase(load=21.0)
        req = phase_environment_request(ph)
        require_driver_capability(d, req.keys())          # no raise
        applied = d.apply(ph, 1, "p1:Congestion")
        validate_applied_environment(applied, req)        # exact match
        self.assertEqual(applied, {"channel_serving_gain_db": 0.0,
                                   "channel_neighbor_gain_db": 0.0,
                                   "offered_load_mbps": 21.0})
        self.assertEqual(calls, [21.0])

    def test_apply_fails_closed_on_nonzero_channel_no_side_effect(self):
        calls = []
        d = OfferedLoadEnvironmentDriver(load_setter=calls.append,
                                         approved=True)
        with self.assertRaises(AxisOverlapError):
            d.apply(self._phase(load=10.0, serving=-5.0), 0, "p0:Bad")
        self.assertEqual(calls, [])          # load NEVER touched (fail closed)

    def test_apply_fails_closed_without_load_setter(self):
        d = OfferedLoadEnvironmentDriver(approved=True)   # no setter
        with self.assertRaises(AxisOverlapError):
            d.apply(self._phase(load=12.0), 0, "p0:Congestion")

    def test_apply_fails_closed_on_unsupported_axis(self):
        # a phase requesting an axis this driver cannot move (disturbance) is
        # refused by the capability check inside apply (never a partial write)
        calls = []
        d = OfferedLoadEnvironmentDriver(load_setter=calls.append,
                                         approved=True)
        ph = {"name": "D", "serving_gain": 0, "neighbor_gain": 0,
              "offered_load_mbps": 12.0, "external_disturbance_db": 0.3}
        with self.assertRaises(AxisOverlapError):
            d.apply(ph, 0, "p0:D")
        self.assertEqual(calls, [])

    def test_rejects_noncallable_load_setter(self):
        with self.assertRaises(ValueError):
            OfferedLoadEnvironmentDriver(load_setter=123)


# --------------------------------------------------------------------------- #
# 4. Runner live path with the load driver                                    #
# --------------------------------------------------------------------------- #

class RunnerLiveDriverTest(unittest.TestCase):
    def _runner(self, driver, phases):
        from experiments.runner import ExperimentRunner
        r = ExperimentRunner(coordinator=_MinCoord())
        r.channel_model = None            # LIVE path
        r.env_driver = driver
        r.phases = phases
        return r

    def test_preflight_and_apply_drive_offered_load(self):
        calls = []
        driver = OfferedLoadEnvironmentDriver(load_setter=calls.append,
                                              approved=True)
        phases = sec1_4_load_phases()
        r = self._runner(driver, phases)
        r._preflight_environment()        # no raise: capability covers each phase
        for idx, ph in enumerate(phases):
            r._apply_phase_environment(ph, idx, "p%d:%s" % (idx, ph["name"]))
        # the driver physically set each phase's offered load, in order
        self.assertEqual(calls, [p["offered_load_mbps"] for p in phases])
        # one apply-log entry per phase, each tagged environment-only
        self.assertEqual(len(r._env_apply_log), len(phases))
        self.assertEqual(r.coordinator.executor.writes, [])   # env != action

    def test_live_nonzero_channel_phase_fails_closed(self):
        driver = OfferedLoadEnvironmentDriver(load_setter=lambda m: None,
                                              approved=True)
        # a §V-B-style channel-gain phase cannot be honestly driven here
        r = self._runner(driver, [{"name": "Impairment", "serving_gain": -5,
                                   "neighbor_gain": 2}])
        with self.assertRaises(AxisOverlapError):
            r._apply_phase_environment(r.phases[0], 0, "p0:Impairment")

    def test_live_without_driver_preflight_fails_closed(self):
        r = self._runner(None, sec1_4_load_phases())
        with self.assertRaises(AxisOverlapError):
            r._preflight_environment()
        self.assertEqual(r.coordinator.executor.writes, [])


# --------------------------------------------------------------------------- #
# 5. Runner CLI wiring (_apply_env_args)                                      #
# --------------------------------------------------------------------------- #

class CliEnvArgsTest(unittest.TestCase):
    def setUp(self):
        # isolate the approval env var for these tests
        self._saved = os.environ.pop(APPROVAL_ENV_VAR, None)

    def tearDown(self):
        os.environ.pop(APPROVAL_ENV_VAR, None)
        if self._saved is not None:
            os.environ[APPROVAL_ENV_VAR] = self._saved

    def _runner(self):
        from experiments.runner import ExperimentRunner
        return ExperimentRunner(coordinator=None)

    def test_default_leaves_legacy_phases_and_no_driver(self):
        from experiments.runner import _apply_env_args
        r = self._runner()
        before = list(r.phases)
        _apply_env_args(r, _args(), None)
        self.assertEqual(r.phases, before)     # legacy 5-phase untouched
        self.assertIsNone(r.env_driver)

    def test_sec14_load_profile_selected(self):
        from experiments.runner import _apply_env_args
        r = self._runner()
        _apply_env_args(r, _args(phase_profile="sec14-load",
                                 offered_load_mbps=9.0), None)
        self.assertEqual([p["name"] for p in r.phases],
                         ["Nominal", "Congestion", "Recovery"])
        self.assertEqual(r.phases[1]["offered_load_mbps"], 18.0)
        self.assertTrue(all(p["serving_gain"] == 0 for p in r.phases))

    def test_env_driver_unapproved_without_env(self):
        from experiments.runner import _apply_env_args
        r = self._runner()
        _apply_env_args(r, _args(env_driver="offered-load"), None)
        self.assertIsInstance(r.env_driver, OfferedLoadEnvironmentDriver)
        self.assertIs(r.env_driver.approved, False)

    def test_env_driver_approved_with_env(self):
        from experiments.runner import _apply_env_args
        os.environ[APPROVAL_ENV_VAR] = "1"
        r = self._runner()
        _apply_env_args(r, _args(env_driver="offered-load"), None)
        self.assertIs(r.env_driver.approved, True)

    def test_env_driver_uses_coordinator_load_hook(self):
        from experiments.runner import _apply_env_args, _resolve_offered_load_setter
        os.environ[APPROVAL_ENV_VAR] = "1"
        seen = []

        class _Coord:
            def set_offered_load(self, mbps):
                seen.append(mbps)

        coord = _Coord()
        self.assertIsNotNone(_resolve_offered_load_setter(coord))
        r = self._runner()
        _apply_env_args(r, _args(env_driver="offered-load"), coord)
        # the wired driver drives the real hook when applied
        r.env_driver.apply({"name": "C", "serving_gain": 0, "neighbor_gain": 0,
                            "offered_load_mbps": 21.0}, 0, "p0:C")
        self.assertEqual(seen, [21.0])

    def test_no_load_hook_resolves_none(self):
        from experiments.runner import _resolve_offered_load_setter
        self.assertIsNone(_resolve_offered_load_setter(None))
        self.assertIsNone(_resolve_offered_load_setter(object()))


if __name__ == "__main__":
    unittest.main()
