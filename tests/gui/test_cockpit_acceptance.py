"""The thirteen Cockpit completion conditions of task section 9, one by one.

The section lists thirteen things a finished Research Operations Cockpit does.
Twelve of them are properties of what the operator can read and reach; the
thirteenth, like the ninth, is a *fail-closed* property -- something the console
must be unable to do -- and those two are asserted as missing capabilities
rather than as absent buttons.

One test class per condition, named for it, so a failure says which of the
thirteen regressed.  Everything runs against the real console, the real
projections and a real hardware-free Assurance Kernel; nothing here contacts a
radio, an xApp, a model or a network.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import List, Mapping, Optional, Sequence, Set, Tuple

from assurance.core.axes import EvidenceCellStatus, TrialOutcome

from gui.operator import data_class as dc
from gui.operator import status as st
from gui.operator.app import OperatorConsole
from gui.operator.session.controller import SessionError
from gui.operator.sources import batch as batch_source
from gui.operator.sources import cockpit
from gui.operator.sources import kernel_live as kl
from gui.operator.viewmodel.types import SessionState
from gui.operator.workspaces import COCKPIT_WORKSPACES, WORKSPACE_MODULES
from gui.operator.workspaces import contract_studio as cs
from gui.operator.workspaces import demo as demo_pane
from gui.operator.workspaces import evidence_ledger as el
from gui.operator.workspaces import trial_safety as ts
from tests.assurance.pin_to_cell_support import HOME_NCI, TARGET_NCI
from tests.gui.kernel_submission_support import (
    KernelSubmissionFixture,
    PIN_UTTERANCE,
    UNSERVABLE_UTTERANCE,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COCKPIT_PACKAGE = REPOSITORY_ROOT / "gui" / "operator"


def cockpit_sources() -> Tuple[Path, ...]:
    """Every production module of the Cockpit, collected from the tree.

    Derived, never listed.  An earlier version of this file named seven files
    by hand and the fail-closed scans below therefore covered seven files: an
    override write injected into ``workspaces/contract_studio.py`` -- one of
    section 9's own eight workspaces -- passed the whole suite, because the
    scan never opened it.  A hand-written allowlist of sources is a guard that
    silently shrinks every time the thing it guards grows, which is the one
    failure mode a coverage guard cannot have.

    ``gui/dashboard.py`` and ``gui/legacy_tools.py`` are deliberately outside
    this set.  They are the *legacy* research console, not the Cockpit;
    ``docs/phase-b-gui/boundary-map.1.0.0.json`` forbids the Cockpit from
    importing them at all, and that prohibition -- not this scan -- is what
    holds them at arm's length.
    """
    return tuple(sorted(COCKPIT_PACKAGE.rglob("*.py")))


COCKPIT_SOURCES: Tuple[Path, ...] = cockpit_sources()

#: Floor on the collected set.  Without it a glob that silently matched
#: nothing would make every scan in this file pass vacuously -- an empty
#: ``for path in COCKPIT_SOURCES`` finds no offender by construction.  The
#: number is well under the real count; it exists to catch zero, not to pin a
#: file count nobody should have to update.
MINIMUM_COCKPIT_SOURCES = 40

#: The seven files the hand-written list used to name, plus the three the
#: reviewer proved were missing from it.  Kept as a regression floor so the
#: collector cannot quietly stop reaching them again.
MUST_BE_COVERED = (
    "gui/operator/sources/cockpit.py",
    "gui/operator/sources/batch.py",
    "gui/operator/data_class.py",
    "gui/operator/shell/cockpit_header.py",
    "gui/operator/workspaces/trial_safety.py",
    "gui/operator/workspaces/evidence_ledger.py",
    "gui/operator/workspaces/batch_experiments.py",
    # The gap: all three were changed by this campaign and none was scanned.
    "gui/operator/workspaces/contract_studio.py",
    "gui/operator/app.py",
    "gui/operator/sources/kernel_live.py",
)


def relative(path: Path) -> str:
    """Repository-relative posix path, for offender messages and allowlists.

    Offenders are reported by path rather than by ``path.name``: the collected
    set holds several ``__init__.py`` files, and a bare file name would make
    two different offenders read as one.

    A path outside the repository -- the temporary mutated copies the coverage
    tests below hand these scanners -- keeps its own posix path, so it matches
    no allowlist key.  That is the correct behaviour for a mutation test: the
    copy must be judged on its content, not inherit an exception.
    """
    try:
        return path.relative_to(REPOSITORY_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


#: Task section 5.3: the vocabulary the design removed.  Checked as exact
#: identifier names over the Cockpit package, the same way
#: ``tests/assurance/test_seams.py`` checks the Kernel package -- a docstring
#: quoting the design stays legal, a field named ``signer`` does not.
FORBIDDEN_IDENTIFIERS = {
    "signer", "signer_id", "signers", "signature", "signatures",
    "signature_bytes", "signed_by", "trusted_signer", "approval", "approver",
    "approved_by", "joint_approval", "governance", "threshold", "thresholds",
    "theta_star", "role", "roles", "private_key", "certificate",
}

#: The one identifier the frozen view-model seam carries that this vocabulary
#: also names, allowed in one file and for that name only.
#:
#: ``theta_star`` on ``DecisionView`` and ``CalibrationView`` is the legacy
#: Coordinator's Eq.17 calibration quantity -- a published experimental value
#: on the pre-Kernel path, predating this design by two gates, and part of a
#: seam the design step froze.  It is not the thing section 5.3 removed, which
#: is *human authority*: signers, roles, approvals and approval quorums.  The
#: exception is keyed by ``(file, identifier)`` rather than by file, so a
#: ``signer`` appearing in the same module still fails.
FROZEN_SEAM_VOCABULARY: Mapping[str, frozenset] = {
    "gui/operator/viewmodel/types.py": frozenset({"theta_star"}),
}

#: **Empty, and it must stay empty.**  Five Cockpit modules used to import the
#: pre-Kernel Coordinator's composition surface directly, which is what made
#: that runtime reachable from the default entry point: loading a profile and
#: pressing Bind constructed ``IntentCoordinator`` and, behind it, the
#: patched-OAI direct-control executor.
#:
#: The B-01 cutover inverted that dependency.  The preserved runtime is now
#: handed to a console from outside it -- ``tools.legacy.episode_support``,
#: through ``OperatorConsole.legacy_episode`` -- exactly the way a Kernel
#: submission session is.  A console built by ``main.py`` is handed nothing, so
#: the legacy path is unreachable *structurally* rather than by a flag, and no
#: file in this package needs an exception any more.
#:
#: Re-adding a key here re-opens that door.  Anything the console genuinely
#: needs from a neutral module belongs in :data:`NEUTRAL_READ_ONLY_IMPORTS`,
#: which carries a transitive-purity proof rather than a promise.
LEGACY_COORDINATOR_IMPORTS: Mapping[str, frozenset] = {}

#: The one ``oran.rapp`` module the Cockpit still reads, and why it is not an
#: exception of the kind above.
#:
#: ``oran.rapp.status_projection`` is a pure, read-only projection: it declares
#: ``READ_ONLY = True``, ``tests/test_gui_boundary_scan.py`` scans it for
#: mutating R1 calls, and ``PreflightRunner.check_boundary_declaration``
#: *deliberately* imports it to assert that declaration.  It imports nothing
#: but the standard library -- no coordinator, no executor, no transport --
#: and ``tests/gui/test_default_entry_reachability.py``'s
#: ``TheNeutralProjectionIsTransitivelyPure`` proves that in a clean process
#: rather than taking this comment's word for it.
NEUTRAL_READ_ONLY_IMPORTS: frozenset = frozenset({
    "oran.rapp",
    "oran.rapp.status_projection",
})

#: Dataclasses in the Cockpit package that are mutable by design, with why.
#: Every one is console-local state; none of them projects a Kernel decision,
#: which is the property the frozen scan exists to protect.
MUTABLE_BY_DESIGN: Mapping[str, str] = {
    "gui.operator.session.composition.LiveComposition":
        "a deployment binding the console mutates as it binds and detaches",
    "gui.operator.shell.window.TickStats":
        "drain-loop timings, a counter rather than a projection",
    "gui.operator.widgets.tables.TableModel":
        "a widget's own row buffer",
    "gui.operator.workspaces.analysis.Selection":
        "which run the operator has selected in the Analysis pane",
}


def cockpit_modules() -> Tuple[str, ...]:
    """Importable dotted names for every collected Cockpit source."""
    names = []
    for path in COCKPIT_SOURCES:
        dotted = relative(path)[: -len(".py")].replace("/", ".")
        if dotted.endswith(".__init__"):
            dotted = dotted[: -len(".__init__")]
        names.append(dotted)
    return tuple(names)


#: Transports, actuators, process controls and collectors.  Task section 7.3:
#: the GUI is not allowed a direct E2/xApp/PRB/SSH/process/USRP path, and the
#: cheapest proof is that none of it is importable from the console.
BANNED_TRANSPORT_IMPORTS = frozenset({
    "socket", "telnetlib", "paramiko", "requests", "subprocess",
    "http.client", "urllib.request", "executor", "collectors",
    "coordinator", "oran.o1", "oran.rapp",
    # Cockpit may read the data-only receipt module, but it must never import
    # the Lab Setup process-driving surface.
    "tools.labctl.cli", "tools.labctl.executor", "tools.labctl.orchestrator",
    # The pre-Kernel decision runtime and the console it was driven from.  The
    # Cockpit reaches none of it: what it needs is handed in by composition.
    "oran.rapp.gui_entry", "oran.rapp.coordinator_adapter",
    "executor.oai_executor", "gui.dashboard", "tools.legacy",
})


def _imported_modules(tree: ast.AST) -> List[Tuple[str, int]]:
    found: List[Tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(alias.name, node.lineno) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level:
            found.append((node.module or "", node.lineno))
    return found


def transport_import_offenders(paths: Sequence[Path]) -> List[str]:
    """Every transport/actuator import in ``paths`` that is not allowlisted.

    Shared by the guard and by the coverage test that mutates a source file to
    prove the guard reaches it, so the two can never check different things.
    """
    offenders: List[str] = []
    for path in paths:
        key = relative(path)
        allowed = (LEGACY_COORDINATOR_IMPORTS.get(key, frozenset())
                   | NEUTRAL_READ_ONLY_IMPORTS)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for name, lineno in _imported_modules(tree):
            if name in allowed:
                continue
            if any(name == item or name.startswith(item + ".")
                   for item in BANNED_TRANSPORT_IMPORTS):
                offenders.append(f"{key}:{lineno}:{name}")
    return offenders


#: Attributes that carry a Kernel decision.  Assigning to one of these from
#: the console is the override task section 9.9 forbids.
KERNEL_DECISION_ATTRIBUTES = frozenset({
    "outcome", "trial_outcome", "verdict", "predicate_verdicts",
    "evidence_status", "status", "harm_charge", "harmCharge", "charged",
    "rolled_back", "settlement", "closure_progress",
})


def kernel_decision_write_offenders(paths: Sequence[Path]) -> List[str]:
    """Every assignment to a Kernel-decision attribute in ``paths``."""
    offenders: List[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AugAssign)):
                continue
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [node.target])
            for target in targets:
                if (isinstance(target, ast.Attribute)
                        and target.attr in KERNEL_DECISION_ATTRIBUTES):
                    offenders.append(
                        f"{relative(path)}:{target.lineno}:{target.attr}")
    return offenders


def authority_vocabulary_offenders(
        paths: Sequence[Path],
        exceptions: Optional[Mapping[str, frozenset]] = None) -> List[str]:
    """Every section 5.3 identifier in ``paths`` that is not allowlisted.

    ``exceptions`` defaults to :data:`FROZEN_SEAM_VOCABULARY`.  It is a
    parameter so the test that proves an exception is still load-bearing can
    re-run the scan without it, rather than mutating a module-level table that
    every other test in the file reads.
    """
    table = (FROZEN_SEAM_VOCABULARY if exceptions is None else exceptions)
    offenders: List[str] = []
    for path in paths:
        key = relative(path)
        allowed = table.get(key, frozenset())
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            name = None
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                name = node.name
            elif isinstance(node, ast.arg):
                name = node.arg
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                name = node.id
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx,
                                                                ast.Store):
                name = node.attr
            if not name or name.lower() in allowed:
                continue
            if name.lower() in FORBIDDEN_IDENTIFIERS:
                offenders.append(f"{key}:{node.lineno}:{name}")
    return offenders


def settled_session(**kwargs):
    """One hardware-free PIN_TO_CELL submission, run to a terminal state."""
    fixture = KernelSubmissionFixture()
    session = fixture.session(**kwargs)
    session.draft(PIN_UTTERANCE)
    session.confirm()
    session.start()
    return fixture, session


# --------------------------------------------------------------------------- #
# 1. one correlation, intent -> contract -> mapping -> trial -> evidence ->
#    verdict -> recovery
# --------------------------------------------------------------------------- #

class Condition01OneCorrelationEndToEnd(unittest.TestCase):

    def setUp(self) -> None:
        self.fixture, self.session = settled_session()
        self.snapshot = cockpit.project_session(self.session)

    def test_the_chain_has_every_link_the_section_names(self) -> None:
        self.assertEqual(tuple(step.key for step in self.snapshot.chain),
                         tuple(key for key, _label
                               in cockpit.CORRELATION_STEPS))

    def test_every_link_resolved_on_a_settled_success(self) -> None:
        for step in self.snapshot.chain:
            with self.subTest(step=step.key):
                self.assertNotEqual(step.value, st.PRE_MEASUREMENT,
                                    f"{step.key} has nothing to show")
                self.assertNotEqual(step.status, st.UNKNOWN)

    def test_the_links_carry_the_kernel_s_own_identifiers(self) -> None:
        chain = {step.key: step for step in self.snapshot.chain}
        self.assertIn(PIN_UTTERANCE, chain["intent"].value)
        self.assertIn("UeCellSteeringPinToCell",
                      chain["contract_preview"].value)
        self.assertIn(self.session.trial_id, chain["candidate_trial"].value)
        self.assertEqual(chain["verdict"].value, TrialOutcome.SUCCESS.value)

    def test_the_correlation_id_is_the_case_id_everything_is_keyed_by(
            self) -> None:
        self.assertEqual(self.snapshot.correlation_id, self.session.case_id)
        self.assertEqual(self.snapshot.header.case_id, self.session.case_id)
        self.assertEqual(self.snapshot.trial_safety.case_id,
                         self.session.case_id)
        self.assertEqual(self.snapshot.evidence.case_id, self.session.case_id)

    def test_the_recovery_link_is_populated_when_a_rollback_happened(
            self) -> None:
        _fixture, session = settled_session(
            observed=(TARGET_NCI, TARGET_NCI, HOME_NCI, TARGET_NCI,
                      TARGET_NCI))
        chain = {step.key: step
                 for step in cockpit.project_session(session).chain}
        self.assertIn("rolled back", chain["recovery"].value)
        self.assertIn("recovery verified yes", chain["recovery"].value)
        self.assertEqual(chain["verdict"].value, TrialOutcome.FAIL.value)

    def test_each_link_states_a_status_of_its_own(self) -> None:
        """A single chain status would hide a closed trial with an open cell."""
        snapshot = cockpit.project({}, case_id=None, mode="DISCONNECTED")
        for step in snapshot.chain:
            with self.subTest(step=step.key):
                self.assertEqual(step.status, st.UNKNOWN)
                self.assertTrue(step.reason, f"{step.key} is bare")

    def test_the_studio_renders_the_chain_with_its_correlation_id(self) -> None:
        text = "\n".join(cs.chain_lines(self.snapshot.chain,
                                        self.snapshot.correlation_id))
        self.assertIn(self.session.case_id, text)
        for key, label in cockpit.CORRELATION_STEPS:
            with self.subTest(step=key):
                self.assertIn(label, text)


# --------------------------------------------------------------------------- #
# 2. Intent Profile, target options, harm limits, measurement/hold,
#    capability limitation and unsupported reason
# --------------------------------------------------------------------------- #

class Condition02TheOperatorCanReadTheContract(unittest.TestCase):

    def setUp(self) -> None:
        self.fixture, self.session = settled_session()
        self.context = cockpit.project_session(self.session).context
        self.text = "\n".join(cs.context_lines(self.context))

    def test_the_intent_profile_is_shown(self) -> None:
        self.assertEqual(self.context.intent_text, PIN_UTTERANCE)
        self.assertEqual(self.context.objective_family,
                         "UeCellSteeringPinToCell")
        self.assertIn("Intent Profile", self.text)
        self.assertIn(PIN_UTTERANCE, self.text)

    def test_the_target_options_and_their_parameter_space_are_shown(
            self) -> None:
        self.assertTrue(self.context.options)
        option = self.context.options[0]
        self.assertEqual(option.option_ref, "option/pin-to-cell")
        self.assertIn("servingCell", option.parameter_space)
        self.assertIn("Target options", self.text)
        self.assertIn("option/pin-to-cell", self.text)
        self.assertIn("parameter space", self.text)

    def test_the_harm_limit_carries_what_makes_it_admissible(self) -> None:
        """Task section 5.8: a sample maximum alone is not an admission bound."""
        self.assertTrue(self.context.harm_limits)
        bound = self.context.harm_limits[0]
        self.assertEqual(bound.admissible_value, 22.0)
        self.assertEqual(bound.measured_value, 20.0)
        self.assertEqual(bound.conservative_margin, 2.0)
        self.assertEqual(bound.enforced_timeout_ms, 10000)
        self.assertTrue(bound.proof_ref)
        self.assertTrue(bound.calibration_records)
        self.assertTrue(bound.operating_scope)
        for token in ("admissible", "measured", "conservative margin",
                      "enforced timeout", "proof", "calibration"):
            with self.subTest(token=token):
                self.assertIn(token, self.text)

    def test_the_measurement_and_hold_windows_are_shown_in_full(self) -> None:
        self.assertTrue(self.context.measurements)
        row = self.context.measurements[0]
        self.assertEqual(row.cadence_ms, 1000)
        self.assertEqual(row.window_width_ms, 3000)
        self.assertEqual(row.hold_ms, 3000)
        self.assertEqual(row.freshness_bound_ms, 2000)
        self.assertEqual(row.clock_requirement, "SYNCHRONISED_REQUIRED")
        for token in ("cadence", "window", "hold", "freshness", "clock",
                      "aggregation", "estimator", "gap"):
            with self.subTest(token=token):
                self.assertIn(token, self.text)

    def test_the_capability_limitation_is_shown(self) -> None:
        self.assertTrue(self.context.capabilities)
        capability = self.context.capabilities[0]
        self.assertEqual(capability.supported_objectives,
                         ("UeCellSteeringPinToCell",))
        self.assertIn("Capability limitation", self.text)
        self.assertIn("capability/ue-cell-steering", self.text)

    def test_the_objective_maps_onto_published_standard_versions(self) -> None:
        """Task section 7.8, on the screen the operator submits from."""
        labels = dict(self.context.standard_mapping)
        self.assertIn("A1 interface", labels)
        self.assertIn("A1 policy type", labels)
        self.assertTrue(any(label == "E2 control"
                            for label, _value
                            in self.context.standard_mapping))
        self.assertIn("Objective / standard mapping", self.text)

    def test_an_unsupported_request_names_itself(self) -> None:
        fixture = KernelSubmissionFixture()
        session = fixture.session()
        with self.assertRaises(kl.SubmissionRefused) as caught:
            session.draft(UNSERVABLE_UTTERANCE)
        self.assertEqual(caught.exception.reason, "INTENT_NOT_RECOGNISED")

    def test_a_family_outside_the_registry_is_unsupported_with_a_reason(
            self) -> None:
        mapping, status, reason = cockpit.standard_mapping_for("NotAnObjective")
        self.assertEqual(mapping, ())
        self.assertEqual(status, st.UNSUPPORTED)
        self.assertIn("NotAnObjective", reason or "")

    def test_a_console_with_no_epoch_says_so_rather_than_going_blank(
            self) -> None:
        context = cockpit.contract_context_view({}, mode="DISCONNECTED")
        self.assertTrue(context.unavailable_reason)
        text = "\n".join(cs.context_lines(context))
        self.assertIn("Unknown", text)
        self.assertIn(context.unavailable_reason, text)


# --------------------------------------------------------------------------- #
# 3. Interactive and Batch use the same Kernel and the same evidence store
# --------------------------------------------------------------------------- #

class Condition03OneKernelOneEvidenceStore(unittest.TestCase):

    def test_the_batch_executor_drives_the_interactive_session_class(
            self) -> None:
        """No second decision path: draft, confirm, start -- the same three."""
        source = (REPOSITORY_ROOT / "gui" / "operator" / "sources"
                  / "batch.py").read_text(encoding="utf-8")
        for call in ("session.draft(", "session.confirm()", "session.start()"):
            with self.subTest(call=call):
                self.assertIn(call, source)

    def test_a_batch_case_reaches_the_console_s_own_kernel_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = KernelSubmissionFixture()
            session = fixture.session()
            batch = batch_source.BatchSession(
                runs_root=tmp, session_factory=lambda case: session,
                mode="MOCK",
                draft=batch_source.PlanDraft(
                    objectives="UeCellSteeringPinToCell",
                    profiles=f"pin={PIN_UTTERANCE}", strategy="deterministic",
                    repeats="1", seed="3", budget_cases="4"))
            batch.confirm(now="2026-08-21T09:00:00.000000Z")
            batch.start()
            self.assertIs(batch.executor.sessions[0].path.kernel,
                          fixture.kernel)

    def test_the_run_store_class_is_shared_by_identity(self) -> None:
        from assurance.batch import runner as batch_runner
        from gui.operator.store import session_store as console_store

        self.assertIs(console_store.SessionStore, batch_runner.SessionStore)

    def test_the_batch_runner_refuses_a_live_executor(self) -> None:
        """The hardware-free lane is enforced by the runner, not by a habit."""
        from assurance.batch.runner import BatchRunner

        class LiveExecutor:
            mode = "LIVE"

            def execute(self, case, plan):                  # pragma: no cover
                raise AssertionError("must never be reached")

        with tempfile.TemporaryDirectory() as tmp:
            runner = BatchRunner(executor=LiveExecutor(), runs_root=tmp)
            plan = batch_source.PlanDraft(
                objectives="UeCellSteeringPinToCell",
                profiles=f"pin={PIN_UTTERANCE}", strategy="deterministic",
                repeats="1", seed="3", budget_cases="4").build()
            with self.assertRaises(ValueError):
                runner.run(plan, confirmation=plan.confirm(
                    event_id="confirm/x",
                    timestamp="2026-08-21T09:00:00.000000Z"))


# --------------------------------------------------------------------------- #
# 4. Interactive confirms one case; Batch confirms a bounded plan once
# --------------------------------------------------------------------------- #

class Condition04OneCaseVersusOneBoundedPlan(unittest.TestCase):

    def test_the_interactive_run_confirms_one_case_and_starts_it(self) -> None:
        fixture = KernelSubmissionFixture()
        session = fixture.session()
        session.draft(PIN_UTTERANCE)
        with self.assertRaises(kl.ConfirmationRequired):
            session.start()
        session.confirm()
        session.start()
        self.assertIsNotNone(session.trial_id)

    def test_a_second_start_of_the_same_case_is_refused(self) -> None:
        _fixture, session = settled_session()
        with self.assertRaises(kl.SubmissionRefused) as caught:
            session.start()
        self.assertEqual(caught.exception.reason, "TRIAL_ALREADY_RUN")

    def test_the_batch_confirms_the_whole_plan_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            batch = batch_source.BatchSession(
                runs_root=tmp, session_factory=lambda case: None, mode="MOCK",
                draft=batch_source.PlanDraft(
                    objectives="A,B", profiles="p=one,q=two",
                    strategy="deterministic", repeats="2", seed="1",
                    budget_cases="16"))
            view = batch.view()
            self.assertEqual(view.case_count, 8)
            record = batch.confirm(now="2026-08-21T09:00:00.000000Z")
            self.assertEqual(record.confirmed_content_hash,
                             view.plan_content_hash)
            self.assertEqual(record.confirmed_object_type, "BatchPlan")

    def test_the_scope_cannot_change_once_the_repetition_starts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = KernelSubmissionFixture()
            session = fixture.session()
            batch = batch_source.BatchSession(
                runs_root=tmp, session_factory=lambda case: session,
                mode="MOCK",
                draft=batch_source.PlanDraft(
                    objectives="UeCellSteeringPinToCell",
                    profiles=f"pin={PIN_UTTERANCE}", strategy="deterministic",
                    repeats="1", seed="1", budget_cases="4"))
            batch.confirm(now="2026-08-21T09:00:00.000000Z")
            before = batch.view().plan_content_hash
            batch.start()
            with self.assertRaises(batch_source.BatchRefused) as caught:
                batch.edit("seed", "2")
            self.assertEqual(caught.exception.code, "BATCH_SCOPE_LOCKED")
            self.assertEqual(batch.view().plan_content_hash, before)


# --------------------------------------------------------------------------- #
# 5. the existing valid functions are preserved and relocated
# --------------------------------------------------------------------------- #

class Condition05TheExistingFunctionsSurvive(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.console = OperatorConsole(runs_root=self._tmp.name)
        self.addCleanup(self.console.shutdown)

    def test_intent_history_still_has_a_pane(self) -> None:
        pane = self.console.workspace("intent_decision")
        self.assertIsNotNone(pane)
        self.assertTrue(hasattr(pane, "on_state"))

    def test_the_commercial_and_local_model_selector_is_still_routed(
            self) -> None:
        routed = set(self.console.action_keys())
        self.assertIn("llm_select", routed)
        self.assertIn("llm_refresh", routed)

    def test_the_multi_stage_reasoning_view_is_still_a_state_field(
            self) -> None:
        from gui.operator.viewmodel.types import DecisionView

        import dataclasses

        fields = {f.name for f in dataclasses.fields(DecisionView)}
        self.assertIn("llm_stages", fields)
        self.assertIn("fsm_stages", fields)

    def test_topology_real_time_chart_and_analysis_still_have_panes(
            self) -> None:
        for workspace_id in ("live_ops", "analysis", "demo"):
            with self.subTest(workspace=workspace_id):
                self.assertIsNotNone(self.console.workspace(workspace_id))

    def test_the_export_and_stored_run_actions_are_still_routed(self) -> None:
        routed = set(self.console.action_keys())
        for key in ("export", "load_run", "load_replay"):
            with self.subTest(action=key):
                self.assertIn(key, routed)


# --------------------------------------------------------------------------- #
# 6. an agent proposal and a Kernel decision never share a badge
# --------------------------------------------------------------------------- #

class Condition06ProposalAndDecisionAreDistinct(unittest.TestCase):

    def setUp(self) -> None:
        self.fixture, self.session = settled_session()
        self.view = self.session.view()

    def test_the_three_origins_are_three_distinct_labels(self) -> None:
        labels = {kl.ORIGIN_LABELS[key]
                  for key in (kl.ORIGIN_AGENT, kl.ORIGIN_OPERATOR,
                              kl.ORIGIN_KERNEL)}
        self.assertEqual(len(labels), 3)

    def test_the_preview_is_marked_as_the_agent_s_reading(self) -> None:
        text = "\n".join(cs.preview_lines(self.view))
        self.assertIn(kl.ORIGIN_LABELS[kl.ORIGIN_AGENT], text)

    def test_the_settlement_is_marked_as_the_kernel_s_decision(self) -> None:
        text = "\n".join(cs.settlement_lines(self.view))
        self.assertIn(kl.ORIGIN_LABELS[kl.ORIGIN_KERNEL], text)
        self.assertNotIn(kl.ORIGIN_LABELS[kl.ORIGIN_AGENT], text)

    def test_the_kernel_backed_panes_mark_their_source(self) -> None:
        snapshot = cockpit.project_session(self.session)
        candidates = "\n".join(ts.candidate_lines(snapshot.trial_safety))
        self.assertIn("KERNEL_DECISION", candidates)

    def test_a_refused_proposal_is_the_kernel_s_answer_not_a_retry(
            self) -> None:
        fixture = KernelSubmissionFixture()
        session = fixture.session()
        with self.assertRaises(kl.SubmissionRefused):
            session.draft(UNSERVABLE_UTTERANCE)
        self.assertIsNone(session.preview)
        self.assertIsNone(session.trial_id)


# --------------------------------------------------------------------------- #
# 7. LIVE / REPLAY / DERIVED / UNKNOWN / UNSUPPORTED are visually distinct
# --------------------------------------------------------------------------- #

class Condition07TheFiveDataClassesAreDrawnApart(unittest.TestCase):

    def test_the_vocabulary_carries_all_five_with_distinct_glyphs(self) -> None:
        glyphs = {dc.DATA_CLASS_SPECS[name].glyph
                  for name in dc.DATA_CLASS_ORDER}
        self.assertEqual(len(glyphs), 5)

    def test_the_header_marks_every_cell_with_its_data_class(self) -> None:
        _fixture, session = settled_session()
        snapshot = cockpit.project_session(session)
        for item in cockpit.header_items(snapshot.header):
            with self.subTest(item=item.key):
                self.assertIn(item.badge.data_class, dc.DATA_CLASS_ORDER)
                self.assertIn(item.badge.glyph, item.as_text())

    def test_a_derived_number_never_shares_a_badge_with_an_observation(
            self) -> None:
        _fixture, session = settled_session()
        snapshot = cockpit.project_session(session)
        items = {item.key: item
                 for item in cockpit.header_items(snapshot.header)}
        self.assertEqual(items["harm"].badge.data_class, dc.DERIVED)
        self.assertNotEqual(items["measurement"].badge.data_class, dc.DERIVED)

    def test_the_trial_safety_pane_prints_the_legend(self) -> None:
        source = (REPOSITORY_ROOT / "gui" / "operator" / "workspaces"
                  / "trial_safety.py").read_text(encoding="utf-8")
        self.assertIn("dc.legend()", source)


# --------------------------------------------------------------------------- #
# 8. the Demo View is a simple representation with no execution logic
# --------------------------------------------------------------------------- #

class Condition08TheDemoViewOnlyDraws(unittest.TestCase):

    def setUp(self) -> None:
        self.path = (REPOSITORY_ROOT / "gui" / "operator" / "workspaces"
                     / "demo.py")
        self.tree = ast.parse(self.path.read_text(encoding="utf-8"),
                              filename=str(self.path))

    def test_the_demo_pane_imports_no_source_store_or_coordinator(self) -> None:
        imported: List[str] = []
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        for banned in ("sources", "store", "coordinator", "assurance",
                       "executor", "threading", "subprocess"):
            with self.subTest(module=banned):
                self.assertFalse(
                    any(banned in name for name in imported),
                    f"the Demo View imports {banned}: {imported}")

    def test_the_demo_snapshot_is_a_pure_function_of_the_session_state(
            self) -> None:
        state = SessionState(mode="REPLAY", run_id="run-1",
                             disposition="COMPLETED")
        first = dict(demo_pane.demo_snapshot(state))
        second = dict(demo_pane.demo_snapshot(state))
        self.assertEqual(first, second)
        self.assertEqual(first["mode_badge"], "REPLAY")

    def test_the_mode_badge_cannot_be_hidden(self) -> None:
        for mode in ("LIVE", "REPLAY", "DISCONNECTED"):
            with self.subTest(mode=mode):
                snapshot = demo_pane.demo_snapshot(SessionState(mode=mode))
                self.assertFalse(snapshot["mode_badge_can_hide"])
                self.assertEqual(snapshot["mode_badge"], mode)

    def test_the_demo_pane_has_no_action_callback(self) -> None:
        workspace = demo_pane.DemoWorkspace()
        self.assertFalse(hasattr(workspace, "on_action"))


# --------------------------------------------------------------------------- #
# 9. the GUI cannot modify a verdict, an evidence closure, a harm charge or a
#    rollback result  (FAIL-CLOSED)
# --------------------------------------------------------------------------- #

class Condition09TheConsoleCannotOverrideTheKernel(unittest.TestCase):

    def test_no_cockpit_module_writes_to_a_kernel_decision(self) -> None:
        """No assignment to an outcome/verdict/closure/charge attribute.

        Over the whole Cockpit package.  ``TheGuardsCoverEveryCockpitSource``
        below proves this reaches every file, and proves the detector fires on
        an injected write -- the two halves together are what "the console
        cannot override the Kernel" means.
        """
        offenders = kernel_decision_write_offenders(COCKPIT_SOURCES)
        self.assertEqual(offenders, [],
                         f"a Cockpit module assigns a Kernel decision: "
                         f"{offenders}")

    def test_every_view_model_in_the_cockpit_is_frozen(self) -> None:
        """Every dataclass the Cockpit defines, not only ``cockpit.py``'s.

        The narrower version of this test walked one module, so a mutable
        projection added to ``batch.py`` or ``data_class.py`` -- both of which
        carry Kernel-backed views -- would have gone unguarded.  It now walks
        the whole package, and the four dataclasses that are legitimately
        mutable are named in :data:`MUTABLE_BY_DESIGN` with the reason each is
        console state rather than a Kernel decision.
        """
        import dataclasses
        import importlib

        checked = 0
        for dotted in cockpit_modules():
            module = importlib.import_module(dotted)
            for name in dir(module):
                obj = getattr(module, name)
                if not (isinstance(obj, type) and dataclasses.is_dataclass(obj)
                        and obj.__module__ == dotted):
                    continue
                checked += 1
                qualified = f"{dotted}.{name}"
                if qualified in MUTABLE_BY_DESIGN:
                    continue
                with self.subTest(view=qualified):
                    self.assertTrue(
                        obj.__dataclass_params__.frozen,
                        f"{qualified} is a mutable view model; a projection "
                        f"the render layer can write back to is a path from "
                        f"the screen into what the screen claims")
        self.assertGreater(checked, 40,
                           "the dataclass sweep found almost nothing, so it "
                           "is not sweeping what it claims to")

    def test_the_two_read_only_panes_build_no_input_widget(self) -> None:
        control_widgets = {"Button", "Entry", "Checkbutton", "Radiobutton",
                           "Scale", "Spinbox", "OptionMenu", "Listbox",
                           "Combobox", "Treeview"}
        for path in (REPOSITORY_ROOT / "gui" / "operator" / "workspaces"
                     / name for name in ("trial_safety.py",
                                         "evidence_ledger.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"),
                             filename=str(path))
            built: Set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = (getattr(node.func, "attr", None)
                            or getattr(node.func, "id", None))
                    if name in control_widgets:
                        built.add(name)
            with self.subTest(pane=path.name):
                self.assertEqual(built, set())

    def test_the_batch_pane_edits_a_plan_and_never_a_result(self) -> None:
        """The one pane with a form: its fields are plan scope, nothing else."""
        self.assertEqual(set(batch_source.PLAN_FIELD_KEYS),
                         {key for key, _l, _h in batch_source.PLAN_FIELDS})
        for banned in ("outcome", "verdict", "closure", "harm_charge",
                       "rollback", "evidence"):
            with self.subTest(field=banned):
                self.assertNotIn(banned, batch_source.PLAN_FIELD_KEYS)

    def test_the_console_has_no_action_that_sets_a_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            console = OperatorConsole(runs_root=tmp)
            try:
                for key in console.action_keys():
                    with self.subTest(action=key):
                        for banned in ("verdict", "outcome", "closure",
                                       "charge", "override", "rollback"):
                            self.assertNotIn(banned, key)
            finally:
                console.shutdown()

    def test_restoring_a_saved_gui_state_cannot_inject_a_verdict(self) -> None:
        for workspace in (ts.TrialSafetyWorkspace(),
                          el.EvidenceLedgerWorkspace()):
            with self.subTest(pane=workspace.id):
                before = workspace.lines()
                workspace.restore_gui_state({"outcome": "SUCCESS",
                                             "harmCharge": 0.0,
                                             "status": "CLOSED_PASS"})
                self.assertEqual(workspace.lines(), before)

    def test_the_removed_authority_vocabulary_stays_removed(self) -> None:
        """Task section 5.3, over the whole Cockpit package."""
        self.assertEqual(authority_vocabulary_offenders(COCKPIT_SOURCES), [])

    def test_the_confirmation_row_cannot_carry_an_identity(self) -> None:
        import dataclasses

        fields = {f.name.lower()
                  for f in dataclasses.fields(cockpit.ConfirmationRow)}
        self.assertEqual(fields & FORBIDDEN_IDENTIFIERS, set())


# --------------------------------------------------------------------------- #
# 10. Emergency Stop is the whole chain
# --------------------------------------------------------------------------- #

class Condition10EmergencyStopRunsTheChain(unittest.TestCase):

    def test_the_header_control_routes_to_the_kernel_stop(self) -> None:
        from gui.operator.shell.cockpit_header import EMERGENCY_STOP_ACTION

        self.assertEqual(EMERGENCY_STOP_ACTION, "kernel_estop")
        with tempfile.TemporaryDirectory() as tmp:
            console = OperatorConsole(runs_root=tmp)
            try:
                self.assertIn(EMERGENCY_STOP_ACTION, console.action_keys())
            finally:
                console.shutdown()

    def test_the_stop_reaches_operator_abort_rollback_and_recovery(
            self) -> None:
        fixture = KernelSubmissionFixture()
        session = fixture.session(
            publish=lambda _channel, view:
                (session.request_emergency_stop()
                 if view.stage == kl.STAGE_OBSERVING and view.poll_count == 2
                 else None))
        session.draft(PIN_UTTERANCE)
        session.confirm()
        session.start()
        snapshot = cockpit.project_session(session)
        trial = snapshot.trial_safety.current_trial
        self.assertEqual(trial.outcome, TrialOutcome.OPERATOR_ABORTED.value)
        text = "\n".join(ts.rollback_lines(snapshot.trial_safety))
        for step in ("OPERATOR_ABORT", "Write Gateway stop", "rollback",
                     "recovery"):
            with self.subTest(step=step):
                self.assertIn(step, text)

    def test_the_header_names_the_chain_before_it_is_pressed(self) -> None:
        view = cockpit.CockpitHeaderView(mode="MOCK", case_id="case/x",
                                         trial_id="case/x:trial:1",
                                         kernel_state="OBSERVING")
        item = {i.key: i for i in cockpit.header_items(view)}["emergency_stop"]
        self.assertIn("OPERATOR_ABORT", item.detail or "")


# --------------------------------------------------------------------------- #
# 11. a value is exact when it exists, and Unsupported/Unknown with a reason
#     when it does not
# --------------------------------------------------------------------------- #

class Condition11ExactOrStated(unittest.TestCase):

    def test_a_present_value_is_shown_exactly(self) -> None:
        _fixture, session = settled_session()
        snapshot = cockpit.project_session(session)
        balance = snapshot.header.harm[0]
        self.assertEqual(balance.usable, 100.0)
        self.assertEqual(balance.charged, 0.0)
        self.assertIn("reserve 100", balance.as_text())

    def test_an_absent_value_never_becomes_a_plausible_default(self) -> None:
        view = cockpit.CockpitHeaderView()
        for item in cockpit.header_items(view):
            if item.key in ("mode", "emergency_stop"):
                continue
            with self.subTest(item=item.key):
                self.assertEqual(item.value, st.PRE_MEASUREMENT)
                self.assertTrue(item.detail, f"{item.key} is bare")

    def test_every_unknown_or_unsupported_badge_states_a_reason(self) -> None:
        snapshot = cockpit.project({}, case_id=None, mode="DISCONNECTED")
        for item in cockpit.header_items(snapshot.header):
            with self.subTest(item=item.key):
                if item.badge.data_class in dc.REASON_REQUIRED:
                    self.assertTrue(item.badge.states_a_reason,
                                    f"{item.key} gives no reason")
        for step in snapshot.chain:
            with self.subTest(step=step.key):
                self.assertTrue(step.reason)

    def test_a_zero_and_an_absence_are_not_the_same_rendering(self) -> None:
        zero = cockpit.HarmBalanceView("harm/x", "ms", usable=0.0, charged=0.0)
        absent = cockpit.HarmBalanceView("harm/x", "ms", usable=None)
        self.assertIn("reserve 0", zero.as_text())
        self.assertIn(st.PRE_MEASUREMENT, absent.as_text())
        self.assertNotEqual(zero.as_text(), absent.as_text())

    def test_a_case_with_no_cells_reports_unknown_progress_not_complete(
            self) -> None:
        view = cockpit.EvidenceLedgerView(mode="MOCK", case_id="case/x")
        self.assertIsNone(view.closure_progress)


# --------------------------------------------------------------------------- #
# 12. Replay is shown for what it supports, and an empty session is not one
# --------------------------------------------------------------------------- #

class Condition12ReplayIsNotFabricated(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.console = OperatorConsole(runs_root=self._tmp.name)
        self.addCleanup(self.console.shutdown)

    def test_a_fresh_console_is_disconnected_not_replaying(self) -> None:
        self.assertEqual(self.console.state.mode, "DISCONNECTED")
        self.assertIsNone(self.console.replay_source)

    def test_selecting_replay_without_a_recorded_source_is_refused(
            self) -> None:
        with self.assertRaises(SessionError) as caught:
            self.console.select_mode("REPLAY")
        self.assertIn("recorded source", str(caught.exception))

    def test_an_empty_kernel_projection_is_not_a_successful_replay(
            self) -> None:
        view = cockpit.evidence_ledger_view({}, (), mode="REPLAY")
        self.assertEqual(view.event_count, 0)
        self.assertEqual(view.unavailable_reason, cockpit.NO_EVENT_STREAM)
        text = "\n".join(el.stream_lines(view))
        self.assertNotIn("0 event(s) in the store", text)
        self.assertIn("Unknown", text)

    def test_a_replay_mode_projection_is_never_badged_live(self) -> None:
        view = cockpit.CockpitHeaderView(mode="REPLAY", case_id="case/x",
                                         trial_id="case/x:trial:1",
                                         kernel_state="OBSERVING")
        for item in cockpit.header_items(view):
            with self.subTest(item=item.key):
                self.assertFalse(item.badge.is_live)

    def test_a_batch_run_records_the_mode_it_actually_ran_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = KernelSubmissionFixture()
            session = fixture.session()
            batch = batch_source.BatchSession(
                runs_root=tmp, session_factory=lambda case: session,
                mode="MOCK",
                draft=batch_source.PlanDraft(
                    objectives="UeCellSteeringPinToCell",
                    profiles=f"pin={PIN_UTTERANCE}", strategy="deterministic",
                    repeats="1", seed="1", budget_cases="4"))
            batch.confirm(now="2026-08-21T09:00:00.000000Z")
            view = batch.start()
            manifest = json.loads(
                (Path(view.run_dir) / "manifest.json").read_text(
                    encoding="utf-8"))
            self.assertEqual(manifest["mode"], "EMULATED")
            self.assertNotEqual(manifest["mode"], "LIVE")


# --------------------------------------------------------------------------- #
# 13. nothing unsupported is advertised as supported  (FAIL-CLOSED)
# --------------------------------------------------------------------------- #

class Condition13NothingUnsupportedIsAdvertised(unittest.TestCase):

    def test_a_mock_run_is_never_badged_live(self) -> None:
        _fixture, session = settled_session()
        snapshot = cockpit.project_session(session)
        self.assertEqual(snapshot.mode, kl.MODE_MOCK)
        self.assertFalse(snapshot.header.is_live)
        for item in cockpit.header_items(snapshot.header):
            with self.subTest(item=item.key):
                self.assertFalse(item.badge.is_live)

    def test_the_registry_never_advertises_an_unsupported_objective(
            self) -> None:
        from gui.operator.sources.objective_registry import project_registry

        for row in project_registry().rows:
            with self.subTest(family=row.family):
                if not row.evidence_is_ota:
                    self.assertFalse(
                        row.reads_as_success,
                        f"{row.family} reads as a completed objective without "
                        f"OTA evidence")

    def test_a_batch_summary_reports_no_kpi_it_did_not_measure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = KernelSubmissionFixture()
            session = fixture.session()
            batch = batch_source.BatchSession(
                runs_root=tmp, session_factory=lambda case: session,
                mode="MOCK",
                draft=batch_source.PlanDraft(
                    objectives="UeCellSteeringPinToCell",
                    profiles=f"pin={PIN_UTTERANCE}", strategy="deterministic",
                    repeats="1", seed="1", budget_cases="4"))
            batch.confirm(now="2026-08-21T09:00:00.000000Z")
            view = batch.start()
            for name in ("KPM", "O1", "Core", "RAN", "UEApplication"):
                with self.subTest(kpi=name):
                    self.assertIsNone((view.summary["kpis"]).get(name))

    def test_an_unsupported_artifact_states_why_rather_than_failing_quietly(
            self) -> None:
        rows = {row.key: row for row in batch_source.artifact_rows(None)}
        self.assertEqual(len(rows), len(batch_source.ARTIFACTS))
        for key, row in rows.items():
            with self.subTest(artifact=key):
                self.assertEqual(row.status, st.UNKNOWN)
                self.assertIn("no batch run has completed", row.reason or "")

    def test_a_pending_evidence_update_is_not_drawn_as_a_closure(self) -> None:
        cell = cockpit.EvidenceCellRow(
            cell_id="cell/x", status=EvidenceCellStatus.PARTIAL.value,
            pending_contributions=1,
            pending_status=EvidenceCellStatus.CLOSED_PASS.value,
            gui_status=st.DEGRADED)
        self.assertFalse(cell.is_closed)
        self.assertTrue(cell.blocks_exhaustion)
        text = "\n".join(el.closure_lines(
            cockpit.EvidenceLedgerView(mode="MOCK", cells=(cell,))))
        self.assertIn("not yet", text)
        self.assertIn("0/1 cells closed", text)

    def test_a_sealed_dormant_cell_is_not_reported_as_evidence(self) -> None:
        cell = cockpit.EvidenceCellRow(
            cell_id="cell/x",
            status=EvidenceCellStatus.DORMANT_SEALED.value,
            sealed=True, sealed_until_vector_ref="vector/next",
            gui_status=st.NOT_APPLICABLE,
            reason="sealed until its target vector is released")
        text = "\n".join(el.closure_lines(
            cockpit.EvidenceLedgerView(mode="MOCK", cells=(cell,))))
        self.assertIn("sealed until vector/next", text)
        self.assertFalse(cell.is_closed)

    def test_a_post_closure_witness_is_counted_separately(self) -> None:
        """Task section 5.16: a witness does not improve a closed fail."""
        cell = cockpit.EvidenceCellRow(
            cell_id="cell/x", status=EvidenceCellStatus.CLOSED_FAIL.value,
            contributions=2, post_closure_witnesses=1, gui_status=st.ERROR)
        text = "\n".join(el.closure_lines(
            cockpit.EvidenceLedgerView(mode="MOCK", cells=(cell,))))
        self.assertIn("post-closure witnesses 1", text)
        self.assertIn("CLOSED_FAIL", text)
        self.assertIn("1/1 cells closed", text)


# --------------------------------------------------------------------------- #
# The boundary the whole Cockpit sits behind
# --------------------------------------------------------------------------- #

class TheCockpitDoesNotReachAroundTheKernel(unittest.TestCase):
    """Task section 7.3: no direct E2/xApp/PRB/SSH/process/USRP path."""

    def test_no_cockpit_module_imports_a_transport_or_an_actuator(self) -> None:
        self.assertEqual(transport_import_offenders(COCKPIT_SOURCES), [])

    def test_assurance_still_never_imports_the_gui(self) -> None:
        offenders: List[str] = []
        for path in sorted((REPOSITORY_ROOT / "assurance").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"),
                             filename=str(path))
            for node in ast.walk(tree):
                names: List[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    names = [node.module or ""]
                for name in names:
                    if name == "gui" or name.startswith("gui."):
                        offenders.append(
                            f"{path.relative_to(REPOSITORY_ROOT)}:{name}")
        self.assertEqual(offenders, [])

    def test_the_eight_workspaces_all_load_from_the_registry(self) -> None:
        modules = {entry[0] for entry in WORKSPACE_MODULES}
        self.assertTrue({workspace_id
                         for workspace_id, _title in COCKPIT_WORKSPACES}
                        <= modules)


# --------------------------------------------------------------------------- #
# The guards themselves: do they reach every Cockpit source, and do they fire?
# --------------------------------------------------------------------------- #

class TheGuardsCoverEveryCockpitSource(unittest.TestCase):
    """A fail-closed scan is worth exactly the files it opens.

    An independent review found that it opened seven.  ``COCKPIT_SOURCES`` was
    a hand-written list, so ``workspaces/contract_studio.py`` -- one of section
    9's own eight workspaces -- was never scanned, and an override write
    injected into it passed the whole suite.  Two properties close that, and
    both are needed:

    * **coverage** -- the collected set is derived from the tree, is not
      empty, and demonstrably contains the files that were missing;
    * **detection** -- each scanner, handed a mutated copy of a real Cockpit
      source, reports the injection.

    Either alone is satisfiable by a guard that does nothing: a scanner that
    opens every file and detects nothing, or a perfect detector pointed at an
    empty list.  The mutations are written to a temporary directory; no file
    in the repository is modified by this test.
    """

    def mutated(self, source: Path, injection: str) -> Path:
        """A temporary copy of ``source`` with ``injection`` appended."""
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        target = Path(directory) / source.name
        target.write_text(source.read_text(encoding="utf-8") + injection,
                          encoding="utf-8")
        return target

    # -- coverage -----------------------------------------------------------

    def test_the_collector_finds_the_cockpit_package(self) -> None:
        self.assertTrue(COCKPIT_SOURCES, "the collector found no source at "
                                         "all, so every scan passes vacuously")
        self.assertGreaterEqual(len(COCKPIT_SOURCES),
                                MINIMUM_COCKPIT_SOURCES)

    def test_the_files_the_hand_written_list_missed_are_covered(self) -> None:
        covered = {relative(path) for path in COCKPIT_SOURCES}
        for name in MUST_BE_COVERED:
            with self.subTest(source=name):
                self.assertIn(name, covered)

    def test_every_registered_workspace_module_is_covered(self) -> None:
        """A new workspace joins the scans by existing, not by being listed."""
        covered = {relative(path) for path in COCKPIT_SOURCES}
        for _id, module_name, _class_name, _title in WORKSPACE_MODULES:
            with self.subTest(module=module_name):
                self.assertIn(module_name.replace(".", "/") + ".py", covered)

    def test_every_cockpit_module_is_importable_under_its_dotted_name(
            self) -> None:
        import importlib

        names = cockpit_modules()
        self.assertEqual(len(names), len(COCKPIT_SOURCES))
        for dotted in names:
            with self.subTest(module=dotted):
                importlib.import_module(dotted)

    # -- detection ----------------------------------------------------------

    def test_an_injected_override_write_is_caught_in_a_previously_missed_file(
            self) -> None:
        """The reviewer's exploit, replayed against the widened guard."""
        injection = (
            "\n\ndef operator_correction(view, settlement):\n"
            "    view.outcome = 'SUCCESS'\n"
            "    settlement.harm_charge = 0.0\n"
            "    view.status = 'CLOSED_PASS'\n")
        for name in ("gui/operator/workspaces/contract_studio.py",
                     "gui/operator/app.py",
                     "gui/operator/sources/kernel_live.py"):
            with self.subTest(source=name):
                copy = self.mutated(REPOSITORY_ROOT / name, injection)
                offenders = kernel_decision_write_offenders((copy,))
                self.assertTrue(offenders,
                                f"an override write injected into {name} was "
                                f"not detected")
                self.assertTrue(any("outcome" in item for item in offenders))
                self.assertTrue(any("harm_charge" in item
                                    for item in offenders))

    def test_an_injected_authority_identifier_is_caught(self) -> None:
        injection = "\n\ndef approve(signer):\n    return signer\n"
        copy = self.mutated(
            REPOSITORY_ROOT / "gui/operator/workspaces/contract_studio.py",
            injection)
        offenders = authority_vocabulary_offenders((copy,))
        self.assertTrue(any("signer" in item for item in offenders), offenders)

    def test_an_injected_transport_import_is_caught(self) -> None:
        injection = "\nimport telnetlib\nimport subprocess\n"
        copy = self.mutated(REPOSITORY_ROOT / "gui/operator/app.py", injection)
        offenders = transport_import_offenders((copy,))
        self.assertTrue(any("telnetlib" in item for item in offenders),
                        offenders)
        self.assertTrue(any("subprocess" in item for item in offenders),
                        offenders)

    def test_the_legacy_exception_map_is_empty(self) -> None:
        """B-01: no Cockpit file may import the pre-Kernel decision runtime.

        The map is the record of that: an empty dict, and a failing test the
        moment somebody re-opens it for one more file.
        """
        self.assertEqual({}, dict(LEGACY_COORDINATOR_IMPORTS))

    def test_the_neutral_allowance_covers_no_decision_runtime(self) -> None:
        """The one remaining allowance is a projection, not a runtime.

        It must never grow to cover the episode entry, the adapter beneath it,
        the coordinator package or a transport.
        """
        for banned in ("coordinator", "coordinator.schema", "executor",
                       "executor.oai_executor", "oran.rapp.gui_entry",
                       "oran.rapp.coordinator_adapter", "oran.rapp.headless",
                       "gui.dashboard", "subprocess", "telnetlib"):
            with self.subTest(module=banned):
                self.assertNotIn(banned, NEUTRAL_READ_ONLY_IMPORTS)

    def test_the_legacy_runtime_is_banned_by_name(self) -> None:
        """Naming the modules explicitly, not only their packages.

        ``oran.rapp`` is allowed through the neutral allowance above, so the
        episode entry and the adapter under it have to be banned on their own
        names or the allowance would cover them.
        """
        for banned in ("oran.rapp.gui_entry", "oran.rapp.coordinator_adapter",
                       "executor.oai_executor", "gui.dashboard",
                       "tools.legacy", "coordinator"):
            with self.subTest(module=banned):
                self.assertIn(banned, BANNED_TRANSPORT_IMPORTS)

    def test_an_injected_legacy_import_is_caught(self) -> None:
        """Mutation: put the old import back and the scan must fail."""
        for injection in ("\nfrom oran.rapp.gui_entry import run_gui_once\n",
                          "\nfrom coordinator.schema import validate_intent_parse\n",
                          "\nfrom tools.legacy.episode_support import "
                          "legacy_episode_support\n"):
            with self.subTest(injection=injection.strip()):
                copy = self.mutated(REPOSITORY_ROOT / "gui/operator/app.py",
                                    injection)
                offenders = transport_import_offenders((copy,))
                self.assertTrue(offenders, injection)

    # -- the exceptions themselves ------------------------------------------

    def test_every_allowlisted_file_exists_and_is_collected(self) -> None:
        """A typo in an exception key would silently widen the hole."""
        covered = {relative(path) for path in COCKPIT_SOURCES}
        for table, label in ((LEGACY_COORDINATOR_IMPORTS, "import"),
                             (FROZEN_SEAM_VOCABULARY, "vocabulary")):
            for key in table:
                with self.subTest(exception=label, source=key):
                    self.assertIn(key, covered)

    def test_the_neutral_allowance_is_still_load_bearing(self) -> None:
        """Drop the allowance and the scan must fail; otherwise it is dead."""
        offenders = []
        for path in COCKPIT_SOURCES:
            tree = ast.parse(path.read_text(encoding="utf-8"),
                             filename=str(path))
            imported = {name for name, _line in _imported_modules(tree)}
            offenders += sorted(NEUTRAL_READ_ONLY_IMPORTS & imported)
        self.assertTrue(
            offenders,
            "no Cockpit source imports the neutrally-allowed modules any "
            "more; drop the allowance rather than leaving it open")

    def test_every_import_exception_is_still_load_bearing(self) -> None:
        """Remove an exception and the scan must fail; otherwise it is dead."""
        for key, allowed in LEGACY_COORDINATOR_IMPORTS.items():
            with self.subTest(source=key):
                path = REPOSITORY_ROOT / key
                tree = ast.parse(path.read_text(encoding="utf-8"),
                                 filename=str(path))
                imported = {name for name, _line in _imported_modules(tree)}
                self.assertTrue(
                    allowed & imported,
                    f"{key} no longer imports {sorted(allowed)}; drop the "
                    f"exception rather than leaving it open")

    def test_every_vocabulary_exception_is_still_load_bearing(self) -> None:
        for key, allowed in FROZEN_SEAM_VOCABULARY.items():
            with self.subTest(source=key):
                without = {name: value
                           for name, value in FROZEN_SEAM_VOCABULARY.items()
                           if name != key}
                self.assertTrue(
                    authority_vocabulary_offenders((REPOSITORY_ROOT / key,),
                                                   exceptions=without),
                    f"{key} no longer uses {sorted(allowed)}; drop the "
                    f"exception rather than leaving it open")

    def test_every_mutable_dataclass_exception_still_names_a_real_one(
            self) -> None:
        import dataclasses
        import importlib

        for qualified, reason in MUTABLE_BY_DESIGN.items():
            with self.subTest(view=qualified):
                self.assertTrue(reason.strip(),
                                "a mutable view model needs a stated reason")
                module_name, _dot, class_name = qualified.rpartition(".")
                obj = getattr(importlib.import_module(module_name), class_name)
                self.assertTrue(dataclasses.is_dataclass(obj))
                self.assertFalse(
                    obj.__dataclass_params__.frozen,
                    f"{qualified} is frozen now; drop the exception rather "
                    f"than leaving it open")

    def test_an_injected_mutable_view_would_be_caught(self) -> None:
        """The frozen sweep is a sweep, not a list of four modules."""
        import dataclasses

        @dataclasses.dataclass
        class MutableProjection:
            outcome: str = ""

        self.assertFalse(MutableProjection.__dataclass_params__.frozen)
        self.assertNotIn(
            f"{MutableProjection.__module__}.MutableProjection",
            MUTABLE_BY_DESIGN,
            "an unlisted mutable dataclass must not be silently permitted")


if __name__ == "__main__":                                 # pragma: no cover
    unittest.main()
