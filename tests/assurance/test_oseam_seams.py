"""The Gate 4 seam contract.

Ownership authority: ``docs/architecture/SEAMS-GATE4.md``.  This file pins what
the three objective lanes (OBJ1, OBJ2, OBJ3) are entitled to rely on while they
work in parallel:

* every family module exists, is owned by exactly one lane, and every frozen
  seat fails loudly instead of returning a placeholder;
* the lane a module declares is the lane the registry records, so a lane cannot
  discover mid-gate that it owns something else;
* the new package respects the Gate 2 boundary -- no transport, no deployment
  import, no model client;
* the ownership document lists every file, and the registry membership matches
  the seven families the task names.

Everything here runs without a display, without a network and without the
testbed.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
import unittest
from pathlib import Path
from typing import Mapping, Optional

from assurance.objectives import FAMILY_MODULES
from assurance.objectives.family import (
    VERDICT_SCENARIOS,
    ObjectiveContractBundle,
    ObjectiveFamilyModule,
    ScenarioName,
)
from assurance.objectives.registry import (
    OBJECTIVE_FAMILIES,
    OBJECTIVE_REGISTRY,
    PIN_REGRESSION_FAMILY,
    RegistryError,
    record_for,
)

from tests.assurance.objective_harness import ObjectiveMatrixMixin

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPOSITORY_ROOT / "assurance" / "objectives"
SEAMS_DOC = REPOSITORY_ROOT / "docs" / "architecture" / "SEAMS-GATE4.md"
GATE2_SEAMS_DOC = REPOSITORY_ROOT / "docs" / "architecture" / "SEAMS-GATE2.md"

#: The six seats of ``docs/architecture/SEAMS-GATE4.md`` section 2.
FROZEN_SEATS = (
    "contract_bundle",
    "candidate_parameters",
    "policy_lifecycle",
    "kpi_declaration",
    "terminal_oracle",
    "hardware_free_expectations",
)

#: Imports that would break the hardware-free guarantee or cross the O-RAN
#: boundary.  The same list Gate 2 applies to ``assurance/**``; repeated for
#: this package so the new subtree is checked by its own gate's test too.
FORBIDDEN_IMPORTS = {
    "tkinter", "matplotlib", "numpy", "scipy", "pandas",
    "subprocess", "socket", "telnetlib", "paramiko", "requests",
    "http.client", "urllib.request", "asyncio",
    "coordinator", "executor", "gui", "decision", "collectors",
    "experiments", "calibration", "diagnosis",
    "oran.rapp", "oran.nonrt", "oran.o1", "oran.integration", "oran.release",
}


def _source_files():
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def _imported_names(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level:
            names.append(node.module or "")
    return names


def _seat_is_unfilled(module, seat: str) -> bool:
    """Whether a seat still carries the seam's ``NotImplementedError`` body.

    Read off the source, and used for one thing only: deciding *which* refusal
    a seat owes.  It is not the test for whether a seat is real -- a lane can
    fill a seat with a body that raises through a helper, which no source
    substring can see.  :func:`_silent_stub_reason` is what checks that.
    """
    return "raise NotImplementedError" in inspect.getsource(getattr(module, seat))


def _seat_arguments(member) -> dict:
    """``None`` for every argument the seat requires, and nothing else."""
    arguments = {
        parameter.name: None
        for parameter in inspect.signature(member).parameters.values()
        if parameter.default is inspect.Parameter.empty
        and parameter.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    arguments.pop("self", None)
    return arguments


def _call_seat(module, seat: str):
    """Call one seat with ``None`` for every required argument.

    Returns ``("returned", value)`` or ``("raised", exception)``.

    Calling is safe here and nowhere near as invasive as it looks: this package
    is pure -- no transport, no deployment import, no model client, asserted by
    :class:`PackageBoundaryTests` below -- so a seat can only compute or raise.
    What is read off the call is *only* whether the body fell off the end; the
    ``None`` arguments are not a scenario, and nothing here judges what a
    correctly-called seat returns.  That belongs to the owning lane's tests.
    """
    member = getattr(module, seat)
    try:
        return "returned", member(module(), **_seat_arguments(member))
    except Exception as exception:                      # noqa: BLE001 - see above
        return "raised", exception


def _statements(member) -> list:
    """The seat's body statements, with the docstring removed."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(member)))
    body = list(tree.body[0].body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return body


def _silent_stub_reason(module, seat: str) -> Optional[str]:
    """Why this seat hands its caller nothing, or ``None`` if it does not.

    Two independent readings, because each catches what the other cannot:

    *Structural* -- a body that is only ``pass``, ``...`` or a bare ``return``
    after its docstring.  Named precisely from the syntax tree rather than by
    looking for words in the source, so a docstring that discusses ``pass``
    does not trip it and a body that raises through a helper does not need to
    spell ``raise`` at the top level.

    *Behavioural* -- the call returns ``None``, or returns an **empty**
    container.  This is the reading that survives refactoring: a seat quietly
    emptied to ``return ()``/``return {}``, or one whose helper stopped
    raising, is caught whatever the source looks like.
    """
    body = _statements(getattr(module, seat))
    if not body:
        return "its body is empty"
    if all(
        isinstance(statement, ast.Pass)
        or (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and statement.value.value is Ellipsis
        )
        or (isinstance(statement, ast.Return) and statement.value is None)
        for statement in body
    ):
        return "its body falls off the end (pass / ... / bare return)"

    outcome, value = _call_seat(module, seat)
    if outcome == "raised":
        return None
    if value is None:
        return "it returns None"
    if isinstance(value, (Mapping, tuple, list, set, frozenset, str)) and not value:
        return f"it returns an empty {type(value).__name__}"
    return None


class FamilyModuleSeatTests(unittest.TestCase):
    """The seat every lane codes against before any body exists."""

    def test_one_module_per_family_and_no_extras(self) -> None:
        self.assertEqual(set(FAMILY_MODULES), set(OBJECTIVE_FAMILIES))

    def test_every_seat_raises_rather_than_returning_a_placeholder(self) -> None:
        """An unfilled seat refuses loudly; a filled one is its lane's business.

        The seam froze the signature *and* the refusal, so that a lane starting
        work finds a traceback rather than an empty bundle.  That property is
        about a seat nobody has written yet, and it stops being checkable the
        moment a lane writes one: a filled seat returns contract content, or --
        for a family this deployment cannot support -- raises its own stated
        refusal naming the missing premise
        (:class:`~assurance.objectives.registry.RegistryError`, which is a
        different statement from "not written").  Calling a filled seat here
        with ``None`` for every argument would exercise the lane's body with
        arguments no caller passes.

        So this asserts the frozen ones still refuse, asserts the contract
        docstring on every seat either way, and leaves what a filled seat
        returns to the lane's own tests -- ``tests/assurance/test_obj1_*.py``,
        ``test_obj2_*.py``, ``test_obj3_*.py``.  A seat cannot be quietly
        emptied without one of those failing.
        """
        for family, module in FAMILY_MODULES.items():
            for seat in FROZEN_SEATS:
                with self.subTest(family=family, seat=seat):
                    member = getattr(module, seat)
                    self.assertTrue(
                        (member.__doc__ or "").strip(),
                        f"{family}.{seat} must carry its contract in a docstring",
                    )
                    if not _seat_is_unfilled(module, seat):
                        continue
                    signature = inspect.signature(member)
                    arguments = {
                        parameter.name: None
                        for parameter in signature.parameters.values()
                        if parameter.default is inspect.Parameter.empty
                        and parameter.kind
                        in (
                            inspect.Parameter.POSITIONAL_OR_KEYWORD,
                            inspect.Parameter.KEYWORD_ONLY,
                        )
                    }
                    arguments.pop("self", None)
                    with self.assertRaises(NotImplementedError):
                        member(module(), **arguments)

    def test_no_seat_is_a_silent_stub(self) -> None:
        """The half-state the check above cannot see: a seat that hands back nothing.

        A frozen seat raises, a filled seat returns contract content or raises
        its own stated refusal -- possibly through a helper, which is how two of
        the three lanes write theirs.  What no seat may be is a body that falls
        off the end, returns ``None``, or returns an empty container: each of
        those lets the shared harness run over nothing at all, which is the
        failure Gate 4's acceptance is written against.

        The negative controls in :class:`SilentStubDetectionTests` show this
        biting on each of those shapes, so "all 42 seats pass" is a statement
        about the seats and not about a check that cannot fail.
        """
        for family, module in FAMILY_MODULES.items():
            for seat in FROZEN_SEATS:
                with self.subTest(family=family, seat=seat):
                    reason = _silent_stub_reason(module, seat)
                    self.assertIsNone(
                        reason, f"{family}.{seat} is a silent stub: {reason}")

    def test_a_filled_seat_does_not_hide_a_not_implemented_path(self) -> None:
        """Source and behaviour agree in both directions.

        A seat whose body still carries the seam's refusal must raise it, and a
        seat a lane has filled must no longer raise it anywhere on the path a
        caller reaches.  Without the second half, a lane could keep the frozen
        refusal behind a branch and the suite would read as filled.
        """
        for family, module in FAMILY_MODULES.items():
            for seat in FROZEN_SEATS:
                with self.subTest(family=family, seat=seat):
                    outcome, value = _call_seat(module, seat)
                    raised_frozen = (outcome == "raised"
                                     and isinstance(value, NotImplementedError))
                    self.assertEqual(
                        raised_frozen, _seat_is_unfilled(module, seat),
                        f"{family}.{seat}: source says "
                        f"{'frozen' if _seat_is_unfilled(module, seat) else 'filled'}"
                        f" and the call says otherwise",
                    )

    def test_every_refusal_names_its_owning_lane_and_the_document(self) -> None:
        """A lane reading the traceback must learn who owns the file."""
        for family, module in FAMILY_MODULES.items():
            for seat in FROZEN_SEATS:
                with self.subTest(family=family, seat=seat):
                    try:
                        getattr(module(), seat)
                    except Exception:  # pragma: no cover - attribute access
                        self.fail(f"{family}.{seat} is missing")
                    outcome, value = _call_seat(module, seat)
                    if outcome != "raised" or not isinstance(
                            value, (NotImplementedError, RegistryError)):
                        continue
                    source = inspect.getsource(getattr(module, seat))
                    self.assertIn("SEAMS-GATE4.md", source)
                    self.assertIn(module.lane, source)

    def test_the_declared_lane_matches_the_registry(self) -> None:
        for family, module in FAMILY_MODULES.items():
            with self.subTest(family=family):
                self.assertEqual(module.family, family)
                self.assertEqual(module.lane, record_for(family).lane)

    def test_the_three_lanes_own_the_seven_families_between_them(self) -> None:
        owned = {}
        for family, module in FAMILY_MODULES.items():
            owned.setdefault(module.lane, []).append(family)
        self.assertEqual(
            {lane: sorted(families) for lane, families in owned.items()},
            {
                "OBJ1": ["QoSTarget", "QoSandTSP", "TrafficSteeringPreference"],
                "OBJ2": ["QoETarget", "QoEandTSP", "UELevelTarget"],
                "OBJ3": ["SliceSLATarget"],
            },
        )

    def test_the_base_class_seats_are_the_ones_the_document_names(self) -> None:
        public = {
            name
            for name, member in vars(ObjectiveFamilyModule).items()
            if callable(member) and not name.startswith("_")
        }
        self.assertEqual(public, set(FROZEN_SEATS))


class SilentStubDetectionTests(unittest.TestCase):
    """The stub check, shown failing on every shape of stub it claims to catch.

    A check nobody has seen fail is a check nobody knows the strength of.  Each
    class below is a family module whose seat has been emptied in one of the
    ways a real one could be emptied by a careless refactor, and each must be
    named as a stub.  The last one is the control in the other direction: a seat
    that raises through a helper -- the style two of the three lanes use -- must
    *not* be flagged, which is exactly the false positive that made the earlier
    source-text version of this check wrong.
    """

    def _reason(self, module) -> object:
        return _silent_stub_reason(module, "candidate_parameters")

    def test_a_pass_body_is_a_stub(self) -> None:
        class PassBody(ObjectiveFamilyModule):
            family, lane = "PassBody", "OBJ0"

            def candidate_parameters(self):
                """Contract docstring."""
                pass

        self.assertIn("falls off the end", str(self._reason(PassBody)))

    def test_an_ellipsis_body_is_a_stub(self) -> None:
        class EllipsisBody(ObjectiveFamilyModule):
            family, lane = "EllipsisBody", "OBJ0"

            def candidate_parameters(self):
                """Contract docstring."""
                ...

        self.assertIn("falls off the end", str(self._reason(EllipsisBody)))

    def test_a_bare_return_is_a_stub(self) -> None:
        class BareReturn(ObjectiveFamilyModule):
            family, lane = "BareReturn", "OBJ0"

            def candidate_parameters(self):
                """Contract docstring."""
                return

        self.assertIn("falls off the end", str(self._reason(BareReturn)))

    def test_returning_none_after_real_work_is_a_stub(self) -> None:
        """The shape the structural half cannot see."""
        class ComputesNothing(ObjectiveFamilyModule):
            family, lane = "ComputesNothing", "OBJ0"

            def candidate_parameters(self):
                """Contract docstring."""
                axes = {"power": ("low", "high")}
                if axes:
                    return None
                return axes

        self.assertEqual(self._reason(ComputesNothing), "it returns None")

    def test_a_seat_quietly_emptied_is_a_stub(self) -> None:
        """The regression this whole check exists for.

        A catalog emptied to ``{}`` keeps every signature, every docstring and
        every type; the harness would freeze an epoch over no candidates at all.
        """
        class Emptied(ObjectiveFamilyModule):
            family, lane = "Emptied", "OBJ0"

            def candidate_parameters(self):
                """Contract docstring."""
                return {}

        self.assertIn("empty", str(self._reason(Emptied)))

        class EmptiedTuple(ObjectiveFamilyModule):
            family, lane = "EmptiedTuple", "OBJ0"

            def kpi_declaration(self):
                """Contract docstring."""
                return ()

        self.assertIn("empty", str(_silent_stub_reason(EmptiedTuple,
                                                       "kpi_declaration")))

    def test_a_seat_that_raises_through_a_helper_is_not_a_stub(self) -> None:
        """The false positive the source-text check produced on two lanes."""
        def refuse(family: str, seat: str):
            raise RegistryError(f"{family}: {seat} refused")

        class RefusesViaHelper(ObjectiveFamilyModule):
            family, lane = "RefusesViaHelper", "OBJ0"

            def candidate_parameters(self):
                """Contract docstring."""
                refuse("RefusesViaHelper", "candidate_parameters")

        self.assertIsNone(self._reason(RefusesViaHelper))

    def test_a_seat_that_returns_content_is_not_a_stub(self) -> None:
        class Real(ObjectiveFamilyModule):
            family, lane = "Real", "OBJ0"

            def candidate_parameters(self):
                """Contract docstring."""
                return {"power": ("low", "high")}

        self.assertIsNone(self._reason(Real))


class PackageBoundaryTests(unittest.TestCase):
    """The new subtree keeps the Gate 2 boundary."""

    def test_no_forbidden_imports(self) -> None:
        violations = []
        for path in _source_files():
            for name in _imported_names(path):
                for banned in FORBIDDEN_IMPORTS:
                    if name == banned or name.startswith(f"{banned}."):
                        violations.append(f"{path.relative_to(REPOSITORY_ROOT)}:{name}")
        self.assertEqual(violations, [], f"forbidden imports: {violations}")

    def test_the_registry_does_not_read_the_deployment(self) -> None:
        """Capability facts are literals with a stated basis, not a live read.

        A registry that read ``oran.integration`` could not be replayed, and a
        disagreement with the deployment would resolve itself silently instead
        of failing ``tests/assurance/test_oseam_registry.py``.
        """
        for path in _source_files():
            for name in _imported_names(path):
                self.assertFalse(
                    name.startswith("oran"),
                    f"{path.relative_to(REPOSITORY_ROOT)} imports {name}",
                )

    def test_every_capability_claim_states_where_it_came_from(self) -> None:
        for record in OBJECTIVE_REGISTRY:
            with self.subTest(family=record.family):
                self.assertTrue(record.deployment_capability.basis.strip())
                for premise in record.premises:
                    self.assertTrue(premise.basis.strip())


class OwnershipDocumentTests(unittest.TestCase):
    def test_the_seams_document_lists_every_new_file(self) -> None:
        text = SEAMS_DOC.read_text(encoding="utf-8")
        missing = [
            str(path.relative_to(REPOSITORY_ROOT))
            for path in _source_files()
            if str(path.relative_to(REPOSITORY_ROOT)) not in text
        ]
        self.assertEqual(missing, [], f"files with no ownership entry: {missing}")

    def test_the_seams_document_lists_the_harness_and_its_tests(self) -> None:
        text = SEAMS_DOC.read_text(encoding="utf-8")
        for path in (
            "tests/assurance/objective_harness.py",
            "tests/assurance/test_oseam_registry.py",
            "tests/assurance/test_oseam_harness.py",
            "tests/assurance/test_oseam_seams.py",
        ):
            self.assertIn(path, text)

    def test_the_gate_2_document_points_at_this_one(self) -> None:
        self.assertIn("SEAMS-GATE4.md", GATE2_SEAMS_DOC.read_text(encoding="utf-8"))

    def test_the_document_states_the_versioned_pin_relationship(self) -> None:
        text = SEAMS_DOC.read_text(encoding="utf-8")
        self.assertIn("PIN_TO_CELL", text)
        self.assertIn(PIN_REGRESSION_FAMILY, text)
        self.assertIn("mapping 1.2.0", text)
        self.assertIn("QoSTarget", text)
        self.assertIn("QoSandTSP", text)

    def test_the_document_names_the_joint_trial_condition(self) -> None:
        text = SEAMS_DOC.read_text(encoding="utf-8")
        self.assertIn("component_predicates", text)
        self.assertIn("QoSandTSP", text)
        self.assertIn("QoEandTSP", text)


class HarnessContractTests(unittest.TestCase):
    """What the shared harness promises a lane, as a surface."""

    def test_the_mixin_covers_every_scenario_the_task_lists(self) -> None:
        methods = {name for name in dir(ObjectiveMatrixMixin) if name.startswith("test_")}
        for scenario in ScenarioName:
            with self.subTest(scenario=scenario.value):
                self.assertIn(f"test_{scenario.value.replace('-', '_')}", methods)

    def test_a_lane_that_forgets_its_case_is_told_so(self) -> None:
        with self.assertRaises(NotImplementedError):
            ObjectiveMatrixMixin().make_case()

    def test_the_bundle_names_every_contract_family_a_trial_needs(self) -> None:
        fields = {field.name for field in ObjectiveContractBundle.__dataclass_fields__.values()}
        self.assertTrue(
            {
                "counters", "measurements", "target", "vector", "release",
                "case_policy", "watchdogs", "harm", "deployment", "actuators",
                "capabilities", "composition", "baseline_config", "safe_state",
                "scope", "sample_scope", "component_predicates",
            }
            <= fields
        )

    def test_verdict_scenarios_are_the_ones_a_family_declares(self) -> None:
        self.assertEqual(
            {scenario.value for scenario in VERDICT_SCENARIOS},
            {"positive", "negative", "stale", "missing", "partial-effect", "fault"},
        )


if __name__ == "__main__":
    unittest.main()
