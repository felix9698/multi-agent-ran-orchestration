"""KAGT lane: the deterministic Intent and xApp agents, and their non-authority.

Authority: docs/architecture/SEAMS-GATE2.md; design section 4.2 ("no direct
actuator tools", "constructed with read-only views ... never with the
Kernel, the Write Gateway, an R1 client or a ledger").
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from assurance.advisors import deterministic_agents, grammar
from assurance.contracts.capability import CapabilityManifest
from assurance.contracts.catalog import Candidate
from assurance.contracts.target import ComparisonOperator, TypedConstraint
from assurance.core.addressing import content_hash
from assurance.core.provenance import DocumentStatus, Provenance, TypedQuantity

STAMP = "2026-08-21T09:00:00.000000Z"
EPOCH = content_hash({"epoch": 1})

REPO_ROOT = Path(__file__).resolve().parents[2]
KAGT_PACKAGE_DIRS = (
    REPO_ROOT / "assurance" / "advisors",
    REPO_ROOT / "assurance" / "collector",
)

FORBIDDEN_IMPORT_PREFIXES = (
    "assurance.kernel", "assurance.gateway", "assurance.contracts.ledgers",
    "coordinator", "executor", "gui", "decision", "collectors", "experiments",
    "calibration", "diagnosis", "subprocess", "socket", "telnetlib", "paramiko",
    "requests", "asyncio",
)
ALLOWED_ORAN_IMPORTS = {"oran.contract.jcs"}

#: Kernel-facade and Write-Gateway-operation names (see
#: tests/assurance/test_seams.py's FROZEN_METHODS): no advisory/collector
#: class KAGT defines may expose a method with any of these names.
FORBIDDEN_METHOD_NAMES = {
    "submit_advisory", "admit_contract", "admit_deployment", "freeze_epoch",
    "open_trial", "advance_trial", "issue_token", "reserve", "charge_harm",
    "ingest_raw_sample", "evaluate_trial", "close_evidence",
    "exhaustion_certificate", "release_target_vector", "terminate_case",
    "settle_trial", "recover",
    "prepare", "ready", "commit", "stop", "reverse_rollback",
    "reread_configuration", "confirm_recovery", "emergency_safe_state",
    "finalize_live", "query_transaction", "dispatch",
}


def _kagt_source_files():
    files = []
    for directory in KAGT_PACKAGE_DIRS:
        files.extend(sorted(directory.glob("*.py")))
    return files


def _imported_module_names(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level:
            names.append(node.module or "")
    return names


class NoAuthorityAccess(unittest.TestCase):
    """The advisors/collector packages import no authority component and
    define no method with an authority-component's name."""

    def test_no_forbidden_imports(self):
        violations = []
        for path in _kagt_source_files():
            for name in _imported_module_names(path):
                if name.startswith("oran"):
                    if name not in ALLOWED_ORAN_IMPORTS:
                        violations.append(f"{path.name}:{name}")
                    continue
                for banned in FORBIDDEN_IMPORT_PREFIXES:
                    if name == banned or name.startswith(banned + "."):
                        violations.append(f"{path.name}:{name}")
        self.assertEqual(violations, [], f"forbidden imports: {violations}")

    def test_no_forbidden_method_names(self):
        violations = []
        for path in _kagt_source_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            if item.name in FORBIDDEN_METHOD_NAMES:
                                violations.append(f"{path.name}:{node.name}.{item.name}")
        self.assertEqual(violations, [], f"forbidden authority methods: {violations}")

    def test_kagt_source_files_are_actually_scanned(self):
        """Guards the two tests above against a silently-empty glob."""
        self.assertGreaterEqual(len(_kagt_source_files()), 10)


class DeterministicIntentAgentBehaviour(unittest.TestCase):
    REGISTRY = {
        "PIN_TO_CELL": grammar.IntentGrammarEntry(
            objective_family="PIN_TO_CELL", keywords=("pin", "handover"),
            measurement_ref="m/serving-cell",
        ),
        "THROUGHPUT": grammar.IntentGrammarEntry(
            objective_family="ThroughputTarget", keywords=("throughput", "mbps"),
            measurement_ref="m/dl", default_unit="Mbps",
        ),
    }

    def test_drafts_a_constraint_with_a_draft_bound(self):
        agent = deterministic_agents.DeterministicIntentAgent()
        message = agent.draft_contract(
            utterance="keep throughput at least 3.5 Mbps for ueId=130",
            objective_registry=self.REGISTRY, capability_view=(),
            correlation_id="corr-1", epoch_hash=EPOCH, now=STAMP,
        )
        self.assertEqual(message.body.objective_family, "ThroughputTarget")
        self.assertEqual(message.body.scope_selector, {"ueId": "130"})
        self.assertEqual(len(message.body.proposed_constraints), 1)
        bound = message.body.proposed_constraints[0].bound
        self.assertFalse(bound.admissible_for_runtime)
        self.assertEqual(bound.document_status, DocumentStatus.DRAFT)
        self.assertEqual(bound.value, 3.5)
        self.assertLessEqual(len(message.body.explanation), 2000)

    def test_no_matching_objective_raises(self):
        agent = deterministic_agents.DeterministicIntentAgent()
        with self.assertRaises(grammar.IntentParseError):
            agent.draft_contract(
                utterance="please repaint the dashboard blue",
                objective_registry=self.REGISTRY, capability_view=(),
                correlation_id="corr-1", epoch_hash=EPOCH, now=STAMP,
            )

    def test_matched_objective_without_a_number_is_unsupported_not_invented(self):
        agent = deterministic_agents.DeterministicIntentAgent()
        message = agent.draft_contract(
            utterance="please improve the throughput somehow",
            objective_registry=self.REGISTRY, capability_view=(),
            correlation_id="corr-1", epoch_hash=EPOCH, now=STAMP,
        )
        self.assertEqual(message.body.proposed_constraints, ())
        self.assertEqual(len(message.body.unsupported_requests), 1)

    def test_repeated_calls_produce_distinct_message_ids(self):
        agent = deterministic_agents.DeterministicIntentAgent()
        ids = {
            agent.draft_contract(
                utterance="throughput at least 3 Mbps", objective_registry=self.REGISTRY,
                capability_view=(), correlation_id="corr-1", epoch_hash=EPOCH, now=STAMP,
            ).message_id
            for _ in range(3)
        }
        self.assertEqual(len(ids), 3)

    def test_explain_is_capped_and_deterministic(self):
        agent = deterministic_agents.DeterministicIntentAgent()
        first = agent.explain(subject_ref="case-1", state_view={"b": 2, "a": 1})
        second = agent.explain(subject_ref="case-1", state_view={"a": 1, "b": 2})
        self.assertEqual(first, second)
        self.assertLessEqual(len(first), 2000)


def _capability_manifest(constraints=()):
    identity = dict(
        version="1.0.0", schema_version="assurance/1.0.0",
        document_status="NORMATIVE", standard_mapping={},
    )
    return CapabilityManifest(
        contract_id="cap/ts", capability_id="cap/ts",
        supported_objectives=("PIN_TO_CELL",), constraints=constraints,
        actuator_refs=("act/1",), measurement_refs=("m/dl",), **identity,
    )


def _candidate(capability_ref: str, candidate_id: str = "cand-1") -> Candidate:
    return Candidate(
        candidate_id=candidate_id, target_ref="target/PIN_TO_CELL", option_ref="opt/pin",
        parameters={"targetCellId": "87654321"}, semantic_hash=content_hash({"c": candidate_id}),
        capability_ref=capability_ref,
    )


class RuleBasedXAppAgentBehaviour(unittest.TestCase):
    def test_applicable_when_capability_is_registered(self):
        constraint = TypedConstraint(
            "m/dl", ComparisonOperator.GREATER_OR_EQUAL,
            TypedQuantity(3.5, "Mbps", Provenance.MEASURED, "cal-1"),
        )
        manifest = _capability_manifest((constraint,))
        agent = deterministic_agents.RuleBasedXAppAgent()
        message = agent.assess_candidate(
            candidate=_candidate("cap/ts"), capability_view=(manifest,), measurement_view=(),
            correlation_id="corr-1", epoch_hash=EPOCH, now=STAMP,
        )
        self.assertTrue(message.body.applicable)
        self.assertEqual(len(message.body.expected_effect), 1)
        effect_bound = message.body.expected_effect[0].bound
        self.assertFalse(effect_bound.admissible_for_runtime)
        self.assertEqual(message.body.evidence_needs, ("m/dl",))

    def test_not_applicable_when_capability_is_absent(self):
        agent = deterministic_agents.RuleBasedXAppAgent()
        message = agent.assess_candidate(
            candidate=_candidate("cap/unregistered"), capability_view=(_capability_manifest(),),
            measurement_view=(), correlation_id="corr-1", epoch_hash=EPOCH, now=STAMP,
        )
        self.assertFalse(message.body.applicable)
        self.assertEqual(message.body.expected_effect, ())

    def test_never_rates_everything_applicable(self):
        """An xApp Agent that always says True is exactly what design section
        4.2 warns against; assert both outcomes are actually reachable."""
        agent = deterministic_agents.RuleBasedXAppAgent()
        manifest = _capability_manifest()
        applicable = agent.assess_candidate(
            candidate=_candidate("cap/ts"), capability_view=(manifest,), measurement_view=(),
            correlation_id="corr-1", epoch_hash=EPOCH, now=STAMP,
        )
        not_applicable = agent.assess_candidate(
            candidate=_candidate("cap/missing"), capability_view=(manifest,), measurement_view=(),
            correlation_id="corr-1", epoch_hash=EPOCH, now=STAMP,
        )
        self.assertTrue(applicable.body.applicable)
        self.assertFalse(not_applicable.body.applicable)


if __name__ == "__main__":
    unittest.main()
