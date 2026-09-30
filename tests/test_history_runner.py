#!/usr/bin/env python3
"""Batch E (P0-15): runner-level history ablation controls.

Validated HistoryPolicy / HistoryResetScope, deterministic reset-scope
semantics, method-order independence, the frozen-empty rejection, and the raw
metadata (policy / scope / snapshot id + size).
"""

import types
import unittest

from experiments.runner import ExperimentRunner
from experiments.environment import (
    ExogenousEnvironmentDriver, ENVIRONMENT_AXES,
)
from coordinator.history import (
    HistoryMode, HistoryResetScope, snapshot_records, restore_records,
)


class _ApprovedEnvOnlyDriver(ExogenousEnvironmentDriver):
    """TEST-ONLY, explicitly-APPROVED, ENV-ONLY exogenous driver for the history
    scope/order tests. It supports EXACTLY the default phases' two channel-gain
    environment axes and its apply() moves them to the phase's ACTUAL requested
    values, reporting the honest {axis: value} it applied. It touches NO
    coordinator action axis and produces NO real/OTA measurement - it exists only
    to satisfy the P0-19 live preflight without weakening it (never a channel
    model, never env_driver=None, never a monkeypatched preflight)."""
    supported_axes = frozenset({"channel_serving_gain_db",
                                "channel_neighbor_gain_db"})
    approved = True
    name = "test-history-approved-env-only-driver"

    def apply(self, phase, phase_idx, phase_label):
        # honest deterministic ENV-only application: exactly the two channel-gain
        # axes at the phase's requested values (default 0) - mirroring the runner's
        # _phase_requested_env, so validate_applied_environment matches exactly.
        return {"channel_serving_gain_db": float(phase.get("serving_gain", 0)),
                "channel_neighbor_gain_db": float(phase.get("neighbor_gain", 0))}


def _rec_dict(ev):
    return {"intent_signature": "sig", "action_axes": ["prb"],
            "operating_regime": "regimeX", "proposer_id": "A",
            "model_version": "modelA", "evidence_id": ev,
            "terminal_outcome": "commit_original", "terminal_reason": "",
            "created_at": ""}


class _FakeCoord:
    """A coordinator stub exposing exactly the history API the runner uses,
    plus an ONLINE-learning simulation (each trial appends one record)."""
    def __init__(self):
        self.history_reservoir = []
        self.history_mode = None
        self._frozen = tuple()
        self._model = "modelA"
        self.calibrator = types.SimpleNamespace(reset=lambda: None)
        self.executor = None
        self.ue_collector = None

    def set_history_mode(self, m):
        self.history_mode = HistoryMode.coerce(m)
        return self.history_mode

    def restore_history_snapshot(self, snap):
        self.history_reservoir = restore_records(snap)

    def set_frozen_history(self, snap):
        self._frozen = snapshot_records(snap)

    def set_probe_config(self, cfg):
        # Batch F: accept the mandatory probe-config install (no real probe here).
        self._probe_config = cfg

    def _active_model_id(self):
        return self._model


def _runner(coord, scope="trial", policy="online", snapshot=()):
    r = ExperimentRunner(coordinator=coord, output_dir="experiment_results")
    r.history_reset_scope = scope
    r.history_policy = policy
    r.paper_history_snapshot = tuple(snapshot)
    # P0-19: inject an APPROVED env-only exogenous driver so the LIVE run_live
    # preflight is honestly satisfied (channel_model stays None) while
    # _run_live_trial remains free to be instrumented for history scope/order.
    r.env_driver = _ApprovedEnvOnlyDriver()
    return r


def _instrument(runner, coord, starts):
    """Replace _run_live_trial with a capture that records the reservoir size
    at trial start and simulates one online-learned record."""
    def _trial(self, method, trial_id):
        starts.append((method, trial_id, len(coord.history_reservoir)))
        coord.history_reservoir.append(_rec_dict(f"{method}-{trial_id}"))
        return [], []
    runner._run_live_trial = types.MethodType(_trial, runner)


class ResetScopeTest(unittest.TestCase):

    def _run(self, scope, methods, trials=2):
        coord = _FakeCoord()
        r = _runner(coord, scope=scope)
        starts = []
        _instrument(r, coord, starts)
        r.run_live(methods, trials=trials)
        return starts

    def test_trial_scope_resets_every_trial(self):
        starts = self._run("trial", ["llm_with_history"], trials=3)
        self.assertEqual([s[2] for s in starts], [0, 0, 0])

    def test_run_scope_accumulates_across_trials(self):
        starts = self._run("run", ["llm_with_history"], trials=3)
        self.assertEqual([s[2] for s in starts], [0, 1, 2])

    def test_method_scope_resets_per_method(self):
        starts = self._run("method",
                           ["llm_with_history", "llm_no_history"], trials=2)
        # each method restarts at 0, accumulates within its own trials
        self.assertEqual([s[2] for s in starts], [0, 1, 0, 1])

    def test_model_scope_resets_only_on_model_change(self):
        coord = _FakeCoord()
        r = _runner(coord, scope="model")
        starts = []
        _instrument(r, coord, starts)
        # model stays modelA the whole run -> reset once, then accumulate
        r.run_live(["llm_with_history"], trials=3)
        self.assertEqual([s[2] for s in starts], [0, 1, 2])

    def test_invalid_scope_fails_closed(self):
        coord = _FakeCoord()
        r = _runner(coord, scope="weekly")
        _instrument(r, coord, [])
        with self.assertRaises(ValueError):
            r.run_live(["llm_with_history"], trials=1)


class MethodOrderIndependenceTest(unittest.TestCase):

    def _starts(self, methods):
        coord = _FakeCoord()
        r = _runner(coord, scope="trial")
        starts = []
        _instrument(r, coord, starts)
        r.run_live(methods, trials=2)
        return {(m, t): n for (m, t, n) in starts}

    def test_reversed_method_order_identical_per_trial_state(self):
        fwd = self._starts(["llm_no_history", "llm_with_history"])
        rev = self._starts(["llm_with_history", "llm_no_history"])
        # TRIAL scope: every (method, trial) starts from the SAME reservoir
        # size regardless of method order (no leakage across method/trial).
        self.assertEqual(fwd, rev)
        self.assertTrue(all(v == 0 for v in fwd.values()))

    def test_paired_methods_share_snapshot_differ_only_in_mode(self):
        coord = _FakeCoord()
        r = _runner(coord, scope="trial")
        r._configure_method("llm_no_history")
        self.assertIs(coord.history_mode, HistoryMode.DISABLED)
        r._configure_method("llm_with_history")
        self.assertIs(coord.history_mode, HistoryMode.ONLINE_RESERVOIR)


class FrozenEmptyRejectionTest(unittest.TestCase):

    def test_frozen_empty_snapshot_is_rejected(self):
        coord = _FakeCoord()
        r = _runner(coord, policy="frozen", snapshot=())
        with self.assertRaises(ValueError):
            r._configure_method("llm_with_history")

    def test_frozen_with_nonempty_snapshot_ok(self):
        coord = _FakeCoord()
        snap = snapshot_records([_rec_dict("seed-1")])
        r = _runner(coord, policy="frozen", snapshot=snap)
        r.paper_history_snapshot = snap
        self.assertTrue(r._configure_method("llm_with_history"))
        self.assertIs(coord.history_mode, HistoryMode.FROZEN_RESERVOIR)


class PairedRngStateTest(unittest.TestCase):
    """REAL random-consuming forward/reverse test with UNEQUAL consumption:
    no-history consumes 1 draw/trial, with-history consumes 2. Deterministic
    per-(experiment_seed, trial, model/config) reseeding must make the STARTING
    first draw + backend counter identical for every (method, trial) regardless
    of method order (the exact gate the coordinator re-probed)."""

    def _make(self, seed=123):
        import types
        from experiments.emulation import ChannelModel
        from decision.llm_backend import DeterministicMockBackend
        channel = ChannelModel(seed=seed)
        mock = DeterministicMockBackend(seed=seed + 1)
        coord = types.SimpleNamespace(
            llm_manager=types.SimpleNamespace(
                dynamic_backends={"mock:deterministic": mock}))
        r = _runner(coord, scope="trial")
        r.channel_model = channel
        r.experiment_seed = seed
        return r, coord, channel, mock

    def _draw(self, r, coord, channel, mock, method, trial):
        # RESEED to this trial's deterministic pair seed, then take the FIRST
        # draw + first backend call; THEN consume UNEQUALLY by method.
        r._pair_state_sync(coord, method, trial)
        first = round(channel.throughput("gnb1", "gnb2", 1.0, 0.0), 10)
        resp = mock.generate("assess feasibility of intent")
        counter = mock._n_feasibility
        # unequal downstream consumption (must NOT affect the NEXT trial's start)
        n_extra = 2 if method == "llm_with_history" else 0
        for _ in range(n_extra):
            channel.throughput("gnb1", "gnb2", 1.0, 0.0)
            mock.generate("more feasibility")
        return (first, resp.content, counter)

    def _order(self, methods, seed=123):
        r, coord, channel, mock = self._make(seed)
        draws = {}
        for m in methods:
            for t in (1, 2, 3):
                draws[(m, t)] = self._draw(r, coord, channel, mock, m, t)
        return draws

    def test_unequal_consumption_forward_reverse_identical(self):
        fwd = self._order(["llm_no_history", "llm_with_history"])
        rev = self._order(["llm_with_history", "llm_no_history"])
        for t in (1, 2, 3):
            # the STARTING draw for each method+trial is identical across order
            self.assertEqual(fwd[("llm_no_history", t)],
                             rev[("llm_no_history", t)])
            self.assertEqual(fwd[("llm_with_history", t)],
                             rev[("llm_with_history", t)])
            # AND the paired methods start trial T identically (only history
            # availability differs, never the RNG start / backend counter)
            self.assertEqual(fwd[("llm_no_history", t)],
                             fwd[("llm_with_history", t)])

    def test_exact_probe_first_draw_equal(self):
        # the coordinator's exact inline probe: seed 123, method-outer order,
        # trial 2 first draw must be EQUAL across forward/reverse.
        fwd = self._order(["llm_no_history", "llm_with_history"], seed=123)
        rev = self._order(["llm_with_history", "llm_no_history"], seed=123)
        first_equal = (fwd[("llm_no_history", 2)][0]
                       == rev[("llm_no_history", 2)][0])
        self.assertTrue(first_equal, "FIRST_DRAW_EQUAL must be True")

    def test_disabled_pairing_diverges(self):
        # sanity: with reseeding OFF, unequal consumption makes the second
        # method's start diverge (proves the reseed is what enforces pairing).
        r, coord, channel, mock = self._make(999)
        r.pair_state_enabled = False
        a1 = self._draw(r, coord, channel, mock, "llm_with_history", 1)
        a2 = self._draw(r, coord, channel, mock, "llm_no_history", 1)
        self.assertNotEqual(a1[0], a2[0])

    def test_unrelated_baseline_is_not_reseeded(self):
        # a non-paired baseline must NOT be reseeded (no accidental pairing):
        # its channel RNG advances freely, so two _pair_state_syncs leave the
        # stream un-reset (distinct consecutive draws), unlike a paired method.
        r, coord, channel, mock = self._make(321)
        r._pair_state_sync(coord, "rule_based", 1)
        d1 = channel.throughput("gnb1", "gnb2", 1.0, 0.0)
        r._pair_state_sync(coord, "rule_based", 1)     # same trial, baseline
        d2 = channel.throughput("gnb1", "gnb2", 1.0, 0.0)
        self.assertNotEqual(d1, d2)                    # NOT reseeded -> advanced
        # whereas a paired method IS reset to the same seed at the same trial
        r._pair_state_sync(coord, "llm_with_history", 5)
        p1 = channel.throughput("gnb1", "gnb2", 1.0, 0.0)
        r._pair_state_sync(coord, "llm_with_history", 5)
        p2 = channel.throughput("gnb1", "gnb2", 1.0, 0.0)
        self.assertEqual(p1, p2)                       # reseeded -> identical


class PairedPromptEqualityTest(unittest.TestCase):
    """Against the SAME controlled non-empty snapshot, the only difference in a
    paired feasibility prompt is the history section: DISABLED sends zero
    records; ONLINE/FROZEN sends the intended ones; everything else is
    byte-identical."""

    def _coord_capturing_prompts(self):
        import threading
        from coordinator.intent_coordinator import IntentCoordinator
        from coordinator.proposer import ProposerContext
        from coordinator.history import HistoryMode
        from decision.llm_backend import LLMResponse

        captured = {}

        class _Backend:
            name = "A"
            model = "modelA"

            def generate(self, prompt, system_prompt=""):
                captured["prompt"] = prompt
                captured["system"] = system_prompt
                return LLMResponse(success=True, content="{}", model="modelA")
        backend = _Backend()

        class _Mgr:
            def analyze_feasibility_with(self, obj, a, n, s, h=None):
                p = self._build(a, n, s, h)
                return obj.generate(p, "SYS")

            def build_feasibility_prompt_hash(self, active, new, state,
                                              history=None):
                # a real full SHA-256 of the exact prompt (the coordinator no
                # longer swallows a missing/failed prompt-hash call).
                import hashlib
                p = self._build(active, new, state, history)
                return "prompt-sha256-" + hashlib.sha256(
                    p.encode("utf-8")).hexdigest()

            def _build(self, active, new, state, history):
                import json
                parts = ["ACTIVE=" + json.dumps(active),
                         "NEW=" + json.dumps(new),
                         "STATE=" + json.dumps(state)]
                if history:
                    parts.append("=== HISTORY ===")
                    for r in history:
                        parts.append(json.dumps(r))
                return "\n".join(parts)
        c = IntentCoordinator.__new__(IntentCoordinator)
        c.llm_manager = _Mgr()
        c._proposer_ctx = ProposerContext(proposer_id="A", backend_name="A",
                                          model_version="modelA",
                                          backend_object=backend)
        c._episode_context = ("modelA", "regimeX")
        c.current_phase = "regimeX"
        c._calibration_context_ok = True
        c._last_intent_signature = None
        c._frozen_history = tuple()
        c.history_reservoir = []
        c.max_history = 100
        return c, backend, captured

    def test_only_history_section_differs(self):
        import json
        from coordinator.history import (
            HistoryMode, snapshot_records)
        from decision.intent_model import (
            ConstraintType, Intent, IntentScope, IntentTarget, IntentType)
        from decision.intent_model import NetworkState
        c, backend, captured = self._coord_capturing_prompts()

        # build the intents ONCE (fresh construction assigns random ids) and
        # REUSE them for both runs, so only the history section can differ.
        intent = Intent(type=IntentType.THROUGHPUT_GOAL,
                        target=IntentTarget(kpi_name="throughput",
                                            constraint_type=ConstraintType.MIN,
                                            target_value=8.0, unit="Mbps"),
                        scope=IntentScope(ue_ids=["ue1"]))
        active = [Intent(type=IntentType.THROUGHPUT_GOAL,
                         target=IntentTarget(kpi_name="throughput",
                                             constraint_type=ConstraintType.MIN,
                                             target_value=5.0, unit="Mbps"))]
        state = NetworkState(ue_states={})

        def _run(mode, snapshot):
            c.set_history_mode(mode)
            c.history_reservoir = list(snapshot)
            c._frozen_history = snapshot_records(snapshot)
            captured.clear()
            c._analyze_feasibility(intent, active, state)
            return captured["prompt"], captured["system"]

        from coordinator.history import HistoryRecord
        sig = c._intent_content_hash(intent)
        snap = [HistoryRecord(intent_signature=sig, action_axes=("prb",),
                              operating_regime="regimeX", proposer_id="A",
                              model_version="modelA", evidence_id="evid-1")]

        disabled_prompt, disabled_sys = _run(HistoryMode.DISABLED, snap)
        online_prompt, online_sys = _run(HistoryMode.ONLINE_RESERVOIR, snap)

        # systems identical; DISABLED carries NO history section
        self.assertEqual(disabled_sys, online_sys)
        self.assertNotIn("=== HISTORY ===", disabled_prompt)
        self.assertIn("=== HISTORY ===", online_prompt)
        # after stripping the history section, the rest is byte-identical
        online_head = online_prompt.split("=== HISTORY ===")[0]
        self.assertEqual(disabled_prompt, online_head.rstrip("\n"))


class MetadataTest(unittest.TestCase):

    def test_run_meta_records_history_controls(self):
        coord = _FakeCoord()
        r = _runner(coord, scope="method", policy="online")
        r.history_snapshot_id = "paper-v1"
        r.paper_history_snapshot = snapshot_records([_rec_dict("m1")])
        meta = r._run_meta("emulated", seed=7)
        h = meta["history"]
        self.assertEqual(h["policy"], "online")
        self.assertEqual(h["reset_scope"], "method")
        self.assertEqual(h["snapshot_id"], "paper-v1")
        self.assertEqual(h["shared_start_snapshot_size"], 1)
        self.assertIn("model_switch_audit", meta)


class ApprovedEnvDriverTest(unittest.TestCase):
    """The injected fixture driver is genuinely approved + environment-only (it
    satisfies the P0-19 preflight honestly, it does not weaken it)."""

    def test_injected_driver_is_approved_and_env_only(self):
        d = _ApprovedEnvOnlyDriver()
        self.assertIs(d.approved, True)                 # EXACTLY True (not truthy)
        self.assertTrue(d.supported_axes)               # nonempty
        self.assertTrue(set(d.supported_axes) <= ENVIRONMENT_AXES)  # env-only
        # apply reports EXACTLY the phase's requested channel-gain values
        self.assertEqual(
            d.apply({"name": "Nominal", "serving_gain": 3, "neighbor_gain": 0},
                    0, "p0:Nominal"),
            {"channel_serving_gain_db": 3.0, "channel_neighbor_gain_db": 0.0})


if __name__ == "__main__":
    unittest.main()
