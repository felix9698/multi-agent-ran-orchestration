"""Hermetic Cockpit sitting composition and record projection."""
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from gui.operator.app import OperatorConsole
from gui.operator.widgets.agent_sitting import episode_rows, answer_payload, intent_entry
from gui.operator.workspaces.intent_decision import IntentDecisionWorkspace

HAS_DISPLAY = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
SENTENCES = (
    "I1: UE ueId=131 needs at least 3.0 Mbps downlink, relaxable to 2.0",
    "I2: UE ueId=132 needs at least 1.5 Mbps downlink, relaxable to 1.0",
)


def payload(method="three-agent"):
    return {"sentences": SENTENCES, "method": method, "budgetTrials": 6,
            "roleModels": {slot: "mock:agent" for slot in
                           ("target", "control", "trajectory", "monolith")}}


class CockpitAgentTests(unittest.TestCase):
    def test_the_run_record_tab_reads_every_episode_record_fact(self):
        """The keys that change what a number means must be on the screen."""
        from gui.operator.widgets.agent_sitting import METHODS, episode_rows
        episode = {
            "schemaVersion": "agent-episode/1.3.0", "episodeId": "e1",
            "timing": {"timingMode": "cold-start", "prepMs": 320},
            "budget": {"trialsK": 16, "deadlineBMs": 600000, "horizonHMs": 900000, "binDeltaMs": 5000},
            "resourceCost": {"prepMs": 320, "reuseCount": 2, "priorPrepMs": 90, "reuseGroup": "g"},
            "condition": {"ablation": "trajectory-only", "methodsWithoutGrid": ["basic-monolith"]},
            "prepared": {"tHash": "a" * 64, "cHash": "b" * 64},
            "excluded": {"rule": "logging-failure", "reason": "no samples", "at": "t"},
            "boundaries": [{"at": "t1", "kind": "policy", "detail": "owner changed"}],
            "nonTrialEvents": [{"kind": "rejected-proposal"}, {"kind": "rollback"}],
            "trials": [{"trialIndex": 0, "counted": False, "controlId": "C0",
                        "proposedTargetId": "T0", "success": {}, "window": {"valid": True},
                        "beforeBoundary": "t1", "kpis": {}, "kernel": {"terminalState": "SETTLED_SUCCESS"}}],
            "T": {"t0": {"targetId": "T0", "requirements": {}}}, "C": {"candidates": []},
        }
        rows = episode_rows(episode)
        facts = {name: (value, detail) for name, value, detail in rows["record"]}
        self.assertEqual("cold-start", facts["Timing mode"][0])
        self.assertIn("charged to B", facts["Timing mode"][1])
        self.assertEqual("0 of 1", facts["Counted trials"][0])
        self.assertEqual("2", facts["Non-trial events"][0])
        self.assertEqual("1", facts["Boundaries"][0])
        self.assertEqual("injected", facts["Prepared T/C"][0])
        self.assertEqual("trajectory-only", facts["Ablation"][0])
        self.assertEqual("yes", facts["Excluded"][0])
        self.assertEqual("2", facts["Reuse"][0])
        self.assertEqual("agent-episode/1.3.0", facts["Schema"][0])
        # The trial row shows what the metrics may count and what a boundary
        # made unusable for a later claim.
        self.assertEqual(("not counted", "t1"), rows["trials"][0][5:7])
        self.assertIn("three-agent-coverage", METHODS)

    def test_axis_ladders_mapping_persistence_and_recorded_cardinality(self):
        from gui.operator.widgets.agent_sitting import axis_settings, cardinality_text
        exposure = axis_settings(['all'], {
            'dlPrbCap': '131:0,12; 132:0,6', 'pfWeight': '131:0.5,1.0',
            'dlMcsBounds': '12345678:0..28,0..16',
            'txAttenuationDb': '12345678:0.0,6.0',
            'slicePrbQuota': '1:0:1:100,0:1:60'}, '4096')
        request = OperatorConsole._sitting_request({**payload(), 'settings': {'axisExposure': exposure}})
        self.assertEqual(request.caps['132'], (0, 6))
        self.assertEqual(request.slice_quotas['1'], ('0:1:100', '0:1:60'))
        self.assertEqual(request.pf_weights['131'], (0.5, 1.0))
        self.assertEqual(len(request.axes), 6)
        self.assertNotIn('axisExposure', request.settings)
        with tempfile.TemporaryDirectory() as tmp:
            console = OperatorConsole(runs_root=tmp)
            try:
                console._workspace_role_models({'settings': {'axisExposure': exposure}})
                restored = console._load_role_models()['settings']['axisExposure']
                self.assertEqual(restored['mcs_bounds']['12345678'], ['0..28', '0..16'])
            finally:
                console.shutdown()
        text = cardinality_text({'C': {'constructionPolicy': {'catalogCardinality': 2048}},
            'catalogCardinality': 16, 'execution': {'preflight': {
                'catalogCeiling': 4096, 'exposedAxisKinds': ['servingCell', 'dlMcsBounds']}}})
        self.assertIn('Exposed combinations: 2048', text)
        self.assertIn('Frozen: 16', text)
        self.assertIn('Ceiling: 4096', text)
        self.assertIn('dlMcsBounds', text)
        with self.assertRaises(ValueError):
            axis_settings(['servingCell'], {'dlPrbCap': '131'}, 4096)

    def test_request_mapping_all_methods_and_structured_intents(self):
        for method in ("three-agent", "internal-monolith", "basic-monolith", "deterministic"):
            request = OperatorConsole._sitting_request(payload(method))
            self.assertEqual(request.method, method)
            self.assertEqual(request.sentences, SENTENCES)
            self.assertEqual(request.budget_trials, 6)
            self.assertEqual(request.role_models["target"],
                             None if method == "deterministic" else "mock:agent")
        request = OperatorConsole._sitting_request({"intents": [{"intentId": "custom"}]})
        self.assertEqual(request.intents, ({"intentId": "custom"},))

    @unittest.skipUnless(HAS_DISPLAY, "needs a Tk display")
    def test_count_fields_mapping_validation_and_restore(self):
        import tkinter as tk
        from gui.operator.widgets.agent_sitting import AgentSittingPanel
        from tools.liveconsole.agent import parse_agent_intents
        with tempfile.TemporaryDirectory() as tmp:
            root = tk.Tk()
            root.withdraw()
            path = Path(tmp) / "agent-intent-set.json"
            panel = AgentSittingPanel(root, sentence=lambda: "", intent_set_path=path)
            try:
                panel.intent_count.set(2)
                panel.create_rows()
                self.assertEqual(str(panel.run_button["state"]), "disabled")
                for index, variables in enumerate(panel.row_vars):
                    values = {"owner": f"owner-{index}", "ueId": str(131 + index),
                              "kpi": "dlGoodputMbps", "op": ">=", "value": "1.5",
                              "unit": "Mbps", "steps": "2" if index == 0 else "0",
                              "bound": "1.0" if index == 0 else "", "priority": str(index + 1),
                              "weight": "3.5" if index == 0 else "", "note": "operator note"}
                    for key, value in values.items():
                        variables[key].set(value)
                self.assertEqual(str(panel.run_button["state"]), "normal")
                self.assertEqual(str(panel.row_widgets[1]["bound"]["state"]), "disabled")
                request = OperatorConsole._sitting_request(panel.payload())
                self.assertEqual(request.sentences, ())
                self.assertEqual(request.intents[0]["owner"], "owner-0")
                self.assertEqual(request.intents[0]["sentence"], "operator note")
                self.assertEqual(request.intents[0]["weight"], 3.5)
                self.assertNotIn("weight", request.intents[1])
                self.assertNotIn("bound", request.intents[1]["requirement"])
                self.assertEqual(request.intents[0]["requirement"], {"reqId": "I1.r1", "kpi": "dlGoodputMbps", "scope": "ue@131", "op": ">=", "value": 1.5, "unit": "Mbps", "steps": 2, "bound": 1.0})
                rows = parse_agent_intents(request)
                self.assertEqual(len(rows), 2)
                from assurance.coordination.intake import IntakeChecklist
                self.assertEqual(IntakeChecklist().check([row.intent for row in rows], settings=request.settings), [])
                self.assertEqual(rows[1].intent.weight_or_default(2), 1.0)
                restored = AgentSittingPanel(root, sentence=lambda: "", intent_set_path=path)
                self.assertEqual(restored.payload()["intents"], panel.payload()["intents"])
                restored.frame.destroy()
                panel.row_vars[0]["owner"].set("")
                self.assertIn("I1: owner", panel.status_text.get())
                self.assertTrue(panel.row_widgets[0]["owner"].instate(["invalid"]))
                self.assertEqual(str(panel.run_button["state"]), "disabled")
                panel.set_record({"kind": "agent-sitting", "running": False})
                self.assertEqual(str(panel.run_button["state"]), "disabled")
                panel.row_vars[1]["kpi"].set("servingCell")
                self.assertEqual(panel.row_vars[1]["steps"].get(), "0")
                self.assertEqual(panel.row_vars[1]["unit"].get(), "nci")
                panel.clear()
                panel.intent_count.set(1)
                panel.create_rows()
                panel.sentence = lambda: "Please improve the connection"
                panel.add_current()
                self.assertEqual(len(panel.entries), 1)
                self.assertEqual(panel.entries[0]["sentence"], "Please improve the connection")
                self.assertEqual(str(panel.run_button["state"]), "disabled")
                panel.clear()
                panel.intent_count.set(2)
                panel.create_rows()
                panel.intents.selection_set(0)
                panel.remove_selected()
                panel.sentence = lambda: "UE ueId=131 needs at least 1 Mbps downlink, non-relaxable"
                panel.add_current()
                ids = [row["intentId"] for row in panel.entries]
                self.assertEqual(len(ids), len(set(ids)))
            finally:
                root.destroy()

    def test_model_file_roundtrip_and_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            console = OperatorConsole(runs_root=tmp)
            try:
                chosen = {**payload()["roleModels"], "method": "internal-monolith"}
                console._workspace_role_models(chosen)
                self.assertEqual(console._load_role_models(), chosen)
                path = Path(tmp) / "agent-role-models.json"
                record = json.loads(path.read_text())
                self.assertEqual(record["schemaVersion"], "agent-role-models/2.1.0")
                self.assertEqual(record["method"], "internal-monolith")
                path.write_text(json.dumps({"schemaVersion": "agent-role-models/1.0.0",
                                            "roleModels": {"intent": "old"}}))
                self.assertIsNone(console._load_role_models()["target"])
            finally:
                console.shutdown()

    def test_settings_roundtrip_and_v20_read(self):
        settings = {"trialsK": 7, "deadlineMs": None, "horizonMs": 90000,
                    "stopAfterRelaxedSuccess": True, "retention": "none", "retain": 3,
                    "unselectedFunctionRule": "keep-current",
                    "observation": {"dlGoodputMbps": {"settleMs": 0, "windowMs": 3000,
                        "statistic": "mean", "minCoverage": 0.8, "validityMs": 60000}},
                    "generation": {"trajectory": {"maxTokens": 900, "thinkingBudgetTokens": 2000,
                                                  "reasoningEffort": "low"}}}
        with tempfile.TemporaryDirectory() as tmp:
            console = OperatorConsole(runs_root=tmp)
            try:
                chosen = {**payload()["roleModels"], "method": "three-agent", "settings": settings}
                console._workspace_role_models(chosen)
                self.assertEqual(console._load_role_models(), chosen)
                request = console._sitting_request({**payload(), "settings": settings})
                self.assertEqual(request.settings, settings)
                path = Path(tmp) / "agent-role-models.json"
                path.write_text(json.dumps({"schemaVersion": "agent-role-models/2.0.0",
                                           "roleModels": payload()["roleModels"], "method": "three-agent"}))
                self.assertEqual(console._load_role_models()["target"], "mock:agent")
                self.assertNotIn("settings", console._load_role_models())
            finally:
                console.shutdown()

    def test_intent_missing_authorization_and_question_merge(self):
        record, label = intent_entry("Custom: UE ueId=132 needs at least 1.5 Mbps downlink", 1,
                                    {"steps": "2", "weight": "3", "priority": "2"})
        self.assertEqual(record["intentId"], "Custom")
        self.assertEqual(record["requirement"]["steps"], 2)
        self.assertIn("bound=missing", label)
        self.assertEqual(record["weight"], 3)
        missing, label = intent_entry("UE ueId=131 needs at least 1 Mbps downlink", 1, {})
        self.assertIsNone(missing["requirement"]["steps"])
        self.assertIn("steps=missing", label)
        questions = [{"intentId": "Custom", "field": "bound", "question": "Bound?"}]
        updated = answer_payload({"intents": [record], "answers": {"Custom": {"steps": 2}}}, questions, ["1.0"], 0)
        self.assertEqual(updated["answers"], {"Custom": {"steps": 2, "bound": 1.0}})
        self.assertEqual(updated["clarificationRound"], 1)

    def test_functions_and_decision_projection(self):
        rows = episode_rows({"T": {"t0": {"targetId": "T0", "levels": {"r": 0}, "cost": 0}},
            "C": {"candidates": [{"controlId": "C1", "functions": [{"functionId": "steer",
                "scope": "ue@131", "policy": {"servingCell": "87654321"}}], "predictedTarget": "T0"}]},
            "functionCatalog": [{"functionId": "steer", "xapp": "traffic-steering", "scopes": ["ue@131"]}],
            "calls": [{"role": "trajectory", "model": "mock:agent", "targetId": "T0",
                       "options": {"maxTokens": 1000}, "staleAtArrival": True}]})
        self.assertEqual(rows["targets"][0][-1], 0)
        self.assertIn('"r": 0', rows["targets"][0][-2])
        self.assertIn("steer", rows["controls"][0][1])
        self.assertEqual(rows["controls"][0][2], "T0")
        self.assertEqual(rows["catalog"][0][0], "steer")
        self.assertEqual(rows["decisions"][0][4], "T0")
        self.assertIn("maxTokens", rows["decisions"][0][5])
        self.assertTrue(rows["decisions"][0][6])

    def test_projection_latest_trial_unknown_and_best(self):
        episode = {"T": {"t0": {"targetId": "T0", "requirements": {"r": 3}},
                         "alternatives": [{"targetId": "T1", "requirements": {"r": 2},
                                           "concession": {"r": 1}}]},
                   "C": {"candidates": [{"controlId": c, "configuration": {"a": c}}
                                           for c in ("C0", "C1", "C2")]},
                   "trials": [{"controlId": "C0", "window": {"valid": True},
                               "success": {"T0": False, "T1": True}},
                              {"controlId": "C1", "window": {"valid": False},
                               "success": {"T0": False, "T1": False}}],
                   "bestAttained": {"targetId": "T1", "controlId": "C0"},
                   "calls": [{"role": "target", "model": "deterministic",
                              "fallbackReason": "invalid answer", "rationale": "bounded alternative"}]}
        rows = episode_rows(episode)
        self.assertEqual(rows["grid"][0], ("T0", "FAIL", "UNKNOWN", "untried"))
        self.assertEqual(rows["grid"][1], ("T1", "PASS * best", "UNKNOWN", "untried"))
        self.assertEqual(rows["decisions"][0][2], "invalid answer")
        self.assertEqual(len(rows["targets"]), 2)
        self.assertEqual(len(rows["controls"]), 3)
        self.assertEqual(episode_rows({})["grid"], [])

    def test_mock_composition_worker_publishes_complete_episode(self):
        with tempfile.TemporaryDirectory() as tmp:
            console = OperatorConsole(runs_root=tmp)
            records = []
            console.bus.subscribe("decision", records.append)
            console.attach_kernel_session(SimpleNamespace(mode="MOCK", case_id="fixture"))
            try:
                with patch("tools.liveconsole.agent.build_agent_sitting", wraps=__import__(
                        "tools.liveconsole.agent", fromlist=["build_agent_sitting"]).build_agent_sitting) as builder:
                    worker = console._workspace_run_sitting(payload())
                    worker.join(30)
                    self.assertFalse(worker.is_alive())
                    self.assertTrue(builder.called)
                console.bus.drain(budget=10000)
                final = records[-1]
                self.assertFalse(final["running"])
                self.assertNotIn("failed", final["status"])
                self.assertEqual(final["episode"]["sessionMode"], "MOCK")
                self.assertTrue(final["episode"]["trials"])
                self.assertTrue(episode_rows(final["episode"])["grid"])
                self.assertTrue(any(r.get("running") and r.get("episode", {}).get("trials") for r in records))
                self.assertTrue(list(Path(tmp).rglob("*-episode.json")))
            finally:
                console.shutdown()

    def test_disconnected_and_failed_run_show_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            console = OperatorConsole(runs_root=tmp)
            try:
                console._workspace_run_sitting(payload())
                self.assertIn("Attach", console.bus.snapshot("decision")["status"])
                console.attach_kernel_session(SimpleNamespace(mode="MOCK", case_id="fixture"))
                console._workspace_run_sitting({"sentences": []}).join(10)
                self.assertIn("failed", console.bus.snapshot("decision")["status"])
                self.assertFalse(console._sitting_busy)
            finally:
                console.shutdown()

    def test_stop_and_live_profile_routing_without_io(self):
        with tempfile.TemporaryDirectory() as tmp:
            console = OperatorConsole(runs_root=tmp)
            console.attach_kernel_session(SimpleNamespace(mode="LIVE", case_id="fixture"))
            sitting = SimpleNamespace(termination="OPERATOR_STOP")
            from unittest.mock import Mock
            sitting.confirm = Mock()
            sitting.request_stop = Mock()
            sitting.episode = lambda: SimpleNamespace(to_record=lambda: {"sessionMode": "LIVE"})
            sitting.run = lambda **kw: console._workspace_stop_sitting()
            try:
                with patch("tools.liveconsole.agent.build_agent_sitting", return_value=sitting) as builder, patch(
                        "tools.liveconsole.agent.write_agent_evidence", return_value={"episode": "fixture.json"}):
                    console._workspace_run_sitting(payload()).join(10)
                    self.assertEqual(builder.call_args.args[0], console.profile.source_path)
                    sitting.request_stop.assert_called_once()
                    sitting.confirm.assert_called_once()
            finally:
                console.shutdown()

    def test_refused_questions_are_published(self):
        from tools.liveconsole.agent import ClarificationNeeded
        questions = [{"intentId": "I2", "field": "bound", "question": "Bound?"}]
        with tempfile.TemporaryDirectory() as tmp:
            console = OperatorConsole(runs_root=tmp)
            console.attach_kernel_session(SimpleNamespace(mode="MOCK", case_id="fixture"))
            try:
                with patch("tools.liveconsole.agent.build_hardware_free_agent_sitting",
                           side_effect=ClarificationNeeded(questions, round_index=2, refused=True)):
                    console._workspace_run_sitting(payload()).join(10)
                record = console.bus.snapshot("decision")
                self.assertTrue(record["refused"])
                self.assertEqual(record["questions"], questions)
                self.assertEqual(record["clarificationRound"], 2)
                self.assertFalse(record["running"])
            finally:
                console.shutdown()

    @unittest.skipUnless(HAS_DISPLAY, "needs a Tk display")
    def test_panel_missing_bound_answer_to_mock_agent_grid(self):
        import tkinter as tk
        with tempfile.TemporaryDirectory() as tmp:
            root = tk.Tk()
            root.withdraw()
            console = OperatorConsole(runs_root=tmp)
            console.attach_kernel_session(SimpleNamespace(mode="MOCK", case_id="fixture"))
            workspace = IntentDecisionWorkspace(**console._workspace_kwargs("intent_decision"))
            def finish_worker():
                end = time.monotonic() + 30
                while getattr(console, "_sitting_busy", False) and time.monotonic() < end:
                    console.bus.drain(budget=10000)
                    root.update()
                    time.sleep(0.01)
                console.bus.drain(budget=10000)
                root.update()
                self.assertFalse(console._sitting_busy)
            try:
                workspace.build(root)
                panel = workspace.sitting
                workspace.form.set_intent_text(SENTENCES[0])
                panel.add_button.invoke()
                workspace.form.set_intent_text("I2: UE ueId=132 needs at least 1.5 Mbps downlink")
                panel.entry_fields["steps"].set("2")
                panel.entry_fields["weight"].set("2.5")
                panel.add_button.invoke()
                self.assertIn("bound=missing", panel.intents.get(1))
                for var in panel.models.values():
                    var.set("mock:agent")
                self.assertEqual(str(panel.run_button["state"]), "disabled")
                panel.missing_button.invoke()
                self.assertTrue(panel.question_values)
                self.assertFalse(panel.question_record.get("refused"))
                for question, var in zip(panel.question_record["questions"], panel.question_values):
                    self.assertEqual(question["field"], "bound")
                    var.set("1.0")
                panel.answer_button.invoke()
                finish_worker()
                self.assertNotIn("failed", panel.status_text.get())
                self.assertTrue(panel.tables["grid"].get_children())
                self.assertTrue(panel.tables["catalog"].get_children())
                self.assertTrue(panel.tables["controls"].get_children())
                self.assertEqual(panel.episode["intake"]["answers"]["I2"]["bound"], 1.0)
                self.assertTrue(any(c["model"] == "mock:agent" and c["accepted"]
                                    for c in panel.episode["calls"]))
                record = {"questions": [{"intentId": "I2", "field": "bound", "question": "Bound?"}],
                          "refused": True}
                panel.show_questions(record)
                self.assertEqual(str(panel.answer_button["state"]), "disabled")
            finally:
                workspace.destroy()
                console.shutdown()
                root.destroy()

    @unittest.skipUnless(HAS_DISPLAY, "needs a Tk display")
    def test_panel_add_remove_run_to_finished_grid(self):
        import tkinter as tk
        with tempfile.TemporaryDirectory() as tmp:
            root = tk.Tk()
            root.withdraw()
            console = OperatorConsole(runs_root=tmp)
            console.attach_kernel_session(SimpleNamespace(mode="MOCK", case_id="fixture"))
            workspace = IntentDecisionWorkspace(**console._workspace_kwargs("intent_decision"))
            try:
                workspace.build(root)
                for sentence in SENTENCES:
                    workspace.form.set_intent_text(sentence)
                    workspace.sitting.add_button.invoke()
                self.assertEqual(workspace.sitting.intents.size(), 2)
                from tools.liveconsole.agent import parse_agent_intents
                self.assertEqual(len(parse_agent_intents(console._sitting_request(workspace.sitting.payload()))), 2)
                workspace.sitting.intents.selection_set(1)
                workspace.sitting.remove_selected()
                workspace.sitting.add_button.invoke()
                for var in workspace.sitting.models.values():
                    var.set("mock:agent")
                workspace.sitting.run_button.invoke()
                end = time.monotonic() + 30
                while getattr(console, "_sitting_busy", False) and time.monotonic() < end:
                    console.bus.drain(budget=10000)
                    root.update()
                    time.sleep(0.01)
                console.bus.drain(budget=10000)
                self.assertFalse(console._sitting_busy)
                self.assertNotIn("failed", workspace.sitting.status_text.get())
                self.assertTrue(workspace.sitting.tables["grid"].get_children())
                self.assertTrue(workspace.sitting.tables["trials"].get_children())
            finally:
                workspace.destroy()
                console.shutdown()
                root.destroy()
