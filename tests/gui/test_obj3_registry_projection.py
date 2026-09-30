"""The registry, projected onto the operator's screen -- honestly and read-only.

Lane **OBJ3** (``docs/architecture/SEAMS-GATE4.md`` section 3).  Gate 4's third
and fourth acceptance items are what this file holds:

* "mock·Replay 결과가 OTA success로 표시되지 않음";
* "objective별 actual capability/evidence level이 registry와 GUI에 정직하게
  표시" (task section 13).

Four properties, and each has a way it could quietly fail:

**All five support states render.**  Only three appear in today's registry, so a
projection could be written that happens to handle those three and paints the
other two as nothing -- or, worse, as ``OK`` because the lookup missed.  The
tests below drive all five through the real projection using records built from
real ones, and the two states that are not in the registry today are built so
that ``validate_record`` still admits them: a state the validator would refuse
is not a state the console has to draw.

**Blocking reasons appear in full.**  The failure mode named in task section
9.11 is a console that says "unavailable" and stops.  An operator who cannot
tell a missing A1 policy type from a missing measurement cannot tell which of
the two would take a week and which would take a testbed.

**A hardware-free round trip never reads as an OTA success.**  Design section 15
and Gate 4.  The registry already decides this in ``evidenceIsOta``; what is
checked here is that the screen draws that decision -- different glyph,
different words, and never the success colour.

**No widget can change any of it.**  Task section 9.9 forbids a GUI path that
overrides a verdict; a support state is the same kind of fact one step further
back.  Asserted structurally over the workspace's syntax tree rather than by
clicking around and finding nothing, because "I could not find a control" and
"there is no control" are different claims.

Hermetic.  The pure projection runs anywhere; the one class that builds real Tk
widgets is skipped without a display.
"""

from __future__ import annotations

import ast
import dataclasses
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

from assurance.objectives.registry import (
    OBJECTIVE_REGISTRY,
    PROJECT_IDENTIFIER_NOTICE,
    EvidenceLevel,
    SupportState,
    record_for,
    registry_view,
    validate_registry,
)
from gui.operator import status as st
from gui.operator.sources.objective_registry import (
    EVIDENCE_STATUS,
    SUPPORT_LABELS,
    SUPPORT_STATUS,
    ObjectiveRegistryView,
    detail_lines,
    pane_lines,
    project_registry,
    summary_text,
)
from gui.operator.workspaces import WORKSPACE_MODULES, PlaceholderWorkspace
from gui.operator.workspaces.objective_registry import (
    AXIS_NOTE,
    ObjectiveRegistryWorkspace,
)
from gui.operator.viewmodel.types import SessionState

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_SOURCE = (REPOSITORY_ROOT / "gui" / "operator" / "workspaces"
                    / "objective_registry.py")
HAS_DISPLAY = bool(os.environ.get("DISPLAY"))

#: Widget classes that take a value or an action from the operator.  A pane that
#: builds one of these is a pane that can be typed into or pressed, whatever it
#: does with the result.
CONTROL_WIDGETS = {
    "Button", "Checkbutton", "Radiobutton", "Entry", "Spinbox", "Scale",
    "Listbox", "OptionMenu", "Menu", "Menubutton", "Combobox", "Treeview",
}

#: Keyword arguments that attach behaviour to a widget.
CONTROL_KWARGS = {"command", "variable", "textvariable", "validatecommand"}


#: One support state per family, chosen so the set covers all five of design
#: section 16 *and* still passes ``validate_registry`` as a whole: a composite
#: never sits above its weakest component, and no state is claimed over an unmet
#: blocking premise.  Written out per family rather than derived from whatever
#: the live registry happens to hold, because the live registry moves as lanes
#: land and a fixture that follows it stops covering the states it was written
#: to cover.
SYNTHETIC_STATES = {
    # demoted: the honest state for a family whose matrix has not run
    "TrafficSteeringPreference": (SupportState.IMPLEMENTATION_IN_PROGRESS,
                                  EvidenceLevel.NONE, ()),
    # composite of QoSTarget and TrafficSteeringPreference: never above the
    # weakest of the two, which the demotion above makes "in progress"
    "QoSandTSP": (SupportState.IMPLEMENTATION_IN_PROGRESS,
                  EvidenceLevel.NONE, ()),
    # QoEandTSP is also a composite over TrafficSteeringPreference.  Keep it
    # at the weakest component's synthetic state so the fixture remains a
    # registry-valid distribution after the live QoE record was promoted.
    "QoEandTSP": (SupportState.IMPLEMENTATION_IN_PROGRESS,
                  EvidenceLevel.NONE, ()),
    "QoSTarget": (SupportState.HARDWARE_FREE_VERIFIED,
                  EvidenceLevel.HARDWARE_FREE_ROUND_TRIP,
                  ("positive", "negative", "malformed", "conflict", "stale",
                   "missing", "timeout", "partial-effect", "duplicate", "fault")),
    "UELevelTarget": (SupportState.OTA_VERIFICATION_PENDING,
                      EvidenceLevel.HARDWARE_FREE_ROUND_TRIP,
                      ("positive", "negative", "fault")),
    # SliceSLATarget retains real blocking reasons, so it is the honest record
    # to exercise the unsupported renderer without manufacturing a reason.
    "SliceSLATarget": (SupportState.UNSUPPORTED_BY_CURRENT_DEPLOYMENT,
                       EvidenceLevel.CONTRACT_DECLARED, ()),
    # The remaining families and the OTA regression contract are left exactly
    # as the registry holds them.
}


def _synthetic_view() -> ObjectiveRegistryView:
    """The registry with all five support states present at once.

    The live registry holds three of the five at any one time, so a projection
    could handle those three and paint the other two as nothing -- or, worse, as
    ``OK`` because the lookup missed.  This fixture puts one record in each of
    the five and hands the set to the same projection the console uses.

    Every record goes through ``validate_registry`` first, as a *set*: the
    console is asked to draw states the registry can really hold, not states
    invented to make the renderer look complete.
    """
    records = []
    for record in OBJECTIVE_REGISTRY:
        change = SYNTHETIC_STATES.get(record.family)
        if change is None:
            records.append(record)
            continue
        support, evidence, scenarios = change
        records.append(dataclasses.replace(
            record, support_state=support, evidence_level=evidence,
            hardware_free_scenarios=scenarios,
            # A demoted record cannot keep OTA evidence references, and the
            # registry refuses that pairing for a good reason: references *are*
            # OTA raw evidence, so a family whose matrix has not run has none
            # to name.  Two of the families demoted here really do carry
            # references since Gate 5 stage 2 drove them over real radio, so
            # the demotion has to drop them rather than inherit them.
            evidence_refs=(record.evidence_refs
                           if evidence is EvidenceLevel.OTA_RAW_EVIDENCE
                           else ())))
    validate_registry(records)
    view: Dict[str, Any] = dict(registry_view())
    view["records"] = [record.as_dict() for record in records]
    return project_registry(view)


class TheProjectionReadsTheRegistryAndDecidesNothing(unittest.TestCase):
    """Every value on screen is the registry's, unchanged."""

    def setUp(self) -> None:
        self.view = project_registry()

    def test_every_record_is_projected_once(self) -> None:
        self.assertEqual([row.family for row in self.view.rows],
                         [record.family for record in OBJECTIVE_REGISTRY])

    def test_the_three_axes_are_carried_verbatim(self) -> None:
        for record in OBJECTIVE_REGISTRY:
            with self.subTest(family=record.family):
                row = self.view.row(record.family)
                self.assertEqual(row.support_state, record.support_state.value)
                self.assertEqual(row.evidence_level, record.evidence_level.value)
                self.assertEqual(row.submittable,
                                 record.deployment_capability.submittable)
                self.assertEqual(
                    row.blocking_reasons,
                    record.deployment_capability.blocking_reasons)

    def test_the_identifier_notice_travels_with_the_projection(self) -> None:
        """Task section 7.7: these are project names, and the screen says so."""
        self.assertEqual(self.view.identifier_notice, PROJECT_IDENTIFIER_NOTICE)
        self.assertIn("project contract identifiers",
                      self.view.identifier_notice)

    def test_nothing_reads_as_success_unless_the_registry_says_ota(self) -> None:
        for row in self.view.rows:
            with self.subTest(family=row.family):
                if not row.evidence_is_ota:
                    self.assertFalse(row.reads_as_success)

    def test_an_unmapped_state_resolves_to_unknown_not_to_ok(self) -> None:
        """Fail closed, the rule ``gui/operator/status.py`` states first.

        A support state this console has not been taught -- a sixth one added
        later, a typo in a record -- must not land on ``OK`` by falling through
        a lookup.
        """
        view = dict(registry_view())
        record = dict(view["records"][0])
        record["supportState"] = "MOSTLY_FINE"
        record["evidenceLevel"] = "PRETTY_GOOD"
        view["records"] = [record]
        row = project_registry(view).rows[0]
        self.assertEqual(row.support.status, st.UNKNOWN)
        self.assertEqual(row.evidence.status, st.UNKNOWN)
        self.assertFalse(row.reads_as_success)
        self.assertEqual(row.support_label, st.UNMAPPED_REASON)


class AllFiveSupportStatesRender(unittest.TestCase):
    """Design section 16 lists five.  The pane draws five."""

    def setUp(self) -> None:
        self.view = _synthetic_view()
        self.pane = ObjectiveRegistryWorkspace(view=self.view).lines()

    def test_the_projection_knows_exactly_the_five_states(self) -> None:
        self.assertEqual(set(SUPPORT_STATUS), {state.value for state in SupportState})
        self.assertEqual(set(SUPPORT_LABELS), {state.value for state in SupportState})
        self.assertEqual(set(EVIDENCE_STATUS), {level.value for level in EvidenceLevel})

    def test_each_of_the_five_appears_on_the_pane(self) -> None:
        text = "\n".join(self.pane["summary"] + self.pane["detail"])
        for state in SupportState:
            with self.subTest(state=state.value):
                self.assertIn(state.value, text)

    def test_each_state_is_drawn_with_a_glyph_and_a_sentence(self) -> None:
        """Never colour alone, and never a bare enum either.

        The status vocabulary gives each status a distinct glyph so the screen
        survives greyscale; the label is what makes the state mean something to
        an operator who has not read design section 16.
        """
        seen = set()
        for row in self.view.rows:
            seen.add(row.support_state)
            with self.subTest(family=row.family):
                self.assertTrue(row.support.glyph.strip())
                self.assertTrue(row.support_label.strip())
                self.assertNotEqual(row.support_label, st.UNMAPPED_REASON)
                line = f"{row.support.glyph} {row.support_state}"
                self.assertIn(line, "\n".join(detail_lines(row)))
        self.assertEqual(seen, {state.value for state in SupportState})

    def test_only_the_ota_verified_state_is_drawn_as_success(self) -> None:
        for row in self.view.rows:
            with self.subTest(family=row.family, state=row.support_state):
                if row.support_state == SupportState.OTA_LIVE_VERIFIED.value:
                    self.assertTrue(row.support.is_ok)
                else:
                    self.assertFalse(row.support.is_ok)

    def test_the_two_axes_are_two_columns_and_can_disagree(self) -> None:
        """``QoSTarget`` is the case that makes the separation necessary.

        Hardware-free verified and submittable through the current steering
        mapping, but not OTA verified. One collapsed column would have to hide
        the evidence boundary or imply that a dry-run was a live result.
        """
        row = self.view.row("QoSTarget")
        self.assertEqual(row.support_state,
                         SupportState.HARDWARE_FREE_VERIFIED.value)
        self.assertEqual(row.evidence_level,
                         EvidenceLevel.HARDWARE_FREE_ROUND_TRIP.value)
        self.assertTrue(row.submittable)
        self.assertFalse(row.evidence_is_ota)
        # The whole matrix passed and a drive plan exists, but no QoS OTA run
        # did: one collapsed column would have to hide one of those facts.
        self.assertEqual(len(row.hardware_free_scenarios), 10)
        self.assertFalse(row.blocking_reasons)
        header = summary_text(self.view)[0]
        self.assertIn("Support state", header)
        self.assertIn("Evidence level", header)
        self.assertIn("Submit", header)
        detail = "\n".join(detail_lines(row))
        self.assertIn("support state", detail)
        self.assertIn("evidence level", detail)
        self.assertIn("two axes", detail)


class NoHardwareFreeResultLooksLikeAnOtaSuccess(unittest.TestCase):
    """Gate 4 acceptance item 3, drawn rather than promised."""

    def setUp(self) -> None:
        self.view = _synthetic_view()

    def test_a_hardware_free_round_trip_is_not_ok_and_says_it_is_not_ota(self) -> None:
        rows = [row for row in self.view.rows
                if row.evidence_level == EvidenceLevel.HARDWARE_FREE_ROUND_TRIP.value]
        self.assertTrue(rows, "the fixture must contain a hardware-free row")
        for row in rows:
            with self.subTest(family=row.family):
                self.assertFalse(row.evidence.is_ok)
                self.assertFalse(row.evidence_is_ota)
                self.assertFalse(row.reads_as_success)
                self.assertIn("not OTA evidence", row.evidence_label)
                self.assertIn("OTA evidence      no",
                              "\n".join(detail_lines(row)))

    def test_the_ota_row_is_the_only_one_that_is_ok_on_both_axes(self) -> None:
        successes = [row.family for row in self.view.rows if row.reads_as_success]
        self.assertEqual(successes, ["UeCellSteeringPinToCell"])
        row = self.view.row("UeCellSteeringPinToCell")
        self.assertTrue(row.evidence_is_ota)
        self.assertTrue(row.evidence_refs, "an OTA row shows its retained evidence")

    def test_the_two_evidence_levels_are_distinguishable_on_the_pane(self) -> None:
        """Distinguishable without colour: different glyph, different words."""
        hardware_free = next(
            row for row in self.view.rows
            if row.evidence_level == EvidenceLevel.HARDWARE_FREE_ROUND_TRIP.value)
        ota = self.view.row("UeCellSteeringPinToCell")
        self.assertNotEqual(hardware_free.evidence.glyph, ota.evidence.glyph)
        self.assertNotEqual(hardware_free.evidence_label, ota.evidence_label)
        summary = "\n".join(summary_text(self.view))
        self.assertIn(f"{hardware_free.evidence.glyph} "
                      f"{EvidenceLevel.HARDWARE_FREE_ROUND_TRIP.value}", summary)
        self.assertIn(f"{ota.evidence.glyph} "
                      f"{EvidenceLevel.OTA_RAW_EVIDENCE.value}", summary)

    def test_a_passed_scenario_list_is_labelled_as_not_an_ota_result(self) -> None:
        row = self.view.row("QoSTarget")
        detail = "\n".join(detail_lines(row))
        self.assertIn("hardware-free scenarios passed", detail)
        self.assertIn("not an OTA result", detail)
        self.assertNotIn("retained OTA evidence", detail)


class BlockingReasonsAreShownInFull(unittest.TestCase):
    """Task section 9.11: state the reason, do not merely state that there is one."""

    def setUp(self) -> None:
        self.view = project_registry()
        self.pane = ObjectiveRegistryWorkspace(view=self.view).lines()

    def test_every_blocking_reason_of_every_record_appears_verbatim(self) -> None:
        text = "\n".join(self.pane["detail"])
        for record in OBJECTIVE_REGISTRY:
            for reason in record.deployment_capability.blocking_reasons:
                with self.subTest(family=record.family,
                                  reason=reason.split(":")[0]):
                    self.assertIn(reason, text)

    def test_the_non_submittable_family_shows_every_one_of_its_reasons(self) -> None:
        """Named literally, and read back from the registry as well.

        The literals are what an operator has to be able to read; the loop
        below is what stops the two drifting apart when the registry gains a
        reason -- which is exactly what happened when the SliceSLATarget
        actuator landed and ``A1_POLICY_FOR_STYLE2_QOS`` became the narrower
        ``A1_SLICE_TYPE_NOT_DEPLOYED`` plus ``NO_LIVE_SLICE_EFFECT_ORACLE``.
        """
        row = self.view.row("SliceSLATarget")
        detail = "\n".join(detail_lines(row))
        self.assertEqual(row.support_state,
                         SupportState.HARDWARE_FREE_VERIFIED.value)
        self.assertFalse(row.submittable)
        self.assertFalse(row.reads_as_success)
        for reason in ("NO_SLICE_SCOPED_MEASUREMENT", "NO_CORE_SLICE_EVIDENCE",
                       "A1_SLICE_TYPE_NOT_DEPLOYED",
                       "NO_LIVE_SLICE_EFFECT_ORACLE"):
            self.assertIn(reason, detail)
        for reason in record_for("SliceSLATarget").deployment_capability.blocking_reasons:
            with self.subTest(reason=reason.split(":")[0]):
                self.assertIn(reason, detail)

    def test_every_unmet_premise_shows_the_basis_it_was_read_from(self) -> None:
        text = "\n".join(self.pane["detail"])
        for record in OBJECTIVE_REGISTRY:
            for premise in record.unmet_premises():
                with self.subTest(family=record.family, kind=premise.kind.value):
                    self.assertIn(premise.statement, text)
                    self.assertIn(premise.basis, text)

    def test_a_mapped_but_undelivered_measurement_says_so(self) -> None:
        """``DRB.UEThpDl`` is mapped, published, and not delivered here."""
        row = self.view.row("QoSTarget")
        detail = "\n".join(detail_lines(row))
        self.assertIn("DRB.UEThpDl", detail)
        self.assertIn("NOT delivered by this deployment", detail)
        self.assertIn("RRU.PrbDl", detail)
        self.assertIn("delivered by this deployment", detail)

    def test_a_composite_shows_its_components_and_the_one_trial_rule(self) -> None:
        row = self.view.row("QoSandTSP")
        detail = "\n".join(detail_lines(row))
        self.assertTrue(row.joint_trial_required)
        for component in row.component_families:
            self.assertIn(component, detail)
        self.assertIn("ONE trial", detail)

    def test_the_pane_carries_the_identifier_notice_and_the_axis_note(self) -> None:
        self.assertIn("project contract identifiers", PROJECT_IDENTIFIER_NOTICE)
        self.assertIn("Only OTA raw evidence comes from the radio", AXIS_NOTE)

    def test_the_whole_pane_as_text_carries_both_regions(self) -> None:
        """``pane_lines`` is the headless reading of the same screen.

        One text, so an export or a screenshot description cannot say something
        the pane does not.
        """
        whole = "\n".join(pane_lines(self.view))
        self.assertIn(PROJECT_IDENTIFIER_NOTICE, whole)
        for region in ("summary", "detail"):
            for line in self.pane[region]:
                if line.strip() and not line.startswith("8 objectives"):
                    self.assertIn(line, whole)


class ThePaneHasNoPathBackIntoTheState(unittest.TestCase):
    """Read-only, asserted over the syntax tree rather than by not finding a button."""

    def setUp(self) -> None:
        self.tree = ast.parse(WORKSPACE_SOURCE.read_text(encoding="utf-8"),
                              filename=str(WORKSPACE_SOURCE))

    def test_the_workspace_builds_no_control_widget(self) -> None:
        built = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None) or getattr(
                    node.func, "id", None)
                if name in CONTROL_WIDGETS:
                    built.add(name)
        self.assertEqual(built, set(),
                         f"the registry pane builds control widgets: {built}")

    def test_no_widget_is_given_behaviour(self) -> None:
        attached = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg in CONTROL_KWARGS:
                        attached.add(keyword.arg)
                name = getattr(node.func, "attr", None)
                if name == "bind":
                    attached.add("bind")
        self.assertEqual(attached, set(),
                         f"the registry pane attaches behaviour: {attached}")

    def test_every_text_widget_is_disabled_after_writing(self) -> None:
        source = WORKSPACE_SOURCE.read_text(encoding="utf-8")
        self.assertEqual(source.count('state="normal"'),
                         source.count('state="disabled"') - 1,
                         "every enable is paired with a disable, plus the "
                         "disable that follows building")

    def test_the_workspace_offers_no_action_callback(self) -> None:
        workspace = ObjectiveRegistryWorkspace()
        self.assertFalse(hasattr(workspace, "on_action"))
        self.assertFalse(hasattr(workspace, "buttons"))
        self.assertEqual(dict(workspace.gui_state()), {})

    def test_restoring_gui_state_cannot_inject_anything(self) -> None:
        workspace = ObjectiveRegistryWorkspace()
        before = workspace.lines()
        workspace.restore_gui_state({"supportState": "OTA_LIVE_VERIFIED",
                                     "submittable": True})
        self.assertEqual(workspace.lines(), before)

    def test_the_session_state_does_not_change_what_the_pane_says(self) -> None:
        """A registry that moved with Live/Replay would be claiming something false."""
        workspace = ObjectiveRegistryWorkspace()
        before = workspace.lines()
        for mode in ("LIVE", "REPLAY", "DISCONNECTED"):
            with self.subTest(mode=mode):
                self.assertIsNone(workspace.on_state(SessionState(mode=mode)))
                self.assertEqual(workspace.lines(), before)

    def test_the_projection_object_is_frozen(self) -> None:
        row = project_registry().rows[0]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            row.submittable = True                     # type: ignore[misc]

    def test_assurance_never_imports_the_gui(self) -> None:
        """The Gate 2 boundary, in the one direction that matters here.

        ``gui`` reading ``assurance`` is the projection.  ``assurance`` reading
        ``gui`` would make the Kernel's own package depend on a console, and
        would open a path for a rendering decision to reach a contract.
        """
        offenders: List[str] = []
        for path in sorted((REPOSITORY_ROOT / "assurance").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    names = [node.module or ""]
                for name in names:
                    if name == "gui" or name.startswith("gui."):
                        offenders.append(
                            f"{path.relative_to(REPOSITORY_ROOT)}:{name}")
        self.assertEqual(offenders, [])


class TheOperatorCanReachIt(unittest.TestCase):
    """A pane only a test can build is not a pane the operator has."""

    def test_the_console_lists_the_workspace(self) -> None:
        declared = {entry[0]: entry for entry in WORKSPACE_MODULES}
        self.assertIn("objective_registry", declared)
        _id, module_name, class_name, title = declared["objective_registry"]
        self.assertEqual(module_name,
                         "gui.operator.workspaces.objective_registry")
        self.assertEqual(class_name, "ObjectiveRegistryWorkspace")
        self.assertEqual(title, "Objective Registry")

    def test_an_assembled_console_builds_the_real_pane(self) -> None:
        from gui.operator.app import OperatorConsole

        with tempfile.TemporaryDirectory() as tmp:
            console = OperatorConsole(runs_root=tmp)
            try:
                workspace = console.workspace("objective_registry")
                self.assertIsNotNone(workspace)
                self.assertNotIsInstance(workspace, PlaceholderWorkspace)
                self.assertIsInstance(workspace, ObjectiveRegistryWorkspace)
                self.assertEqual(
                    [row.family for row in workspace.view.rows],
                    [record.family for record in OBJECTIVE_REGISTRY])
            finally:
                console.shutdown()


@unittest.skipUnless(HAS_DISPLAY, "requires an X display")
class TheRenderedPane(unittest.TestCase):
    """The same assertions against real Tk widgets, not against the text function."""

    def setUp(self) -> None:
        import tkinter as tk

        self.root = tk.Tk()
        self.root.geometry("1600x1000+0+0")
        self.workspace = ObjectiveRegistryWorkspace(view=_synthetic_view())
        self.workspace.build(self.root)
        self.pump()

    def tearDown(self) -> None:
        try:
            self.workspace.destroy()
        finally:
            self.root.destroy()

    def pump(self, times: int = 3) -> None:
        for _ in range(times):
            self.root.update_idletasks()
            self.root.update()

    def widget_text(self, region: str) -> str:
        return self.workspace._texts[region].get("1.0", "end")

    def test_the_widgets_hold_what_the_projection_says(self) -> None:
        for region in ("summary", "detail"):
            with self.subTest(region=region):
                rendered = self.widget_text(region)
                for line in self.workspace.lines()[region]:
                    if line.strip():
                        self.assertIn(line, rendered)

    def test_every_text_widget_is_disabled(self) -> None:
        for region, widget in self.workspace._texts.items():
            with self.subTest(region=region):
                self.assertEqual(str(widget.cget("state")), "disabled")

    def test_the_widget_tree_contains_no_control(self) -> None:
        import tkinter as tk

        def walk(widget):
            yield widget
            for child in widget.winfo_children():
                yield from walk(child)

        for widget in walk(self.workspace.frame):
            with self.subTest(widget=str(widget)):
                self.assertNotIsInstance(widget, (tk.Button, tk.Entry,
                                                  tk.Checkbutton, tk.Listbox,
                                                  tk.Radiobutton, tk.Scale,
                                                  tk.Spinbox, tk.Menu))

    def test_all_five_states_and_the_reasons_are_on_screen(self) -> None:
        text = self.widget_text("summary") + self.widget_text("detail")
        for state in SupportState:
            self.assertIn(state.value, text)
        for reason in record_for("SliceSLATarget").deployment_capability.blocking_reasons:
            self.assertIn(reason, text)
        self.assertIn("not OTA evidence", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
