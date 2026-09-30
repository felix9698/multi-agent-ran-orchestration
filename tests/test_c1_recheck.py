"""CI coverage for the C1 1.0.1 contract recheck and fail-closed gates."""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
RECHECK_PATH = REPO / "diagnosis" / "1.0.1-recheck.py"
BUNDLE = REPO / "contracts" / "oran-aic" / "1.0.1" / "shared-contract-bundle"


def _load_recheck_module():
    spec = importlib.util.spec_from_file_location("c1_recheck_1_0_1", RECHECK_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load C1 recheck module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class C1RecheckCiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.recheck = _load_recheck_module()
        cls.catalog = json.loads(
            (BUNDLE / "scenario-catalog.1.0.1.json").read_text(encoding="utf-8")
        )
        cls.runner = json.loads(
            (BUNDLE / "scenario-runner-contract.1.0.1.json").read_text(encoding="utf-8")
        )

    def test_recheck_runs_from_unittest_and_accepts_non_c1_bundle_files(self):
        completed = subprocess.run(
            [sys.executable, str(RECHECK_PATH)],
            cwd=REPO,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

    def test_unevaluable_rules_in_split_modes_are_rejected(self):
        mutations = {
            "SC-063": "RULE-O1-LIVE-VALUE-INVARIANTS",
            "SC-066": "RULE-O1-NOTIFY-AND-RETRIEVAL",
            "SC-069": "RULE-O1-SUBSCRIPTION-LIFECYCLE",
        }
        check = getattr(self.recheck, "assertion_rule_input_violations", lambda _catalog: [])
        for scenario_id, rule in mutations.items():
            with self.subTest(scenario=scenario_id, rule=rule):
                mutated = copy.deepcopy(self.catalog)
                scenario = next(item for item in mutated["scenarios"] if item["id"] == scenario_id)
                scenario["rules"].append(rule)
                issues = check(mutated)
                self.assertTrue(
                    any(scenario_id in issue and rule in issue for issue in issues),
                    issues,
                )

    def test_every_rule_declares_machine_readable_inputs(self):
        for rule_id, rule in self.catalog["assertionRules"].items():
            with self.subTest(rule=rule_id):
                self.assertIsInstance(rule, dict)
                self.assertIsInstance(rule.get("semantics"), str)
                evaluation = rule.get("evaluation", {})
                self.assertTrue(evaluation.get("allowedFixtureModes"))
                self.assertTrue(evaluation.get("inputAlternatives"))

    def test_fault_boundaries_have_one_explicit_machine_interpretation(self):
        for fault_type in ("CRASH_PROCESS", "DROP_CALLBACK_DELIVERY"):
            specification = self.runner["faults"][fault_type]
            self.assertIn("boundary", specification["required"])
            boundary = specification["boundary"]
            self.assertEqual("EXACTLY_ONE", boundary["stepAnchorCardinality"])
            self.assertEqual(
                {"afterStep", "beforeStep"}, set(boundary["stepAnchorSemantics"])
            )
            self.assertTrue(boundary["allowedValues"])

        for scenario in self.catalog["scenarios"]:
            step_ids = {step["id"] for step in scenario["materialization"]["steps"]}
            for fault in scenario["materialization"]["faults"]:
                if fault["type"] not in {"CRASH_PROCESS", "DROP_CALLBACK_DELIVERY"}:
                    continue
                specification = self.runner["faults"][fault["type"]]
                anchors = [name for name in ("afterStep", "beforeStep") if name in fault]
                self.assertEqual([specification["boundary"]["allowedValues"][fault["boundary"]]["anchorField"]], anchors)
                self.assertIn(fault[anchors[0]], step_ids)

    def test_sc033_installs_and_expects_the_required_status_notification(self):
        scenario = next(
            item for item in self.catalog["scenarios"] if item["id"] == "SC-033"
        )
        installed_destinations = [
            state
            for state in scenario["materialization"]["initialState"]
            if isinstance(state, dict)
            and state.get("op") == "INSTALL_STATUS_DESTINATION"
        ]
        self.assertEqual(
            [{
                "op": "INSTALL_STATUS_DESTINATION",
                "url": "{a1StatusCallbackRoot}/notifications",
            }],
            installed_destinations,
        )
        self.assertEqual(
            [{
                "id": "disconnect",
                "atMs": 0,
                "op": "SET_DEPENDENCY",
                "dependency": "E2_NODE_ASSOCIATION",
                "ready": False,
            }],
            scenario["materialization"]["steps"],
        )
        self.assertTrue(
            {"statusSnapshot", "callbackStatus", "callbackAttempts"}.issubset(
                self.runner["operations"]["SET_DEPENDENCY"]["declaredOutputs"]
            )
        )
        self.assertEqual(1, scenario["expected"]["a1StatusCallbackAttempts"])
        self.assertEqual([204], scenario["expected"]["httpSequence"])

    def test_vacuous_rule_error_is_registered_in_preflight(self):
        preflight = self.runner["preflight"]
        self.assertEqual(
            "AIC_RUNNER_VACUOUS_RULE", preflight["declaredRuleInputError"]
        )
        self.assertEqual(
            preflight["declaredRuleInputError"],
            self.runner["assertionRuleEvaluation"]["failureCode"],
        )


if __name__ == "__main__":
    unittest.main()
