"""Track T1: the state bus, the application chrome and the session lifecycle.

Everything here is hermetic - no display, no hardware, no LLM, no network.  The
console is deliberately built so that this is possible: the toolkit is imported
inside ``build()``, and every rule that could mislead an operator is a pure
function that can be asserted directly.

This module is also the **performance harness** for gate ``G-RESPONSIVENESS``.
``python3 -m tests.gui.test_shell_and_bus --soak 1800`` runs the thirty-minute
Replay soak and prints tick latency, RSS growth and bus high-water as numbers.
The unit test runs the same code path for a few seconds so the harness itself
cannot rot between soaks.
"""

import ast
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from gui.operator import status as st
from gui.operator.session.controller import SessionController, SessionError
from gui.operator.session.preflight import PreflightRunner, blocking_failures
from gui.operator.session.profile import (
    ExperimentProfile, ProfileError, default_profile,
)
from gui.operator.shell import confirm as confirm_mod
from gui.operator.shell.footer import footer_actions, footer_fields
from gui.operator.shell.header import format_bytes, format_duration, header_fields
from gui.operator.shell.window import ConsoleWindow, TickMonitor
from gui.operator.store import session_store as store_module
from gui.operator.viewmodel.bus import CHANNELS, UNKNOWN_CHANNEL_KIND, StateBus
from gui.operator.viewmodel.types import (
    ComponentStatusView, SessionState, TimelineEventView,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #


class FakeStore:
    """A stand-in for the SessionStore body, which track T3 owns.

    It implements exactly the frozen signature this track consumes, so when the
    real store lands the controller needs no change.  It is a stub, not a
    reimplementation: it records what it was asked to write and nothing else.
    """

    def __init__(self, runs_root, *, mode, mode_evidence, profile_id=None,
                 config_snapshot=None, fail_on_event=False):
        self.run_dir = Path(runs_root) / f"{mode.lower()}-fake"
        self.run_id = self.run_dir.name
        self.mode = mode
        self.mode_evidence = dict(mode_evidence)
        self.profile_id = profile_id
        self.config_snapshot = dict(config_snapshot or {})
        self.disposition = "RUNNING"
        self.events = []
        self.finalized = None
        self.fail_on_event = fail_on_event

    def append_event(self, event):
        if self.fail_on_event:
            raise OSError("disk full")
        self.events.append(dict(event))

    def finalize(self, disposition):
        self.disposition = disposition
        self.finalized = {"runId": self.run_id, "mode": self.mode,
                          "disposition": disposition,
                          "events": len(self.events)}
        return self.finalized


def store_factory(**overrides):
    def factory(runs_root, *, mode, mode_evidence, profile_id=None,
                config_snapshot=None):
        return FakeStore(runs_root, mode=mode, mode_evidence=mode_evidence,
                         profile_id=profile_id,
                         config_snapshot=config_snapshot, **overrides)
    return factory


def make_controller(tmp, **kwargs):
    bus = StateBus()
    profile = default_profile().with_overrides(runs_root=str(tmp))
    controller = SessionController(bus, profile=profile,
                                   store_factory=store_factory(**kwargs))
    return bus, controller


# --------------------------------------------------------------------------- #
# StateBus
# --------------------------------------------------------------------------- #


class StateBusThreading(unittest.TestCase):

    def test_publish_is_safe_from_many_threads(self):
        bus = StateBus(max_queue=100_000)
        threads = [threading.Thread(
            target=lambda n=n: [bus.publish("metric", (n, i))
                                for i in range(500)]) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        received = []
        bus.subscribe("metric", received.append)
        while bus.drain(budget=1024):
            pass
        self.assertEqual(len(received), 8 * 500)
        self.assertEqual(bus.dropped(), 0)

    def test_drain_dispatches_in_publish_order(self):
        bus = StateBus()
        seen = []
        bus.subscribe("timeline", seen.append)
        for index in range(50):
            bus.publish("timeline", index)
        self.assertEqual(bus.drain(), 50)
        self.assertEqual(seen, list(range(50)))

    def test_publish_many_is_not_interleaved(self):
        """A batch enters the queue under one lock, so ordering is preserved."""
        bus = StateBus()
        stop = threading.Event()

        def noise():
            while not stop.is_set():
                bus.publish("metric", "x")

        worker = threading.Thread(target=noise, daemon=True)
        worker.start()
        try:
            batch = [("timeline", i) for i in range(20)]
            bus.publish_many(batch)
        finally:
            stop.set()
            worker.join(timeout=2)
        seen = []
        bus.subscribe("timeline", seen.append)
        while bus.drain(budget=4096):
            pass
        self.assertEqual(seen, list(range(20)))

    def test_drain_honours_the_budget(self):
        bus = StateBus()
        bus.subscribe("metric", lambda _payload: None)
        for index in range(20):
            bus.publish("metric", index)
        self.assertEqual(bus.drain(budget=5), 5)
        self.assertEqual(bus.depth(), 15)


class StateBusIsolation(unittest.TestCase):

    def test_a_raising_subscriber_does_not_affect_the_others(self):
        bus = StateBus()
        before, after = [], []
        bus.subscribe("warning", before.append)
        bus.subscribe("warning", lambda _p: (_ for _ in ()).throw(
            RuntimeError("widget failure")))
        bus.subscribe("warning", after.append)
        bus.publish("warning", "w")
        with self.assertLogs("gui.operator.bus", level="ERROR"):
            self.assertEqual(bus.drain(), 1)
        self.assertEqual(before, ["w"])
        self.assertEqual(after, ["w"])

    def test_a_raising_subscriber_does_not_reach_the_publisher(self):
        bus = StateBus()
        bus.subscribe("metric", lambda _p: (_ for _ in ()).throw(ValueError()))
        bus.publish("metric", 1)
        with self.assertLogs("gui.operator.bus", level="ERROR"):
            bus.drain()
        bus.publish("metric", 2)          # publisher is unharmed

    def test_unsubscribe_stops_delivery(self):
        bus = StateBus()
        seen = []
        cancel = bus.subscribe("intent", seen.append)
        bus.publish("intent", "a")
        bus.drain()
        cancel()
        bus.publish("intent", "b")
        bus.drain()
        self.assertEqual(seen, ["a"])
        cancel()                          # idempotent


class StateBusBackPressure(unittest.TestCase):

    def test_back_pressure_sheds_oldest_and_reports(self):
        bus = StateBus(max_queue=10)
        seen = []
        bus.subscribe("metric", seen.append)
        for index in range(1000):
            bus.publish("metric", index)
        self.assertEqual(bus.depth(), 10)
        self.assertEqual(bus.dropped(), 990)
        bus.drain(budget=100)
        self.assertEqual(seen, list(range(990, 1000)),
                         "back-pressure must shed the OLDEST records")

    def test_the_queue_never_grows_without_bound(self):
        bus = StateBus(max_queue=64)
        for index in range(100_000):
            bus.publish("metric", index)
        self.assertLessEqual(bus.depth(), 64)
        self.assertEqual(bus.stats()["highWater"], 64)

    def test_publish_never_raises_and_never_blocks(self):
        bus = StateBus(max_queue=2)
        started = time.monotonic()
        for index in range(5000):
            bus.publish("metric", index)
        self.assertLess(time.monotonic() - started, 5.0)


class StateBusChannels(unittest.TestCase):

    def test_an_unknown_channel_is_loud_but_not_fatal(self):
        """A typo'd channel must be visible, and must not kill the publisher."""
        bus = StateBus()
        warnings = []
        bus.subscribe("warning", warnings.append)
        with self.assertLogs("gui.operator.bus", level="ERROR"):
            bus.publish("typo", {"value": 1})
        bus.drain()
        self.assertEqual(bus.rejected(), 1)
        self.assertEqual(warnings[0]["kind"], UNKNOWN_CHANNEL_KIND)
        self.assertEqual(warnings[0]["channel"], "typo")
        self.assertIn("'value': 1", warnings[0]["record"],
                      "the misrouted record must be identifiable, not just "
                      "counted")

    def test_a_misrouted_record_description_is_bounded_and_cannot_raise(self):
        class Hostile:
            def __repr__(self):
                raise RuntimeError("no repr for you")

        bus = StateBus()
        warnings = []
        bus.subscribe("warning", warnings.append)
        with self.assertLogs("gui.operator.bus", level="ERROR"):
            bus.publish("typo", Hostile())
            bus.publish("typo", "x" * 10_000)
        bus.drain(budget=16)
        self.assertIn("unrepresentable Hostile", warnings[0]["record"])
        self.assertTrue(warnings[1]["record"].endswith("(truncated)"))
        self.assertLess(len(warnings[1]["record"]), 400)

    def test_subscribe_to_an_unknown_channel_fails_at_wiring_time(self):
        with self.assertRaises(ValueError):
            StateBus().subscribe("typo", lambda _p: None)

    def test_snapshot_returns_the_last_value(self):
        """A workspace activated later repaints from the snapshot."""
        bus = StateBus()
        self.assertIsNone(bus.snapshot("session"))
        bus.publish("session", "first")
        bus.publish("session", "second")
        self.assertEqual(bus.snapshot("session"), "second")

    def test_every_declared_channel_is_subscribable(self):
        bus = StateBus()
        for channel in CHANNELS:
            bus.subscribe(channel, lambda _p: None)


# --------------------------------------------------------------------------- #
# Header, footer and confirmation
# --------------------------------------------------------------------------- #


class HeaderChrome(unittest.TestCase):

    def test_the_seven_required_readouts_are_present(self):
        keys = {field.key for field in header_fields(SessionState())}
        for required in ("run", "profile", "elapsed", "health", "llm",
                         "recording", "alert", "mode"):
            self.assertIn(required, keys)

    def test_an_empty_state_never_claims_live(self):
        """And no longer claims REPLAY either.

        A console that has opened nothing is DISCONNECTED.  Reading REPLAY there
        was the milder version of the same fault this test exists to catch: a
        mode badge asserting a data source that is not attached.
        """
        fields = {f.key: f for f in header_fields(SessionState())}
        self.assertEqual(fields["mode"].value, "DISCONNECTED")
        self.assertIn("not a live radio", fields["mode"].detail or "")
        self.assertFalse(SessionState().is_live)

    def test_absent_values_render_the_placeholder_not_a_default(self):
        fields = {f.key: f for f in header_fields(SessionState())}
        self.assertEqual(fields["run"].value, st.PRE_MEASUREMENT)
        self.assertEqual(fields["profile"].value, st.PRE_MEASUREMENT)
        self.assertEqual(fields["elapsed"].value, st.PRE_MEASUREMENT)

    def test_every_readout_is_glyph_led_not_colour_only(self):
        for field in header_fields(SessionState()):
            self.assertTrue(field.as_text().startswith(field.glyph))
            self.assertNotEqual(field.glyph, "")

    def test_health_reports_the_worst_element(self):
        state = SessionState(health=st.ERROR,
                             health_counts={st.OK: 5, st.ERROR: 1})
        fields = {f.key: f for f in header_fields(state)}
        self.assertEqual(fields["health"].status, st.ERROR)
        self.assertIn("ERROR:1", fields["health"].detail)

    def test_the_latest_alert_carries_its_identifiers(self):
        warning = TimelineEventView(
            seq=1, lane="A1", kind="POLICY_ERROR", severity="ERROR",
            title="readback mismatch", intent_id="i-1", policy_id="p-1",
            run_id="r-1", component="nonrt-ric",
            t_utc="2026-08-14T10:00:00Z")
        fields = {f.key: f
                  for f in header_fields(SessionState(last_warning=warning))}
        detail = fields["alert"].detail
        for token in ("intent=i-1", "policy=p-1", "run=r-1",
                      "component=nonrt-ric", "2026-08-14T10:00:00Z"):
            self.assertIn(token, detail)

    def test_an_unreported_byte_count_is_not_rendered_as_zero(self):
        """"The store has not said" and "nothing was written" are different."""
        unreported = {f.key: f for f in header_fields(
            SessionState(run_id="r", recording=True))}["recording"]
        reported = {f.key: f for f in header_fields(
            SessionState(run_id="r", recording=True,
                         recorded_bytes=2048))}["recording"]
        self.assertIn(st.PRE_MEASUREMENT, unreported.value)
        self.assertIn("2.0 KiB", reported.value)

    def test_duration_and_byte_formatting_refuse_to_invent_values(self):
        self.assertEqual(format_duration(None), st.PRE_MEASUREMENT)
        self.assertEqual(format_duration(float("nan")), st.PRE_MEASUREMENT)
        self.assertEqual(format_duration(True), st.PRE_MEASUREMENT)
        self.assertEqual(format_duration(3725), "1:02:05")
        self.assertEqual(format_bytes(None), st.PRE_MEASUREMENT)
        self.assertEqual(format_bytes(0), "0 B")


class FooterChrome(unittest.TestCase):

    def test_shed_updates_are_visible(self):
        fields = {f.key: f for f in footer_fields(
            SessionState(), bus_stats={"dropped": 7, "depth": 3})}
        self.assertEqual(fields["dropped"].value, "7")
        self.assertEqual(fields["dropped"].status, st.DEGRADED)

    def test_actions_are_disabled_with_a_stated_reason(self):
        idle = {a.key: a for a in footer_actions(SessionState())}
        self.assertFalse(idle["abort"].enabled)
        self.assertTrue(idle["abort"].reason)
        self.assertTrue(idle["start"].enabled)
        running = {a.key: a for a in footer_actions(
            SessionState(run_id="r-1", disposition="RUNNING"))}
        self.assertTrue(running["abort"].enabled)
        self.assertFalse(running["start"].enabled)
        self.assertTrue(running["start"].reason)

    def test_abort_is_marked_destructive(self):
        actions = {a.key: a for a in footer_actions(SessionState())}
        self.assertTrue(actions["abort"].destructive)


class Confirmation(unittest.TestCase):

    def test_abort_and_withdraw_require_a_typed_phrase(self):
        for action_id in ("C-SESSION-ABORT", "C-INTENT-WITHDRAW"):
            spec = confirm_mod.spec_for(action_id, title="t", targets=("a",),
                                        effects=("b",))
            self.assertEqual(spec.acknowledgement, confirm_mod.TYPED_CONFIRM)
            self.assertTrue(spec.irreversible)

    def test_submit_and_llm_switch_are_single_confirm(self):
        for action_id in ("C-INTENT-SUBMIT", "C-LLM-SWITCH"):
            spec = confirm_mod.spec_for(action_id, title="t", targets=("a",),
                                        effects=("b",))
            self.assertEqual(spec.acknowledgement, confirm_mod.SINGLE_CONFIRM)

    def test_a_typed_confirmation_refuses_a_near_miss(self):
        spec = confirm_mod.spec_for("C-SESSION-ABORT", title="t",
                                    targets=("run r-1",), effects=("aborted",))
        self.assertFalse(confirm_mod.evaluate_confirmation(
            spec, acknowledged=True, typed="abort").confirmed)
        self.assertFalse(confirm_mod.evaluate_confirmation(
            spec, acknowledged=True, typed="").confirmed)
        self.assertTrue(confirm_mod.evaluate_confirmation(
            spec, acknowledged=True, typed=" ABORT ").confirmed)

    def test_not_acknowledged_is_always_refused(self):
        spec = confirm_mod.spec_for("C-INTENT-SUBMIT", title="t",
                                    targets=("a",), effects=("b",))
        self.assertFalse(confirm_mod.evaluate_confirmation(
            spec, acknowledged=False).confirmed)

    def test_the_dialog_text_states_targets_and_effects(self):
        spec = confirm_mod.spec_for("C-SESSION-ABORT", title="Abort run r-1",
                                    targets=("run r-1", "3 events"),
                                    effects=("disposition ABORTED",))
        text = confirm_mod.describe(spec)
        for token in ("run r-1", "3 events", "disposition ABORTED",
                      "cannot be undone"):
            self.assertIn(token, text)


# --------------------------------------------------------------------------- #
# Profile
# --------------------------------------------------------------------------- #


class Profiles(unittest.TestCase):

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = default_profile("p1").with_overrides(
                runs_root=str(Path(tmp) / "runs"),
                credential_refs=("env:ANTHROPIC_API_KEY",))
            path = profile.save(Path(tmp) / "p1.json")
            loaded = ExperimentProfile.load(path)
            self.assertEqual(loaded.profile_id, "p1")
            self.assertEqual(loaded.credential_refs, ("env:ANTHROPIC_API_KEY",))
            self.assertEqual(loaded.source_path, str(path))

    def test_a_literal_secret_is_refused_not_redacted(self):
        with self.assertRaises(ProfileError):
            ExperimentProfile.from_dict(
                {"profileId": "p", "api_key": "sk-live-000000000000"})
        with self.assertRaises(ProfileError):
            ExperimentProfile.from_dict(
                {"profileId": "p", "credentialRefs": ["sk-live-00000000"]})

    def test_a_credential_reference_is_accepted(self):
        profile = ExperimentProfile.from_dict(
            {"profileId": "p", "credentialRefs": ["env:OPENAI_API_KEY",
                                                  "file:/tmp/creds"]})
        self.assertEqual(len(profile.credential_refs), 2)

    def test_credential_summary_never_carries_a_value(self):
        profile = default_profile().with_overrides(
            credential_refs=("env:AIC_TEST_TOKEN_THAT_IS_NOT_SET",))
        summary = profile.credential_summary()
        self.assertEqual(summary[0]["configured"], False)
        self.assertNotIn("value", summary[0])

    def test_validation_separates_blocking_from_advisory(self):
        profile = default_profile().with_overrides(refresh_interval_ms=5000,
                                                   graph_window_s=-1.0)
        issues = {issue.field: issue for issue in profile.validate()}
        self.assertEqual(issues["refreshIntervalMs"].severity, "WARNING")
        self.assertEqual(issues["graphWindowS"].severity, "ERROR")
        self.assertFalse(profile.is_valid)

    def test_a_malformed_profile_raises_rather_than_defaulting(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ProfileError):
                ExperimentProfile.load(path)


# --------------------------------------------------------------------------- #
# Preflight
# --------------------------------------------------------------------------- #


class Preflight(unittest.TestCase):

    def test_every_check_reports_a_status_and_a_reason_or_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = PreflightRunner(
                default_profile().with_overrides(runs_root=tmp)).run()
            self.assertTrue(results)
            for view in results:
                self.assertIn(view.status, (st.OK, st.ERROR, st.UNSUPPORTED,
                                            st.UNKNOWN, st.UNAVAILABLE,
                                            st.DEGRADED, st.BLOCKED))
                self.assertTrue(view.reason or view.detail,
                                f"{view.check_id} states nothing")

    def test_an_absent_boundary_is_unsupported_not_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = {v.check_id: v for v in PreflightRunner(
                default_profile().with_overrides(runs_root=tmp)).run()}
            self.assertEqual(results["PF-R1-BOOTSTRAP"].status, st.UNSUPPORTED)
            self.assertEqual(results["PF-READINESS"].status, st.UNSUPPORTED)
            self.assertIn("GAP-01", results["PF-READINESS"].reason)
            self.assertFalse(blocking_failures(tuple(results.values())))

    def test_an_unwritable_runs_root_blocks(self):
        results = PreflightRunner(
            default_profile().with_overrides(runs_root="/proc/x/y")).run()
        blocking = blocking_failures(results)
        self.assertTrue(blocking)
        self.assertEqual(blocking[0].check_id, "PF-RUNS-ROOT")

    def test_llm_availability_is_never_claimed_from_construction(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = {v.check_id: v for v in PreflightRunner(
                default_profile().with_overrides(runs_root=tmp),
                llm_backend_names=("claude-sonnet",)).run()}
            self.assertEqual(results["PF-LLM"].status, st.UNKNOWN)
            self.assertIn("GAP-11", results["PF-LLM"].reason)

    def test_preflight_performs_no_write_beyond_the_runs_root(self):
        """The only side effect preflight is allowed is ``mkdir`` on its root."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "runs"
            PreflightRunner(
                default_profile().with_overrides(runs_root=str(root))).run()
            self.assertTrue(root.is_dir())
            self.assertEqual(list(root.iterdir()), [])


# --------------------------------------------------------------------------- #
# Session lifecycle
# --------------------------------------------------------------------------- #


class SessionLifecycle(unittest.TestCase):

    def test_start_records_a_run_and_publishes_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            bus, controller = make_controller(tmp)
            controller.preflight()
            run_id = controller.start(mode="REPLAY",
                                      mode_evidence={"reason": "fixture"})
            self.assertTrue(run_id)
            self.assertEqual(controller.disposition, "RUNNING")
            state = controller.state()
            self.assertEqual(state.mode, "REPLAY")
            self.assertFalse(state.is_live)
            self.assertTrue(state.recording)
            kinds = [event.kind for event in controller.timeline]
            self.assertIn("SESSION_STARTED", kinds)

    def test_start_refuses_a_mode_the_store_does_not_define(self):
        with tempfile.TemporaryDirectory() as tmp:
            _bus, controller = make_controller(tmp)
            controller.preflight()
            with self.assertRaises(SessionError):
                controller.start(mode="LIVE_ISH", mode_evidence={})

    def test_start_refuses_without_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            _bus, controller = make_controller(tmp)
            with self.assertRaises(SessionError):
                controller.start(mode="REPLAY", mode_evidence={})

    def test_start_refuses_on_a_blocking_preflight(self):
        bus = StateBus()
        controller = SessionController(
            bus, profile=default_profile().with_overrides(
                runs_root="/proc/x/y"), store_factory=store_factory())
        controller.preflight()
        with self.assertRaises(SessionError) as caught:
            controller.start(mode="REPLAY", mode_evidence={})
        self.assertIn("PF-RUNS-ROOT", str(caught.exception))

    def test_an_aborted_run_is_never_a_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            _bus, controller = make_controller(tmp)
            controller.preflight()
            controller.start(mode="REPLAY", mode_evidence={})
            manifest = controller.abort()
            self.assertEqual(manifest["disposition"], "ABORTED")
            self.assertNotIn("ABORTED", store_module.SUCCESS_DISPOSITIONS)
            self.assertEqual(controller.disposition, "ABORTED")
            self.assertTrue(manifest["events"] >= 1,
                            "artifacts captured before the abort are preserved")

    def test_the_abort_confirmation_states_targets_and_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            _bus, controller = make_controller(tmp)
            controller.preflight()
            run_id = controller.start(mode="REPLAY", mode_evidence={})
            spec = controller.abort_confirmation()
            self.assertEqual(spec.acknowledgement, confirm_mod.TYPED_CONFIRM)
            self.assertTrue(any(run_id in target for target in spec.targets))
            self.assertTrue(any("ABORTED" in effect for effect in spec.effects))
            self.assertTrue(any("never reported as a success" in effect
                                for effect in spec.effects))

    def test_the_profile_cannot_change_mid_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            _bus, controller = make_controller(tmp)
            controller.preflight()
            controller.start(mode="REPLAY", mode_evidence={})
            with self.assertRaises(SessionError):
                controller.set_profile(default_profile("other"))

    def test_a_failing_store_write_is_reported_not_swallowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            bus, controller = make_controller(tmp, fail_on_event=True)
            warnings = []
            bus.subscribe("warning", warnings.append)
            controller.preflight()
            controller.start(mode="REPLAY", mode_evidence={})
            bus.drain(budget=4096)
            self.assertTrue(any(getattr(w, "kind", "") == "RECORDING_FAILED"
                                for w in warnings))

    def test_a_store_that_cannot_identify_itself_disables_recording_loudly(self):
        """A store with no run id is not a store that can be said to record.

        Written when the store body was still another track's stub; kept, and
        narrowed to the property, now that the real store has landed.  The
        console must still run - it simply must not claim to be recording.
        """
        class Unidentified:
            mode = "REPLAY"

            @property
            def run_id(self):
                raise RuntimeError("this store never opened a run directory")

            def append_event(self, record):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            bus = StateBus()
            warnings = []
            bus.subscribe("warning", warnings.append)
            controller = SessionController(
                bus, profile=default_profile().with_overrides(runs_root=tmp),
                store_factory=lambda *a, **k: Unidentified())
            controller.preflight()
            controller.start(mode="REPLAY", mode_evidence={})
            controller.record_event(lane="SESSION", kind="PROBE")
            bus.drain(budget=4096)
            self.assertFalse(controller.state().recording)
            self.assertTrue(any(getattr(w, "kind", "") == "RECORDING_UNAVAILABLE"
                                for w in warnings))

    def test_a_store_whose_append_is_unimplemented_disables_recording_loudly(self):
        """A signature-only store degrades; it does not take the window down."""
        class Unimplemented:
            run_id = "run-stub"
            mode = "REPLAY"

            def append_event(self, record):
                raise NotImplementedError("no store body")

        with tempfile.TemporaryDirectory() as tmp:
            bus = StateBus()
            controller = SessionController(
                bus, profile=default_profile().with_overrides(runs_root=tmp),
                store_factory=lambda *a, **k: Unimplemented())
            controller.preflight()
            controller.start(mode="REPLAY", mode_evidence={})
            controller.record_event(lane="SESSION", kind="PROBE")
            self.assertFalse(controller.state().recording)

    def test_the_real_store_records_and_reports_a_run_id(self):
        """The seam, exercised end to end now that both sides have landed.

        The default factory is ``SessionStore.create``; this is the assertion
        that the controller's call shape and the store's constructor agree,
        which no single-track suite could make.
        """
        with tempfile.TemporaryDirectory() as tmp:
            bus = StateBus()
            controller = SessionController(
                bus, profile=default_profile().with_overrides(runs_root=tmp))
            controller.preflight()
            run_id = controller.start(
                mode="REPLAY",
                mode_evidence={"basis": "REPLAY_OF_RECORDED_SOURCE"})
            controller.record_event(lane="SESSION", kind="PROBE")
            self.assertTrue(run_id)
            self.assertTrue(controller.state().recording)
            store = store_module.SessionStore.open(Path(tmp) / run_id)
            kinds = [event.get("kind") for event in store.read_events()]
            self.assertIn("PROBE", kinds)
            self.assertIn("SESSION_STARTED", kinds)

    def test_events_carry_run_identity_and_a_monotonic_sequence(self):
        with tempfile.TemporaryDirectory() as tmp:
            _bus, controller = make_controller(tmp)
            controller.preflight()
            run_id = controller.start(mode="REPLAY", mode_evidence={})
            controller.record_event(lane="A1", kind="POLICY_ERROR",
                                    severity="ERROR", title="mismatch",
                                    policy_id="p-1")
            events = controller.timeline
            self.assertEqual([e.seq for e in events],
                             sorted(e.seq for e in events))
            last = events[-1]
            self.assertEqual(last.run_id, run_id)
            self.assertEqual(last.policy_id, "p-1")
            self.assertEqual(controller.state().last_warning.kind,
                             "POLICY_ERROR")

    def test_worker_failures_become_events_not_crashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            _bus, controller = make_controller(tmp)
            thread = controller.run_in_worker(
                "boom", lambda: (_ for _ in ()).throw(RuntimeError("nope")))
            thread.join(timeout=5)
            kinds = [event.kind for event in controller.timeline]
            self.assertIn("WORKER_FAILED", kinds)


# --------------------------------------------------------------------------- #
# Responsiveness (gate G-RESPONSIVENESS)
# --------------------------------------------------------------------------- #


class DrainLoopResponsiveness(unittest.TestCase):

    def test_the_tick_keeps_dispatching_while_a_worker_is_busy(self):
        """A long worker call must not stall the drain loop.

        The worker below sleeps for far longer than a frame, which is the
        simulated LLM call and the simulated large export in one: what matters
        is that it is not on the drain thread.
        """
        bus = StateBus()
        window = ConsoleWindow(bus, drain_interval_ms=20)
        seen = []
        bus.subscribe("metric", seen.append)
        stop = threading.Event()

        def slow_worker():
            index = 0
            while not stop.is_set():
                time.sleep(0.005)
                bus.publish("metric", index)
                index += 1

        worker = threading.Thread(target=slow_worker, daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                window.tick()
                time.sleep(0.02)
        finally:
            stop.set()
            worker.join(timeout=2)
        stats = window.monitor.stats()
        self.assertGreater(stats.ticks, 20)
        self.assertGreater(len(seen), 0)
        self.assertLess(stats.max_interval_ms, 20 * 4,
                        "a tick gap exceeded four times the interval")

    def test_a_raising_tick_hook_cannot_stop_the_heartbeat(self):
        bus = StateBus()
        window = ConsoleWindow(bus)
        window.add_tick_hook(lambda: (_ for _ in ()).throw(RuntimeError("x")))
        for _ in range(5):
            window.tick()
        self.assertEqual(window.monitor.stats().ticks, 5)

    def test_tick_monitor_is_bounded(self):
        monitor = TickMonitor(capacity=32)
        now = 0.0
        for _ in range(5000):
            monitor.record(start=now, end=now + 0.001, dispatched=1, depth=3)
            now += 0.01
        stats = monitor.stats()
        self.assertEqual(stats.ticks, 5000)
        self.assertEqual(stats.queue_high_water, 3)
        self.assertLessEqual(len(monitor._intervals), 32)

    def test_f11_presents_the_demo_view_and_returns_where_it_was(self):
        """The presentation key, wired at integration.

        Both operator documents describe F11 as what a speaker presses when the
        projector is live.  A full-screen Settings pane is not that, and losing
        the operator's place on the way back is exactly the "returning to the
        detail screen costs you the context" failure section 5 forbids.
        """
        class Pane:
            def __init__(self, workspace_id):
                self.id = workspace_id
                self.title = workspace_id

            def build(self, parent):
                pass

            def on_activate(self):
                pass

            def on_deactivate(self):
                pass

        window = ConsoleWindow(StateBus())
        for name in ("live_ops", "analysis", "demo"):
            window.add_workspace(Pane(name))
        window.select("analysis")

        self.assertTrue(window.toggle_fullscreen())
        self.assertEqual(window.active_workspace, "demo")
        self.assertFalse(window.toggle_fullscreen())
        self.assertEqual(window.active_workspace, "analysis")

    def test_f11_without_a_demo_view_still_toggles_rather_than_failing(self):
        class Pane:
            def __init__(self, workspace_id):
                self.id = workspace_id
                self.title = workspace_id

            def build(self, parent):
                pass

            def on_activate(self):
                pass

            def on_deactivate(self):
                pass

        window = ConsoleWindow(StateBus())
        window.add_workspace(Pane("live_ops"))
        window.select("live_ops")
        self.assertTrue(window.toggle_fullscreen())
        self.assertEqual(window.active_workspace, "live_ops")

    def test_no_workspace_repaint_reaches_a_source_store_or_exporter(self):
        """AST proof for gate G-RESPONSIVENESS.

        A workspace's ``on_state`` runs on the Tk thread.  If it could call into
        a source, the store or the export pipeline, the window would block on
        I/O.  The rule is that it reads the view models and nothing else.
        """
        banned = ("SessionStore", "read_telemetry", "read_events",
                  "read_episodes", "append_telemetry", "copy_raw",
                  "write_figure", "export")
        offenders = []
        for path in sorted((REPO_ROOT / "gui" / "operator" / "workspaces")
                           .glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"),
                             filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef):
                    continue
                if node.name not in ("on_state", "repaint", "update"):
                    continue
                for call in ast.walk(node):
                    if not isinstance(call, ast.Call):
                        continue
                    name = getattr(call.func, "attr",
                                   getattr(call.func, "id", ""))
                    if name in banned:
                        offenders.append(f"{path.name}:{call.lineno}:{name}")
        self.assertEqual(offenders, [], "Tk-thread repaint reaches I/O")


# --------------------------------------------------------------------------- #
# Entry points and headless preservation (gate G-HEADLESS)
# --------------------------------------------------------------------------- #


class ConsoleSelection(unittest.TestCase):
    """The entry points after the B-01 cutover, and the legacy quarantine.

    There is no console *selection* any more.  ``main.py`` starts one thing -
    the Cockpit over the Assurance Kernel - and the preserved Coordinator
    console is a different program behind the same operator gate it always
    had.  These tests pin both halves: that the default entry has no legacy
    option left to take, and that the legacy entry still refuses without the
    approval and still runs with it.
    """

    def setUp(self):
        import main

        from tools.legacy import coordinator_console

        self.main = main
        self.legacy = coordinator_console

    def _default_namespace(self):
        """What ``python3 main.py`` with no argument actually parses."""
        import argparse
        from unittest import mock

        captured = {}
        real_parse = argparse.ArgumentParser.parse_args

        def capture(self, *args, **kwargs):
            namespace = real_parse(self, *args, **kwargs)
            captured.update(vars(namespace))
            return namespace

        with mock.patch.object(argparse.ArgumentParser, "parse_args", capture), \
                mock.patch.object(self.main, "run_operator_console") as gui, \
                mock.patch.object(os.sys, "argv", ["main"]):
            self.main.main()
        gui.assert_called_once()
        return captured

    def test_the_default_entry_offers_no_legacy_option(self):
        """The flags that reached the old runtime are gone, not defaulted."""
        captured = self._default_namespace()
        # ``no_gui`` and ``cmd`` are deliberately *not* in this list any more.
        # They were the legacy console's names, and they are now the Kernel
        # path's own headless flags - ``--no-gui`` runs one intent through the
        # same KernelSubmissionSession over the hardware-free adapter
        # (``--hardware-free``) or the deployment (``--live``).  The next test
        # is what keeps that reuse honest: neither reaches a runtime on its own.
        for gone in ("console", "experiment", "trials", "calibration_mode"):
            self.assertNotIn(gone, captured)

    def test_the_reused_headless_flags_reach_no_runtime_on_their_own(self):
        """``--no-gui`` names which runtime, or it is refused.

        The old console was reached by ``--no-gui`` alone.  Here that argument
        selects nothing: without ``--hardware-free`` or ``--live`` the entry
        point exits rather than falling back to any runtime.
        """
        import subprocess
        import sys as _sys

        result = subprocess.run(
            [_sys.executable, "main.py", "--no-gui", "--cmd", "pin the UE"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=300)
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn("--hardware-free", result.stderr)
        self.assertIn("--live", result.stderr)

    def test_the_default_entry_constructs_no_coordinator(self):
        """No name to construct it with, and no import that could supply one.

        Read off the syntax tree rather than the file text: the module
        docstring *names* the runtime it refuses to build, and a substring
        search would count that as the offence.
        """
        import ast

        self.assertFalse(hasattr(self.main, "IntentCoordinator"))
        tree = ast.parse((REPO_ROOT / "main.py").read_text(encoding="utf-8"))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        for name in imported:
            with self.subTest(module=name):
                self.assertFalse(
                    name == "coordinator" or name.startswith("coordinator.")
                    or name == "executor" or name.startswith("executor.")
                    or name.startswith("gui.dashboard")
                    or name.startswith("tools.legacy"), name)

    def test_the_legacy_console_refuses_without_the_operator_approval(self):
        import contextlib
        import io
        from unittest import mock

        buffer = io.StringIO()
        with mock.patch.dict(os.environ, {self.legacy.LEGACY_CONSOLE_ENV: ""},
                             clear=False), \
                mock.patch.object(self.legacy, "IntentCoordinator") as ctor, \
                mock.patch.object(self.legacy, "run_with_gui") as legacy, \
                mock.patch.object(os.sys, "argv", ["console"]), \
                contextlib.redirect_stdout(buffer):
            code = self.legacy.main()
        self.assertEqual(code, 2)
        legacy.assert_not_called()
        ctor.assert_not_called()
        self.assertIn(self.legacy.LEGACY_CONSOLE_ENV, buffer.getvalue())

    def test_the_legacy_console_starts_when_explicitly_approved(self):
        from unittest import mock

        with mock.patch.dict(os.environ,
                             {self.legacy.LEGACY_CONSOLE_ENV: "1"},
                             clear=False), \
                mock.patch.object(self.legacy, "IntentCoordinator"), \
                mock.patch.object(self.legacy, "run_with_gui") as legacy, \
                mock.patch.object(os.sys, "argv", ["console"]):
            self.legacy.main()
        legacy.assert_called_once()

    def test_the_legacy_headless_paths_still_work(self):
        from unittest import mock

        for argv, target in ((["console", "--no-gui"], "run_cli"),
                             (["console", "--cmd", "hold 10 Mbps"],
                              "run_single_command")):
            with mock.patch.dict(os.environ,
                                 {self.legacy.LEGACY_CONSOLE_ENV: "1"},
                                 clear=False), \
                    mock.patch.object(self.legacy, "IntentCoordinator"), \
                    mock.patch.object(self.legacy, target) as entry, \
                    mock.patch.object(self.legacy,
                                      "run_with_operator_gui") as gui, \
                    mock.patch.object(os.sys, "argv", argv):
                self.legacy.main()
            entry.assert_called_once()
            gui.assert_not_called()


class HeadlessPurity(unittest.TestCase):
    """The console must not leak a toolkit into the headless paths."""

    def _run(self, code):
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-c", code], cwd=str(REPO_ROOT),
            capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 0,
                         f"{result.stdout}\n{result.stderr}")
        return result.stdout.strip()

    def test_the_headless_entries_import_no_toolkit(self):
        """A fresh interpreter, so no other test can pollute the answer.

        The headless entries moved to ``tools.legacy.coordinator_console`` with
        the runtime they drive; the property they had to keep - no toolkit
        anywhere on a ``--no-gui`` or ``--cmd`` run - is asserted on them
        there, unchanged.
        """
        for argv in ('["console", "--no-gui"]', '["console", "--cmd", "x"]'):
            output = self._run(
                "import os, sys; sys.argv = %s\n"
                "os.environ['AIC_LEGACY_CONSOLE_APPROVED'] = '1'\n"
                "from unittest import mock\n"
                "from tools.legacy import coordinator_console as c\n"
                "with mock.patch.object(c, 'IntentCoordinator'), \\\n"
                "     mock.patch.object(c, 'run_cli'), \\\n"
                "     mock.patch.object(c, 'run_single_command'):\n"
                "    c.main()\n"
                "print('tkinter' in sys.modules, 'matplotlib' in sys.modules)"
                % argv)
            self.assertEqual(output, "False False", f"argv={argv}")

    def test_the_default_entry_imports_no_toolkit_until_the_window_opens(self):
        """``build_console`` is the whole composition and needs no display."""
        output = self._run(
            "import sys\n"
            "import main\n"
            "main.build_console()\n"
            "print('tkinter' in sys.modules, 'matplotlib' in sys.modules)")
        self.assertEqual(output, "False False")

    def test_no_console_module_imports_the_toolkit_at_module_scope(self):
        """The toolkit is imported inside ``build``, never at import time.

        This is what lets every projection in the console be asserted in a
        hermetic test, and it is what keeps ``gui.operator`` importable from the
        export pipeline and from a machine with no display.
        """
        offenders = []
        for path in sorted((REPO_ROOT / "gui" / "operator").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"),
                             filename=str(path))
            for node in tree.body:
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    names = [node.module or ""]
                for name in names:
                    if name.split(".")[0] in ("tkinter", "matplotlib"):
                        offenders.append(f"{path.name}:{node.lineno}:{name}")
        self.assertEqual(offenders, [])

    def test_the_console_assembles_without_a_display(self):
        from gui.operator.app import OperatorConsole

        with tempfile.TemporaryDirectory() as tmp:
            console = OperatorConsole(runs_root=tmp)
            # ``main`` leads: it is the one-screen view the console opens on,
            # and the shell activates the first workspace it is given.
            self.assertEqual([w.id for w in console.workspaces],
                             ["main", "live_ops", "contract_studio",
                              "trial_safety", "evidence_ledger",
                              "batch_experiments", "objective_registry",
                              "intent_decision", "analysis", "demo",
                              "settings"])
            self.assertIsNone(console.window.root)
            self.assertFalse(console.state.is_live)

    def test_a_missing_workspace_module_becomes_a_stated_placeholder(self):
        from gui.operator.workspaces import PlaceholderWorkspace, load_workspace

        workspace = load_workspace("x", "gui.operator.workspaces.not_here",
                                   "Nope", "Missing")
        self.assertIsInstance(workspace, PlaceholderWorkspace)
        self.assertIn("not present in this build", workspace.reason)

    def test_an_action_requiring_confirmation_fails_closed_without_a_dialog(self):
        """No dialog available must mean refused, never silently approved.

        The submission guards now run first - a submission into a session that
        is not recording is refused before anything is confirmed - so the
        confirmation gate is exercised where it actually applies: inside a
        running session.
        """
        from gui.operator.app import OperatorConsole

        with tempfile.TemporaryDirectory() as tmp:
            console = OperatorConsole(runs_root=tmp)
            console.intent_submitter = lambda _text: self.fail(
                "submitted without confirmation")
            console.controller.start(
                mode="REPLAY",
                mode_evidence={"basis": "REPLAY_OF_RECORDED_SOURCE"},
                require_preflight=False)
            console.handle_action("submit", "hold 10 Mbps")
            kinds = [event.kind for event in console.controller.timeline]
            self.assertIn("ACTION_CANCELLED", kinds)
            console.controller.stop("COMPLETED")

    def test_a_submission_without_a_running_session_is_refused_with_a_reason(self):
        """The guard the confirmation gate now sits behind."""
        from gui.operator.app import OperatorConsole

        with tempfile.TemporaryDirectory() as tmp:
            console = OperatorConsole(runs_root=tmp)
            console.intent_submitter = lambda _text: self.fail(
                "submitted with no session recording")
            console.handle_action("submit", "hold 10 Mbps")
            last = console.controller.timeline[-1]
            self.assertEqual(last.kind, "ACTION_REFUSED")
            self.assertIn("no session is recording", last.title)

    def test_an_unknown_action_is_refused_with_a_stated_reason(self):
        from gui.operator.app import OperatorConsole

        with tempfile.TemporaryDirectory() as tmp:
            console = OperatorConsole(runs_root=tmp)
            console.handle_action("launch_the_gnb", "")
            last = console.controller.timeline[-1]
            self.assertEqual(last.kind, "ACTION_REFUSED")
            self.assertIn("launch_the_gnb", last.title)


# --------------------------------------------------------------------------- #
# The soak harness
# --------------------------------------------------------------------------- #


def _rss_bytes():
    """Current resident set size of this process, or ``None``.

    Read from this process's own ``/proc`` entry.  It is a self-measurement for
    the performance harness; it is never used as, or presented as, the status of
    an O-RAN component.
    """
    try:
        with open("/proc/self/statm", "r", encoding="ascii") as handle:
            pages = int(handle.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return None


def run_soak(duration_s=5.0, *, rate_hz=200.0, interval_ms=100,
             max_queue=4096, progress=None):
    """Drive the real drain loop against a synthetic Replay-rate publisher.

    Returns the numbers gate ``G-RESPONSIVENESS`` reads: tick interval and drain
    cost, bus depth high-water and shed count, and RSS growth.  The publisher
    runs on a worker and the drain runs here, exactly as the console arranges
    them, so the harness measures the production path rather than a model of it.
    """
    bus = StateBus(max_queue=max_queue)
    window = ConsoleWindow(bus, drain_interval_ms=interval_ms)
    received = {"count": 0}

    def count(_payload):
        received["count"] += 1

    for channel in ("metric", "timeline", "session"):
        bus.subscribe(channel, count)

    stop = threading.Event()

    def publisher():
        period = 1.0 / max(1.0, rate_hz)
        index = 0
        components = tuple(
            ComponentStatusView(element_id=f"e{i}", label=f"element {i}",
                                kind="O_DU", status=st.UNKNOWN)
            for i in range(12))
        while not stop.is_set():
            bus.publish_many((
                ("metric", {"metric": "RRU.PrbDl", "value": index % 100}),
                ("timeline", TimelineEventView(seq=index, lane="O1",
                                               kind="EVIDENCE_RECEIVED")),
                ("session", SessionState(run_id="soak", mode="REPLAY",
                                         components=components)),
            ))
            index += 1
            time.sleep(period)

    rss_start = _rss_bytes()
    worker = threading.Thread(target=publisher, daemon=True)
    worker.start()
    started = time.monotonic()
    try:
        next_tick = started
        while time.monotonic() - started < duration_s:
            window.tick()
            next_tick += interval_ms / 1000.0
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.monotonic()
            if progress is not None and window.monitor.ticks % 600 == 0:
                progress(window.monitor.stats())
    finally:
        stop.set()
        worker.join(timeout=5)
    rss_end = _rss_bytes()
    stats = window.monitor.stats().as_dict()
    stats.update({
        "durationS": round(time.monotonic() - started, 2),
        "published": bus.stats()["published"],
        "delivered": received["count"],
        "dropped": bus.dropped(),
        "busHighWater": bus.stats()["highWater"],
        "rssStartBytes": rss_start, "rssEndBytes": rss_end,
        "rssGrowthBytes": (None if rss_start is None or rss_end is None
                           else rss_end - rss_start),
        "intervalBudgetMs": interval_ms,
    })
    return stats


class SoakHarness(unittest.TestCase):
    """The harness itself is tested short, so a real soak cannot rot."""

    def test_a_short_soak_reports_the_required_numbers(self):
        stats = run_soak(2.0, rate_hz=120.0, interval_ms=50)
        for key in ("ticks", "maxIntervalMs", "meanIntervalMs", "maxDrainMs",
                    "queueHighWater", "published", "delivered", "dropped",
                    "rssGrowthBytes", "busHighWater"):
            self.assertIn(key, stats)
        self.assertGreater(stats["ticks"], 10)
        self.assertGreater(stats["delivered"], 0)
        self.assertLess(stats["maxIntervalMs"], 50 * 6,
                        "the drain loop fell far behind its interval")

    def test_the_bus_is_bounded_under_a_faster_publisher(self):
        stats = run_soak(1.5, rate_hz=2000.0, interval_ms=100, max_queue=256)
        self.assertLessEqual(stats["busHighWater"], 256)
        self.assertGreaterEqual(stats["dropped"], 0)


def widget_smoke(out_dir, *, capability_manifest=None):
    """Build the real window, exercise the tabs, and save screenshots.

    Kept out of the unit suite on purpose - the suite is hermetic and must not
    open a window on an operator's desktop - but kept *in this file* so the
    screenshot evidence is reproducible with one command rather than an ad hoc
    script that rots.  Run it under a virtual display::

        xvfb-run -a -s "-screen 0 1920x1080x24" \\
            python3 -m tests.gui.test_shell_and_bus --widget-smoke --out /tmp/shots
    """
    from gui.operator.app import OperatorConsole
    from gui.operator.workspaces import live_ops
    from oran.rapp import status_projection as sp

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    manifest = dict(capability_manifest or {})
    console = OperatorConsole(runs_root=str(out / "runs"),
                              capability_manifest=manifest)
    root = console.create_window()
    written = []

    def shot(name):
        root.update_idletasks()
        root.update()
        path = out / f"operator-console-{name}.png"
        try:
            from PIL import ImageGrab

            ImageGrab.grab(xdisplay=os.environ.get("DISPLAY")).save(path)
            written.append(str(path))
        except Exception as exc:                          # pragma: no cover
            print(f"screenshot {name} skipped: {exc}")

    capability = sp.project_capability(manifest)
    console.controller.preflight(capability_manifest=manifest or None,
                                 llm_backend_names=("claude-sonnet",))
    console.controller.set_components(live_ops.build_topology(capability),
                                      live_ops.build_readiness(None))
    console.controller.record_event(lane="SESSION", kind="CONSOLE_READY",
                                    title="operator console assembled")
    console.controller.record_event(
        lane="A1", kind="POLICY_ERROR", severity="ERROR",
        title="readback mismatch", policy_id="p-1", intent_id="i-1",
        component="nonrt-ric")
    for _ in range(8):
        console.window.tick()
    for workspace in console.workspaces:
        assert console.window.select(workspace.id), workspace.id
        shot(workspace.id)
    stats = console.window.monitor.stats().as_dict()
    root.destroy()
    return {"screenshots": written, "tick": stats,
            "workspaces": [w.title for w in console.workspaces]}


def _main(argv):
    import argparse

    parser = argparse.ArgumentParser(
        description="Operator Console performance soak (gate G-RESPONSIVENESS) "
                    "and widget smoke")
    parser.add_argument("--soak", type=float, default=1800.0,
                        help="soak duration in seconds (default 1800 = 30 min)")
    parser.add_argument("--rate-hz", type=float, default=200.0)
    parser.add_argument("--interval-ms", type=int, default=100)
    parser.add_argument("--json", type=str, default=None,
                        help="write the result to this path as JSON")
    parser.add_argument("--widget-smoke", action="store_true",
                        help="build the real window and save screenshots "
                             "instead of running the soak")
    parser.add_argument("--out", type=str, default="gui_smoke",
                        help="directory for --widget-smoke screenshots")
    parser.add_argument("--capability", type=str, default=None,
                        help="capability manifest JSON for --widget-smoke")
    args = parser.parse_args(argv)

    if args.widget_smoke:
        manifest = (json.loads(Path(args.capability).read_text(encoding="utf-8"))
                    if args.capability else {})
        result = widget_smoke(args.out, capability_manifest=manifest)
        print(json.dumps(result, indent=2))
        if args.json:
            Path(args.json).write_text(json.dumps(result, indent=2) + "\n",
                                       encoding="utf-8")
        return 0

    def progress(stats):
        print(f"  t={stats.ticks:>7}  maxInterval={stats.max_interval_ms:8.2f}ms"
              f"  maxDrain={stats.max_drain_ms:7.2f}ms"
              f"  qHigh={stats.queue_high_water}", flush=True)

    print(f"soak: {args.soak:.0f}s at {args.rate_hz:.0f} Hz, "
          f"{args.interval_ms} ms drain interval", flush=True)
    stats = run_soak(args.soak, rate_hz=args.rate_hz,
                     interval_ms=args.interval_ms, progress=progress)
    print(json.dumps(stats, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps(stats, indent=2) + "\n",
                                   encoding="utf-8")
    return 0


if __name__ == "__main__":
    import sys

    if "--soak" in sys.argv or "--widget-smoke" in sys.argv:
        raise SystemExit(_main(sys.argv[1:]))
    unittest.main()
