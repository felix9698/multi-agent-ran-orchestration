# SPDX-License-Identifier: MIT
"""Two clocks, one board: the Kernel case deadline, B, H and the excised time (2026-09-23).

Audit of 2026-09-23, eight defects, and the owner's rule of the same evening:

    "우리 측 문제로 기기 연결 끊기거나 핸드오버 오래 걸리거나 이런 건 그냥 시간으로
     카운트하지 말고 판을 그대로 이어간 걸로 치자 ... 그동안 측정한 kpm 도 안 쓰는 거지.
     그냥 볼드모트 취급하자고."  -- and: "너무 많이 기다리지 마라."

Every class below fails on the code before these changes.
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tools.hfconsole.agent_env import EmulatedRan
from tools.liveconsole import agent as agent_module
from tools.liveconsole.agent import (
    AgentRequest, AgentSitting, METHOD_DETERMINISTIC, TIMING_COLD_START,
    build_hardware_free_agent_sitting)

HOME, TARGET = "12345678", "87654321"
# Numeric labels resolve to themselves, so no role resolver is needed.
INTENTS = tuple({"intentId": f"I{n}", "owner": f"13{n}-owner", "ueId": f"13{n}", "priority": 1,
                 "weight": 1.0, "requirement": {"reqId": f"I{n}.r1", "kpi": "dlGoodputMbps",
                                                "op": ">=", "value": 6.0, "bound": 5.5,
                                                "steps": 2, "unit": "Mbps"}}
                for n in (1, 2))   # unattainable on 5 Mbps cells: every trial runs


def hardware_free(tmp, **kwargs):
    ran = EmulatedRan(ues={"131": HOME, "132": HOME}, cells={HOME: 5.0, TARGET: 5.0},
                      offered_load_mbps={"131": 4.0, "132": 4.0}, noise_sigma=0.0)
    request = AgentRequest(intents=INTENTS, method=METHOD_DETERMINISTIC,
                           axes=("servingCell",), timing_mode=TIMING_COLD_START, **kwargs)
    return build_hardware_free_agent_sitting(request, tmp_dir=tmp, ran=ran,
                                             stamp="20260923T010000Z")


class Clock:
    def __init__(self, t=0.0):
        self.t = float(t)

    def monotonic_ms(self):
        return self.t

    def sleep_ms(self, ms):
        self.t += float(ms)

    def now(self):
        # A wall clock that moves with the monotonic one.  It used to wrap every 60 s
        # ("12:00:%06.3f" % (t % 60)), harmless while waits were sums of ``waitedMs``;
        # since 2026-09-24 the relief is the union of time spans, so it must not wrap.
        moment = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc) + timedelta(milliseconds=self.t)
        return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Hermetic(unittest.TestCase):
    def setUp(self):
        guard = patch("socket.socket", side_effect=AssertionError("network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = temporary.name


# --------------------------------------------------------------------------- #
# 1. CASE_NOT_TERMINABLE: the Kernel case deadline follows B
# --------------------------------------------------------------------------- #


class TheKernelCaseClosesWhenBDoes(Hermetic):
    """49 episodes (2026-09-22..23) stopped on B and the Kernel answered
    CASE_NOT_TERMINABLE: its case deadline was the derived 36.8 min."""

    def test_the_case_deadline_is_what_b_has_left_less_one_trial(self):
        sitting = hardware_free(self.tmp, budget_trials=40, deadline_ms=10_000,
                                horizon_ms=10_000)
        case = sitting.runtime.kernel.reduced_state()["cases"][sitting.runtime.case_id]
        span = (agent_module._parse_time(case["deadlineAt"])
                - agent_module._parse_time(case["startedAt"]))
        self.assertLessEqual(span, 10_000)
        self.assertGreater(span, 0)

    def test_a_sitting_that_stops_on_b_has_a_kernel_ending(self):
        sitting = hardware_free(self.tmp, budget_trials=40, deadline_ms=10_000,
                                horizon_ms=10_000)
        sitting.confirm()
        sitting.run()
        self.assertEqual("DEADLINE", sitting.termination, sitting.termination_detail)
        self.assertEqual("EVIDENCE_INCOMPLETE", sitting.kernel_termination,
                         sitting.preflight.get("kernelTerminationRefusal"))

    def test_time_b_was_relieved_of_rolls_the_case_over_instead_of_ending(self):
        """Excised time moves B but not an open case's deadline: the Kernel runs out
        first and refuses CASE_DEADLINE_REACHED.  That is not the end of the board."""
        sitting = hardware_free(self.tmp, budget_trials=40, deadline_ms=20_000,
                                horizon_ms=20_000)
        sitting.confirm()
        # A 15 s wait for the bed that the open case knows nothing about.
        sitting.clock.sleep_ms(15_000)
        sitting.hardware_disconnects.append(
            {"ues": {}, "reregistered": True, "trials": [], "waitedMs": 15_000.0})
        sitting.run()
        self.assertNotEqual("KERNEL_TERMINATED", sitting.termination, sitting.termination_detail)
        rollovers = [item for item in sitting.identity_rebinds
                     if item.get("reason") == "kernel-deadline-rollover"]
        self.assertTrue(rollovers, sitting.identity_rebinds)
        self.assertEqual("REBOUND", rollovers[0]["outcome"])
        self.assertIsNotNone(sitting.kernel_termination)


# --------------------------------------------------------------------------- #
# 2. H and the policy window move with the exempt time
# --------------------------------------------------------------------------- #


class TheHorizonMovesByTheExemptTime(unittest.TestCase):
    def test_a_173_s_wait_still_leaves_the_horizon_to_observe(self):
        clock = Clock(t=500_000.0)
        sitting = AgentSitting.__new__(AgentSitting)
        sitting.clock, sitting.started_ms = clock, 0.0
        sitting.request = SimpleNamespace(horizon_ms=480_000)
        sitting.composition_wait_ms = 0.0
        sitting.hardware_disconnects = [{"waitedMs": 173_000.0}]
        sitting.service_trace = []
        sitting.stopped = False
        sitting.preflight = {}
        sitting.declare_non_trial_event = lambda *a, **k: None
        sitting._sample_service_trace = lambda **k: sitting.service_trace.append(
            {"t": clock.t, "kpis": {}}) or {}
        sitting._observe_to_horizon(1000)
        # 500 s elapsed, 173 s of it the bed's: 153 s of H are still owed.
        self.assertGreaterEqual(sitting.preflight["horizonObservation"]["samples"], 150)


def _live_cap_sitting(test, directory, amf, **request_kwargs):
    """The live composition (R1 cap adapter, KPM readback) over the emulator."""
    from assurance.objectives.action102_support import CAP_ACTION_ID
    from oran.campaign5.families import CAMPAIGN5_FAMILIES
    from tools.hfconsole.agent_env import (
        EmulatedClock, EmulatedKpiObserver, EmulatedKpmStream, EmulatedPolicyPort,
        HermeticDeployment, POLICY_TYPE_ID)
    family = CAMPAIGN5_FAMILIES["cap"]
    ran = EmulatedRan(ues={"ue1": HOME}, cells={HOME: 5.0, TARGET: 5.0}, noise_sigma=0.0)
    clock = EmulatedClock(ran)
    stream = EmulatedKpmStream(clock, ran)
    stream.amf_of = amf

    def lines():
        records = [json.loads(line) for line in stream()]
        for record in records:
            record["ues"][0]["measurements"] = [
                {"name": family.readback_counter, "type": "int", "value": ran.caps["ue1"]}]
        return [json.dumps(record) for record in records]

    intent = {"intentId": "I1", "owner": "ue1-owner", "ueId": "ue1", "priority": 1,
              "weight": 1.0, "requirement": {"reqId": "I1.r1", "kpi": "dlGoodputMbps",
                                             "op": ">=", "value": 3.0, "bound": 2.0,
                                             "steps": 2, "unit": "Mbps"}}
    sitting = agent_module.build_agent_sitting(
        HermeticDeployment.write(directory, ues=ran.ues, cells=ran.cells),
        AgentRequest(intents=(intent,), method=METHOD_DETERMINISTIC,
                     axes=("servingCell", "dlPrbCap"), caps={"ue1": (6, 12)},
                     restrict_catalog_to_candidates=False, **request_kwargs),
        read_new_lines=lines, policy_port=EmulatedPolicyPort(POLICY_TYPE_ID),
        action_policy_ports={CAP_ACTION_ID: object()}, ports=clock,
        scope_clearer=lambda *args, **kwargs: (),
        kpi_observer=EmulatedKpiObserver(ran), stamp="20260923T010000Z")
    test.addCleanup(lambda: [item.close() for item in sitting.supplementary])
    return sitting, clock


class ThePolicyWindowAndTheCompositionWaitFollowTheSitting(Hermetic):
    def setUp(self):
        super().setUp()
        self.amf = {"ue1": 34}
        resolver = patch.object(agent_module, "role_identity_resolver",
                                lambda _doc, **_kw: (lambda label: self.amf.get(str(label))))
        resolver.start()
        self.addCleanup(resolver.stop)

    def test_a_policy_written_after_a_wait_outlives_the_old_horizon(self):
        """2026-09-19 board 093728: the settled pfWeight policy expired at t0 + 480 s
        after a 173 s wait; the readback saw 4.0 -> 1.0 and locked the trial down."""
        providers = []
        real = agent_module._build_supplementary

        def capture(**kwargs):
            providers.append(kwargs["validity_provider"])
            return real(**kwargs)

        with patch.object(agent_module, "_build_supplementary", capture):
            sitting, _clock = _live_cap_sitting(self, self.tmp, self.amf, deadline_ms=480_000,
                                                horizon_ms=480_000,
                                                timing_mode=TIMING_COLD_START)
        before = agent_module._parse_time(providers[-1]({})["notAfter"])
        sitting.hardware_disconnects.append({"ues": {}, "trials": [], "waitedMs": 173_000.0})
        after = agent_module._parse_time(providers[-1]({})["notAfter"])
        self.assertAlmostEqual(173_000.0, after - before, delta=5.0)

    def test_a_rebind_s_composition_wait_reaches_the_sitting(self):
        real_wait = agent_module._await_addressable

        def slow_when_moved(**kwargs):
            if self.amf["ue1"] == 35:
                kwargs["clock"].sleep_ms(40_000)     # the re-registered UE is not yet seen
            return real_wait(**kwargs)

        sitting, _clock = _live_cap_sitting(self, self.tmp, self.amf)
        before = float(sitting.composition_wait_ms)
        self.amf["ue1"] = 35
        with patch.object(agent_module, "_await_addressable", slow_when_moved):
            sitting._rebind_if_reregistered()
        self.assertEqual("REBOUND", sitting.identity_rebinds[-1]["outcome"])
        self.assertGreaterEqual(sitting.composition_wait_ms - before, 40_000.0)


# --------------------------------------------------------------------------- #
# 4. a rebind that fails is retried, and a trial never goes to a dead id
# --------------------------------------------------------------------------- #


class _Runtime:
    def __init__(self, case_id):
        self.case_id, self.trials, self.terminated = case_id, [], False

    def terminate(self):
        self.terminated = True
        return SimpleNamespace(value="EVIDENCE_INCOMPLETE")

    def live_baseline(self):
        return {}

    def spent_ids(self):
        return set()


class AFailedRebindIsRetriedThenEndsAsHardware(unittest.TestCase):
    def sitting(self, fail_times):
        clock = Clock()
        sitting = AgentSitting.__new__(AgentSitting)
        sitting.clock = clock
        sitting.identities = {"ue3": SimpleNamespace(amf_ue_ngap_id=37, observed_at="t0",
                                                     serving_nci=12345678)}
        sitting.amf_of = lambda ue: 38
        sitting.runtime = _Runtime("case/x")
        sitting.retention_runtime = None
        sitting.retired_runtimes, sitting.identity_rebinds = [], []
        sitting.hardware_disconnects, sitting.service_trace = [], []
        sitting.baselines, sitting.policy_builders, sitting.supplementary = {}, [], ()
        sitting.request = SimpleNamespace(budget_trials=8, deadline_ms=480_000)
        sitting.started_ms, sitting.composition_wait_ms = 0.0, 0.0
        sitting.stopped, sitting.termination = False, None
        sitting.search_terminated_at = None
        sitting.grid = SimpleNamespace(trials=[])
        sitting.applied_configuration = lambda: {}
        sitting._trials_used_total = lambda: 0
        calls = []

        def factory(identities, changed, applied, index, remaining_trials=None):
            calls.append(clock.t)
            if len(calls) <= fail_times:
                raise RuntimeError("no fresh indication for 38")
            return {"runtime": _Runtime("case/x/rebind-1"),
                    "identities": dict(identities, ue3=SimpleNamespace(
                        amf_ue_ngap_id=38, observed_at="t1", serving_nci=12345678)),
                    "freed": {"ue3": []}}

        sitting.rebind_factory = factory
        return sitting, calls

    def test_two_failures_then_a_rebind(self):
        sitting, calls = self.sitting(fail_times=2)
        self.assertTrue(sitting._rebind_if_reregistered())
        record = sitting.identity_rebinds[-1]
        self.assertEqual(("REBOUND", 3), (record["outcome"], record["attempts"]))
        self.assertEqual(38, sitting.identities["ue3"].amf_ue_ngap_id)
        # The retries are a hardware wait: B is relieved of them.
        disconnect, = sitting.hardware_disconnects
        self.assertGreaterEqual(disconnect["waitedMs"], 2 * agent_module.REBIND_RETRY_MS)
        self.assertTrue(disconnect["reregistered"])

    def test_a_rebind_that_never_succeeds_stops_the_sitting_without_a_trial(self):
        sitting, calls = self.sitting(fail_times=10**6)
        old = sitting.runtime
        self.assertFalse(sitting._rebind_if_reregistered())
        self.assertIs(old, sitting.runtime)
        self.assertEqual("NOT_REBOUND", sitting.identity_rebinds[-1]["outcome"])
        self.assertEqual("HARDWARE_UNAVAILABLE", sitting.termination, sitting.termination_detail)
        self.assertGreater(len(calls), 1)
        self.assertLessEqual(sitting.clock.t, agent_module.HARDWARE_WAIT_CAP_MS
                             + agent_module.REBIND_RETRY_MS)


# --------------------------------------------------------------------------- #
# 5. the power axis baseline is polled, never guessed
# --------------------------------------------------------------------------- #


class TheAttenuationBaselineIsPolled(unittest.TestCase):
    def test_a_value_that_arrives_on_the_third_read_is_used(self):
        reads = iter([[], [], ["line"]] + [[]] * 100)

        class Sample:
            counter_id = "RAN.Cell.TxAttenuationDb"
            scope_snapshot = {"e2_node": "ngran=02;plmn=208-095-2;nb=0000002816/00;cudu=none:0"}
            value = SimpleNamespace(value=10.0)
            # A live baseline must be current (2026-09-26): received now, not replayed.
            observed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        adapter = SimpleNamespace(parse_lines=lambda lines: SimpleNamespace(samples=[Sample()]))
        topology = SimpleNamespace(nb_id_to_nci={2816: 87654321})
        clock = Clock()
        found = agent_module._await_cell_attenuations(
            lambda: next(reads), adapter, [87654321], topology, clock, wait_ms=15_000)
        self.assertEqual({87654321: "10.0"}, found)
        self.assertEqual(2000.0, clock.t)

    def test_a_cell_never_read_comes_back_missing_after_the_wait(self):
        clock = Clock()
        adapter = SimpleNamespace(parse_lines=lambda lines: SimpleNamespace(samples=[]))
        found = agent_module._await_cell_attenuations(
            lambda: [], adapter, [87654321], SimpleNamespace(nb_id_to_nci={}), clock,
            wait_ms=agent_module.CELL_BASELINE_WAIT_MS)
        self.assertEqual({}, found)
        self.assertGreaterEqual(clock.t, agent_module.CELL_BASELINE_WAIT_MS)


# --------------------------------------------------------------------------- #
# 6. a re-asked decision keeps its latency
# --------------------------------------------------------------------------- #


class EveryDecisionCarriesItsOwnLatency(unittest.TestCase):
    def test_the_record_is_timed_by_the_call_that_made_it(self):
        clock = Clock()
        sitting = AgentSitting.__new__(AgentSitting)
        sitting.clock = clock
        sitting.request = SimpleNamespace(method="three-agent")
        sitting._trajectory_inputs = lambda: SimpleNamespace(observations=())
        record = SimpleNamespace(role="trajectory", model="m", phase="select",
                                 fallback_reason=None, rationale="", repair_retries=0,
                                 options={}, stale_at_arrival=False)

        def select(_inputs):
            clock.sleep_ms(7_000)
            return SimpleNamespace(target_id="T0", control_id="C1"), record

        sitting.agents = SimpleNamespace(may_reask=None, select_next=select,
                                         monolith_select=select)
        sitting._may_reask = lambda *a, **k: False
        sitting._charge_model_time = lambda _record: None
        sitting._answered_stale = lambda _inputs: False
        pair, decision = sitting._next_decision()
        self.assertEqual(("T0", "C1"), pair)
        self.assertEqual(7_000.0, decision["decisionLatencyMs"])


# --------------------------------------------------------------------------- #
# 7. a hardware stop is not DEADLINE; the caps are named and fixed
# --------------------------------------------------------------------------- #


class TheWaitsAreBoundedAndNamed(unittest.TestCase):
    def test_the_caps(self):
        self.assertEqual(300_000.0, agent_module.HARDWARE_WAIT_CAP_MS)
        self.assertEqual(600_000.0, agent_module.EXCISED_TOTAL_CAP_MS)
        self.assertEqual(agent_module.HARDWARE_WAIT_CAP_MS, AgentSitting.HARDWARE_WAIT_CAP_MS)
        self.assertLessEqual(agent_module.ROLE_ADDRESSABLE_WAIT_MS,
                             agent_module.HARDWARE_WAIT_CAP_MS)
        self.assertEqual(4_400.0, agent_module.HANDOVER_NORMAL_MS)

    def waiting(self):
        clock = Clock()
        sitting = AgentSitting.__new__(AgentSitting)
        sitting.clock, sitting.started_ms = clock, 0.0
        sitting.request = SimpleNamespace(deadline_ms=480_000.0)
        sitting.composition_wait_ms, sitting.service_trace = 0.0, []
        sitting.stopped, sitting.termination = False, None
        sitting.search_terminated_at = None
        sitting.identities = {"ue2": SimpleNamespace(amf_ue_ngap_id=640)}
        sitting.hardware_disconnects = []
        sitting.runtime = SimpleNamespace(case_id="case/x")
        sitting.amf_of = lambda ue: None
        return sitting

    def test_a_ue_that_never_returns_ends_as_hardware_not_as_deadline(self):
        sitting = self.waiting()
        self.assertFalse(sitting._await_reregistration(["ue2"], reserve_ms=60_000))
        self.assertEqual("HARDWARE_UNAVAILABLE", sitting.termination, sitting.termination_detail)
        self.assertLessEqual(sitting.clock.t, agent_module.HARDWARE_WAIT_CAP_MS + 1000)
        wait, = sitting.hardware_disconnects[0]["waits"]
        self.assertTrue(wait["start"] and wait["end"])

    def test_the_board_s_waits_add_up_to_one_cap(self):
        sitting = self.waiting()
        sitting.composition_wait_ms = 400_000.0
        sitting.hardware_disconnects = [{"waitedMs": 250_000.0}]
        self.assertTrue(sitting._past_deadline())
        self.assertEqual("HARDWARE_UNAVAILABLE", sitting.termination)

    def test_the_cumulative_cap_also_ends_a_wait_below_its_own_cap(self):
        sitting = self.waiting()
        sitting.composition_wait_ms = 550_000.0
        self.assertFalse(sitting._await_reregistration(["ue2"], reserve_ms=60_000))
        self.assertEqual("HARDWARE_UNAVAILABLE", sitting.termination)
        self.assertLess(sitting.clock.t, 60_000)


# --------------------------------------------------------------------------- #
# 8. excision ("볼드모트"): the interval does not exist, the board is kept
# --------------------------------------------------------------------------- #


def row(t_ms, gaps=(), cells=None):
    kpis = {"dlGoodputMbps@ue1": 5.0}
    for ue, cell in dict(cells or {}).items():
        if cell is not None:
            kpis[f"servingCell@{ue}"] = cell
    return {"t": float(t_ms), "kpis": kpis, "valid": True, "gaps": list(gaps)}


class WhatIsExcised(unittest.TestCase):
    HEARTBEAT = {"ueId": "ue2", "error": "ssh exit 1: flow-goodput: no complete running heartbeat"}
    MISMATCH = {"ueId": "ue3", "kind": "flow-payload-mismatch",
                "error": "flow payload did not advance while the tun received +76136 B"}

    def marked(self, history, current):
        agent_module._mark_excision(current, history)
        return current.get("excised")

    def test_a_source_that_stopped_is_excised_a_starved_flow_is_not(self):
        self.assertEqual(["observer-gap"], self.marked([], row(0, [self.HEARTBEAT])))
        self.assertIsNone(self.marked([], row(0, [self.MISMATCH])))

    def test_only_the_part_of_a_handover_past_its_normal_time_is_excised(self):
        history = [row(0, cells={"ue1": HOME})] + [row(t, cells={"ue1": None})
                                                   for t in (1000, 2000, 3000, 4000)]
        self.assertIsNone(self.marked(history, row(4400, cells={"ue1": None})))
        self.assertEqual(["handover-overlong@ue1"],
                         self.marked(history, row(5000, cells={"ue1": None})))
        self.assertIsNone(self.marked(history, row(5000, cells={"ue1": TARGET})))

    def test_the_excised_time_relieves_b_while_it_happens(self):
        sitting = AgentSitting.__new__(AgentSitting)
        sitting.composition_wait_ms, sitting.hardware_disconnects = 0.0, []
        sitting.service_trace = [row(0), {**row(1000), "excised": ["observer-gap"]},
                                 {**row(2000), "excised": ["observer-gap"]}, row(3000)]
        self.assertEqual(2000.0, sitting._hardware_wait_ms())


    def test_rows_sampled_during_a_recorded_wait_are_not_counted_twice(self):
        """A cold start's formation sampler keeps sampling while composition waits for a
        dropped UE; those rows' heartbeat gaps are the seconds the wait already covers."""
        sitting = AgentSitting.__new__(AgentSitting)
        sitting.composition_wait_ms, sitting.hardware_disconnects = 30_000.0, []
        sitting.composition_wait_log = [{"start": "2026-09-23T12:00:00Z",
                                         "end": "2026-09-23T12:00:30Z", "waitedMs": 30_000.0}]
        base = agent_module._parse_time("2026-09-23T12:00:00Z")
        sitting.service_trace = [{**row(base + t), "excised": ["observer-gap"]}
                                 for t in range(0, 31_000, 1000)]
        # 30 s of wait plus the one row at the wait's start (outside the half-open span).
        self.assertEqual(31_000.0, sitting._hardware_wait_ms())

    def test_a_rebind_retry_does_not_charge_again_what_its_composition_charged(self):
        sitting, _calls = AFailedRebindIsRetriedThenEndsAsHardware().sitting(fail_times=1)
        sitting.composition_wait_log = []
        real = sitting.rebind_factory

        def composing_then_failing(*args, **kwargs):
            if not getattr(composing_then_failing, "done", False):
                composing_then_failing.done = True
                started = sitting.clock.now()
                sitting.clock.sleep_ms(30_000)             # _await_addressable inside
                sitting.composition_wait_ms += 30_000.0    # ... charged by composition
                # ... with its span, as ``_charge_composition_wait`` writes it live.
                sitting.composition_wait_log.append(
                    {"start": started, "end": sitting.clock.now(), "waitedMs": 30_000.0})
            return real(*args, **kwargs)

        sitting.rebind_factory = composing_then_failing
        self.assertTrue(sitting._rebind_if_reregistered())
        self.assertAlmostEqual(30_000.0 + agent_module.REBIND_RETRY_MS,
                               sitting._hardware_wait_ms(), delta=1.0)


class AnExcisedSampleDoesNotExist(unittest.TestCase):
    def test_the_window_reaches_back_over_usable_time_only(self):
        rows = [row(t) for t in range(0, 10_000, 1000)]
        for index in (4, 5):
            rows[index]["excised"] = ["observer-gap"]
            rows[index]["kpis"]["dlGoodputMbps@ue1"] = 0.0      # what the dead source said
        kept = agent_module._without_excised(rows, 1000)
        self.assertEqual(8, len(kept))
        self.assertTrue(all(item["kpis"]["dlGoodputMbps@ue1"] == 5.0 for item in kept))
        # The last row is where it was; everything before the gap moved by 2 s.
        self.assertEqual(9000.0, kept[-1]["t"])
        self.assertEqual(2000.0, kept[0]["t"])
        self.assertEqual(rows[:3], agent_module._without_excised(rows[:3], 1000))


class AJudgedWindowIsRefilled(Hermetic):
    def test_a_gap_in_a_hold_is_excised_and_the_hold_extended(self):
        sitting = hardware_free(self.tmp, budget_trials=1)
        sitting.confirm()
        real = agent_module._service_row
        state = {"trial": False, "n": 0}
        original_run_trial = AgentSitting._run_trial

        def run_trial(this, *args, **kwargs):
            state["trial"] = True
            try:
                return original_run_trial(this, *args, **kwargs)
            finally:
                state["trial"] = False

        def gappy(observer, now, errors):
            item = real(observer, now, errors)
            if state["trial"]:
                state["n"] += 1
                if state["n"] in (2, 3):
                    item["gaps"].append(dict(WhatIsExcised.HEARTBEAT, at=item["t"]))
            return item

        with patch.object(agent_module, "_service_row", gappy), \
                patch.object(AgentSitting, "_run_trial", run_trial):
            sitting.run()
        trial = sitting.grid.trials[-1]
        self.assertEqual(2, trial.window["excisedSamples"])
        self.assertEqual(2, trial.window["extensionSamples"])
        record = sitting.episode_record()
        self.assertGreater(record["hardwareWaitMs"]["excisedTraceMs"], 0)
        self.assertEqual(2, record["excision"]["excisedSamples"])
        self.assertTrue(record["excision"]["intervals"])


class TheBoardIsKept(unittest.TestCase):
    def test_disconnects_and_observer_gaps_no_longer_exclude_a_board(self):
        document = {"termination": {"reason": "DEADLINE"},
                    "hardwareDisconnects": [{"ues": {"ue2": {}}, "waitedMs": 78_000.0}],
                    "kpiObserverFailures": [dict(WhatIsExcised.HEARTBEAT, at="x")] * 30}
        self.assertEqual([], [item for item in agent_module.attribute_failures(document)
                              if item["kind"] == "external-equipment-failure"])

    def test_only_a_board_the_bed_stopped_is_attributed_with_its_evidence(self):
        document = {"termination": {"reason": "HARDWARE_UNAVAILABLE", "detail": "cap"},
                    "excision": {"totalMs": 612_000.0, "capMs": 600_000.0,
                                 "intervals": [{"start": "a", "end": "b"}]}}
        failure, = agent_module.attribute_failures(document)
        self.assertEqual(("external-equipment-failure", "external"),
                         (failure["kind"], failure["cause"]))
        self.assertEqual(612_000.0, failure["evidence"]["excisedTotalMs"])


class TheDeficitClockSkipsTheExcisedTime(unittest.TestCase):
    def test_points_inside_are_dropped_and_later_points_move_earlier(self):
        from experiments.agent_metrics import _service_elapsed
        episode = {"timing": {"t0": "2026-09-23T12:00:00Z"},
                   "excision": {"intervals": [{"start": "2026-09-23T12:00:10Z",
                                               "end": "2026-09-23T12:00:40Z"}]}}
        self.assertEqual(5_000.0, _service_elapsed(episode, {"t": "2026-09-23T12:00:05Z"}))
        self.assertIsNone(_service_elapsed(episode, {"t": "2026-09-23T12:00:20Z"}))
        self.assertIsNone(_service_elapsed(episode, {"t": "2026-09-23T12:00:05Z",
                                                     "excised": ["observer-gap"]}))
        self.assertEqual(20_000.0, _service_elapsed(episode, {"t": "2026-09-23T12:00:50Z"}))
        # An episode written before excision existed reads as it always did.
        self.assertEqual(50_000.0, _service_elapsed({"timing": episode["timing"]},
                                                    {"t": "2026-09-23T12:00:50Z"}))


if __name__ == "__main__":
    unittest.main()
