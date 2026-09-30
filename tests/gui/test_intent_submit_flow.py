from copy import deepcopy
import threading
import unittest

from gui.operator.sources.coordinator_hooks import install_coordinator_hook
from gui.operator.sources.live import (
    IntentSubmission, IntentWorker, build_submit_confirmation,
    normalized_intent_preview, submit_intent, withdraw_intent,
)
from gui.operator.sources.llm_registry import LlmRegistry
from gui.operator.viewmodel.types import IntentRowView
from gui.operator.widgets.intent_form import IntentForm
from tests.test_safety_transaction import _make_coordinator, _trial_ok


class IntentSubmitFlowTest(unittest.TestCase):
    def test_normalized_preview_uses_strict_coordinator_schema(self):
        """The preview validates with the *episode runtime's own* validator.

        Injected rather than imported since the B-01 cutover: importing it here
        is what put ``coordinator.schema`` - and the pre-Kernel decision
        runtime behind it - inside the console package.  The strictness is
        unchanged, and the third case is new: with no validator supplied the
        preview is unknown rather than passed through unchecked.
        """
        from tools.legacy.episode_support import legacy_episode_support

        validate = legacy_episode_support().validate_intent_parse
        parsed = {
            "type": "throughput_goal", "constraint": "min", "value": 8.0,
            "unit": "Mbps", "scope": {"ue_ids": ["ue1"], "bs_ids": []},
            "description": "keep throughput",
        }
        self.assertEqual(normalized_intent_preview(parsed, validate=validate),
                         parsed)
        self.assertIsNone(normalized_intent_preview({**parsed, "value": "8"},
                                                    validate=validate))
        self.assertIsNone(normalized_intent_preview(parsed))

    def test_recording_confirmation_names_scope_policy_run_and_effect(self):
        submission = IntentSubmission(
            "values.json", {"intentText": "intent"}, target_scope="UE-1",
            objective="throughput >= 8 Mbps", constraints=("cell=c1",),
            policy_revision="3")
        confirmation = build_submit_confirmation(submission, run_id="run-7")
        combined = " ".join(confirmation.targets + confirmation.effects)
        self.assertIn("UE-1", combined)
        self.assertIn("run-7", combined)
        self.assertIn("revision 3", combined)
        self.assertIn("via R1", combined)

    def test_authoritative_result_is_presentation_independent(self):
        expected = {
            "episode_id": "ep-1", "success": True,
            "terminal_outcome": "commit_original",
            "evidence": [{"quality": "OK"}],
        }
        calls = []

        def runner(*, integration_path, request, present, **_kwargs):
            self.assertEqual(integration_path, "values.json")
            self.assertEqual(request["intentText"], "keep UE throughput above 8 Mbps")
            value = deepcopy(expected)
            present(value)
            calls.append(value)
            return deepcopy(expected)

        def broken_present(value):
            value["terminal_outcome"] = "technical_failsafe"
            value["evidence"].clear()
            raise RuntimeError("widget failed")

        result = submit_intent(
            IntentSubmission(
                integration_path="values.json",
                request={"intentText": "keep UE throughput above 8 Mbps"}),
            present=broken_present, runner=runner)
        self.assertEqual(result.authoritative, expected)
        self.assertEqual(result.decision.terminal_outcome, "commit_original")
        self.assertEqual(result.decision.eq12_state, "Admitted")
        self.assertEqual(calls, [expected])

    def test_coordinator_hook_preserves_existing_listener_and_isolates_view_only(self):
        seen = []

        class Coordinator:
            on_state_change = staticmethod(lambda source, target: seen.append((source, target)))

        coordinator = Coordinator()
        transitions = []
        install_coordinator_hook(coordinator, transitions.append)
        install_coordinator_hook(coordinator, lambda _event: (_ for _ in ()).throw(RuntimeError("bad view")))
        coordinator.on_state_change("S0", "S1")
        self.assertEqual(seen, [("S0", "S1")])
        self.assertEqual((transitions[0].source, transitions[0].target), ("S0", "S1"))
        self.assertTrue(transitions[0].observed_at.endswith("Z"))
        self.assertGreater(transitions[0].monotonic_s, 0)

    def test_coordinator_hook_propagates_previous_safety_listener_failure(self):
        class Coordinator:
            on_state_change = staticmethod(
                lambda _source, _target: (_ for _ in ()).throw(
                    RuntimeError("safety listener failed")))

        coordinator = Coordinator()
        install_coordinator_hook(coordinator, lambda _event: None)
        with self.assertRaisesRegex(RuntimeError, "safety listener failed"):
            coordinator.on_state_change("S3", "S4")

    def test_s4_safety_listener_still_rolls_back_with_gui_hook_attached(self):
        states = []

        def safety_listener(_old, new):
            states.append(new)
            if new == "S4":
                raise RuntimeError("S4 state callback boom")

        coordinator, _ = _make_coordinator()
        coordinator.on_state_change = safety_listener
        view_transitions = []
        install_coordinator_hook(coordinator, view_transitions.append)
        rollbacks = []
        coordinator._execute_trial = _trial_ok
        coordinator._validate_trial = lambda new, active: {
            "all_satisfied": True,
            "metrics": {},
            "monitor_verdicts": {
                getattr(intent, "id", None): "satisfied"
                for intent in list(active) + [new]
            },
        }
        coordinator._rollback = lambda _snapshot: rollbacks.append(1) or None

        result = coordinator.process_intent("throughput >= 8 Mbps")

        self.assertEqual(rollbacks, [1])
        self.assertEqual(result["terminal_outcome"], "technical_failsafe")
        self.assertFalse(result["success"])

    def test_unknown_preview_does_not_gate_natural_language_submit(self):
        class FakeButton:
            state = None

            def configure(self, **kwargs):
                self.state = kwargs.get("state", self.state)

        class FakeText:
            def configure(self, **_kwargs):
                pass

            def delete(self, *_args):
                pass

            def insert(self, *_args):
                pass

        submitted = []
        form = object.__new__(IntentForm)
        form._normalized = {"stale": "preview"}
        form._submit_button = FakeButton()
        form._preview_text = FakeText()
        form._on_submit = submitted.append
        form.values = lambda: {
            "intentText": "keep UE throughput above 8 Mbps",
            "normalizedIntent": form._normalized,
        }

        form.set_normalized(None, reason="parser unavailable")
        form._submit()

        self.assertEqual(form._submit_button.state, "normal")
        self.assertEqual(submitted, [{
            "intentText": "keep UE throughput above 8 Mbps",
            "normalizedIntent": None,
        }])

    def test_withdrawal_keeps_row_when_lifecycle_fails(self):
        row = IntentRowView("i-1", "2", "intent", policy_id="p-1")
        ok, retained, reason = withdraw_intent(
            row, lambda _row: {"success": False, "reason": "R1 refused"})
        self.assertFalse(ok)
        self.assertIs(retained, row)
        self.assertEqual(retained.lifecycle, "ACTIVE")
        self.assertIn("R1 refused", reason)

    def test_cancelled_worker_suppresses_late_ui_result(self):
        entered = threading.Event()
        release = threading.Event()
        delivered = []

        def runner(**kwargs):
            entered.set()
            release.wait(1)
            return {"success": False, "terminal_outcome": "pending_not_admitted"}

        worker = IntentWorker()
        worker.start(
            IntentSubmission("values.json", {"intentText": "intent"}),
            on_result=delivered.append, runner=runner)
        self.assertTrue(entered.wait(1))
        worker.cancel()
        release.set()
        self.assertTrue(worker.wait(1))
        self.assertEqual(delivered, [])

    def test_llm_registry_uses_coordinator_audited_switch(self):
        class Backend:
            model = "model-v1"

            def is_available(self):
                return True

        class Manager:
            def get_available_names(self):
                return ["local:model-v1"]

            def active_backend_name(self):
                return "local:model-v1"

            def backend_by_name(self, _name):
                return Backend()

        class Coordinator:
            llm_manager = Manager()

            def __init__(self):
                self.selected = []

            def set_llm_backend(self, value):
                self.selected.append(value)
                return True

        coordinator = Coordinator()
        registry = LlmRegistry(coordinator)
        self.assertEqual(registry.views()[0].availability, "UNKNOWN")
        self.assertEqual(registry.views()[0].availability_reason, "CONSTRUCTION_ONLY")
        self.assertTrue(registry.select("local:model-v1"))
        self.assertEqual(coordinator.selected, ["local:model-v1"])


if __name__ == "__main__":
    unittest.main()
