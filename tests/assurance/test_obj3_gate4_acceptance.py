"""The Gate 4 acceptance document, checked against the code it describes.

Lane **OBJ3** (``docs/architecture/SEAMS-GATE4.md`` section 3).

An acceptance document's failure mode is not being wrong when written -- it is
being right when written and quietly false three commits later.  Gate 2's
acceptance document had to be corrected by hand when the reducer version moved;
this one is checked instead, so the values in
``docs/architecture/GATE4-ACCEPTANCE.md`` and its machine-readable copy
``gate4-acceptance.1.0.0.json`` cannot drift away from the registry, the seats
and the test counts without a test going red.

Three things are pinned:

**The two documents agree.**  Every acceptance verdict, the baseline commit and
every test count appear in both, and the markdown is where a reader looks first,
so a JSON updated alone is a defect.

**The record and seat tables are the live ones.**  The eight registry records and
the six seats per family are read out of the code, not out of the document, and
compared.  A lane that fills a seat or promotes a record without updating the
gate document is what this catches.

**The verdicts are honest by vocabulary.**  ``PASS`` / ``PARTIAL`` /
``NOT_APPLICABLE`` and nothing else, and a criterion recorded ``PASS`` must not
be one the document itself describes as unmerged.

Hermetic: reads two files and the registry, runs nothing.
"""

from __future__ import annotations

import inspect
import json
import unittest
from pathlib import Path
from typing import Dict

from assurance.objectives import FAMILY_MODULES
from assurance.objectives.registry import (
    OBJECTIVE_REGISTRY,
    SupportState,
    validate_registry,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DOC = REPOSITORY_ROOT / "docs" / "architecture" / "GATE4-ACCEPTANCE.md"
JSON_DOC = REPOSITORY_ROOT / "docs" / "architecture" / "gate4-acceptance.1.0.0.json"

SEATS = ("contract_bundle", "candidate_parameters", "policy_lifecycle",
         "kpi_declaration", "terminal_oracle", "hardware_free_expectations")

RESULTS = {"PASS", "PARTIAL", "NOT_APPLICABLE"}

#: The four acceptance items of task section 13, Gate 4.
ACCEPTANCE_IDS = ("B1", "B2", "B3", "B4")

#: Every lane's Gate 4 test file, and where its count is recorded.  All three
#: lanes are covered, not only this one: the acceptance document reports the
#: whole gate, so a count it carries for another lane has to be as checked as
#: its own.
LANE_TEST_MODULES = (
    "tests.assurance.test_obj1_matrix",
    "tests.assurance.test_obj2_ue_level_target",
    "tests.assurance.test_obj2_qoe_target",
    "tests.assurance.test_obj2_qoe_and_tsp",
    "tests.assurance.test_obj3_slice_sla_target",
    "tests.assurance.test_obj3_gate4_acceptance",
    "tests.gui.test_obj3_registry_projection",
)

#: The subset this lane owns, counted separately because the OBJ3 reproduce
#: line (``discover -s tests -p 'test_obj3_*.py'``) reports exactly it.
OBJ3_TEST_MODULES = (
    "tests.assurance.test_obj3_slice_sla_target",
    "tests.assurance.test_obj3_gate4_acceptance",
    "tests.gui.test_obj3_registry_projection",
)


def _count(module_name: str) -> int:
    loader = unittest.defaultTestLoader
    return loader.loadTestsFromName(module_name).countTestCases()


def _seats(family: str) -> Dict[str, str]:
    module = FAMILY_MODULES[family]
    return {
        seat: ("FROZEN_SEAT"
               if "raise NotImplementedError" in
               inspect.getsource(getattr(module, seat))
               else "FILLED_BY_LANE")
        for seat in SEATS
    }


class BothDocumentsExistAndParse(unittest.TestCase):
    def test_the_markdown_and_the_json_are_present(self) -> None:
        self.assertTrue(DOC.exists(), f"{DOC} is missing")
        self.assertTrue(JSON_DOC.exists(), f"{JSON_DOC} is missing")

    def test_the_json_parses_and_names_this_gate(self) -> None:
        document = json.loads(JSON_DOC.read_text(encoding="utf-8"))
        self.assertEqual(document["schemaVersion"], "1.0.0")
        self.assertEqual(document["gate"], "W-OBJ3")
        self.assertTrue(document["authority"])


class TheTwoDocumentsAgree(unittest.TestCase):
    def setUp(self) -> None:
        self.document = json.loads(JSON_DOC.read_text(encoding="utf-8"))
        self.text = DOC.read_text(encoding="utf-8")

    def test_every_acceptance_item_has_a_verdict_in_the_vocabulary(self) -> None:
        items = {item["id"]: item for item in self.document["acceptance"]}
        self.assertEqual(tuple(sorted(items)), ACCEPTANCE_IDS)
        for identifier, item in items.items():
            with self.subTest(item=identifier):
                self.assertIn(item["result"], RESULTS)
                self.assertTrue(item["criterion"].strip())
                self.assertTrue(item["command"].strip())
                self.assertTrue(item["observed"])

    def test_the_markdown_carries_the_same_verdicts(self) -> None:
        for item in self.document["acceptance"]:
            with self.subTest(item=item["id"]):
                row = next((line for line in self.text.splitlines()
                            if line.startswith(f"| {item['id']} |")), None)
                self.assertIsNotNone(row, f"{item['id']} has no row in the markdown")
                self.assertIn(f"**{item['result']}**", row)

    def test_the_baseline_commit_matches(self) -> None:
        self.assertIn(self.document["baselineCommit"], self.text)
        self.assertIn(self.document["seamCommit"], self.text)

    def test_the_reproduce_commands_appear_in_the_markdown(self) -> None:
        for entry in self.document["reproduce"]:
            with self.subTest(command=entry["command"]):
                self.assertIn(entry["command"], self.text)

    def test_what_is_not_claimed_is_stated_in_both(self) -> None:
        self.assertTrue(self.document["notClaimed"])
        self.assertIn("무엇을 주장하지 않는가", self.text)


class TheRecordTableIsTheLiveRegistry(unittest.TestCase):
    def setUp(self) -> None:
        self.document = json.loads(JSON_DOC.read_text(encoding="utf-8"))
        self.records = {item["family"]: item for item in self.document["records"]}

    def test_the_registry_still_validates(self) -> None:
        validate_registry(OBJECTIVE_REGISTRY)

    def test_every_record_is_described_exactly_once(self) -> None:
        self.assertEqual(
            [item["family"] for item in self.document["records"]],
            [record.family for record in OBJECTIVE_REGISTRY])

    def test_each_recorded_axis_matches_the_registry(self) -> None:
        for record in OBJECTIVE_REGISTRY:
            with self.subTest(family=record.family):
                item = self.records[record.family]
                self.assertEqual(item["supportState"], record.support_state.value)
                self.assertEqual(item["evidenceLevel"], record.evidence_level.value)
                self.assertEqual(item["submittable"],
                                 record.deployment_capability.submittable)
                self.assertEqual(item["blockingReasons"],
                                 list(record.deployment_capability.blocking_reasons))
                self.assertEqual(item["unmetPremises"],
                                 [premise.kind.value
                                  for premise in record.unmet_premises()])
                self.assertEqual(item["evidenceRefs"], list(record.evidence_refs))

    def test_the_markdown_table_names_each_family_and_its_state(self) -> None:
        """One row somewhere in the markdown carries all three axes together.

        Searched across the whole document rather than at a fixed line, so
        re-ordering the sections does not fail the test -- but the row must
        carry the family, its support state and its evidence level *in the same
        row*, which is what makes it the record table and not a mention.
        """
        rows = [line for line in DOC.read_text(encoding="utf-8").splitlines()
                if line.startswith("|")]
        for record in OBJECTIVE_REGISTRY:
            with self.subTest(family=record.family):
                matching = [
                    row for row in rows
                    if f"`{record.family}`" in row
                    and record.support_state.value in row
                    and record.evidence_level.value in row
                ]
                self.assertTrue(
                    matching,
                    f"no markdown row states {record.family} with "
                    f"{record.support_state.value} and "
                    f"{record.evidence_level.value}")

    def test_the_seat_table_is_the_live_one(self) -> None:
        for family in FAMILY_MODULES:
            with self.subTest(family=family):
                item = self.records[family]
                self.assertEqual(item["seats"], _seats(family))
                self.assertEqual(
                    item["seatsFilled"],
                    sum(value == "FILLED_BY_LANE"
                        for value in _seats(family).values()))


class TheVerdictsAreConsistentWithTheState(unittest.TestCase):
    """A verdict that contradicts the tree is worse than a missing one."""

    def setUp(self) -> None:
        self.document = json.loads(JSON_DOC.read_text(encoding="utf-8"))
        self.items = {item["id"]: item for item in self.document["acceptance"]}

    def test_b1_is_pass_only_when_every_seat_is_filled(self) -> None:
        filled = all(state == "FILLED_BY_LANE"
                     for family in FAMILY_MODULES
                     for state in _seats(family).values())
        if not filled:
            self.assertNotEqual(
                self.items["B1"]["result"], "PASS",
                "B1 cannot be PASS while a family module is still a frozen seat")

    def test_the_lanes_recorded_as_unmerged_really_are(self) -> None:
        for lane in self.document["scope"]["lanesNotMerged"]:
            with self.subTest(lane=lane):
                families = [family for family in FAMILY_MODULES
                            if FAMILY_MODULES[family].lane == lane]
                self.assertTrue(families)
                for family in families:
                    self.assertTrue(
                        all(state == "FROZEN_SEAT"
                            for state in _seats(family).values()),
                        f"{family} has filled seats but {lane} is recorded as "
                        "not merged")

    def test_the_lane_recorded_as_merged_really_is(self) -> None:
        for lane in self.document["scope"]["lanesInThisCommit"]:
            with self.subTest(lane=lane):
                families = [family for family in FAMILY_MODULES
                            if FAMILY_MODULES[family].lane == lane]
                for family in families:
                    self.assertTrue(
                        all(state == "FILLED_BY_LANE"
                            for state in _seats(family).values()),
                        f"{family} is recorded as merged but has a frozen seat")

    def test_every_unsupported_family_is_not_applicable_for_b2(self) -> None:
        """The judgement this gate owed, written down rather than implied.

        Derived from the registry, not from a list in the document: a family
        that fails closed is exactly the family the matrix does not run for, so
        the two must move together or the document is describing a different
        tree than the one it ships with.
        """
        observed = self.items["B2"]["observed"]
        unsupported = sorted(
            record.family for record in OBJECTIVE_REGISTRY
            if record.support_state is SupportState.UNSUPPORTED_BY_CURRENT_DEPLOYMENT)
        self.assertEqual(sorted(observed["notApplicable"]), unsupported)
        self.assertIn("self-contradictory", observed["notApplicableBasis"])
        matrix_families = sorted(observed["matrixFamilies"])
        self.assertEqual(
            matrix_families,
            sorted(record.family for record in OBJECTIVE_REGISTRY
                   if record.hardware_free_scenarios))
        self.assertFalse(set(matrix_families) & set(unsupported))
        text = DOC.read_text(encoding="utf-8")
        self.assertIn("지원 목록에 없는 family에 round-trip을 요구하는 것은 자기모순", text)
        self.assertIn("NOT_APPLICABLE", text)

    def test_every_ota_claim_is_the_registry_s_and_carries_its_evidence(self) -> None:
        """At Gate 4 only the regression contract claimed OTA evidence.

        Gate 5 stage 2 drove ``TrafficSteeringPreference`` and ``UELevelTarget``
        over real radio, so a fixed list would now be a statement about a
        moment rather than about honesty.  What has to hold is that the
        document's OTA claims are exactly the registry's, and that each one
        names retained evidence that exists.
        """
        ota = sorted(item["family"] for item in self.document["records"]
                     if item["evidenceIsOta"])
        self.assertEqual(
            ota,
            sorted(record.family for record in OBJECTIVE_REGISTRY
                   if record.as_dict()["evidenceIsOta"]))
        self.assertIn("UeCellSteeringPinToCell", ota)
        for item in self.document["records"]:
            if not item["evidenceIsOta"]:
                self.assertEqual(item["evidenceRefs"], [])
                continue
            self.assertTrue(item["evidenceRefs"],
                            f"{item['family']} claims OTA with no evidence")
            for reference in item["evidenceRefs"]:
                self.assertTrue(
                    (REPOSITORY_ROOT / reference).exists(),
                    f"{item['family']}: retained evidence missing: {reference}")


class TheTestCountsAreReal(unittest.TestCase):
    """The numbers in the document are counted, not remembered."""

    def setUp(self) -> None:
        self.document = json.loads(JSON_DOC.read_text(encoding="utf-8"))
        self.counts = self.document["tests"]

    def test_each_lane_test_file_holds_the_recorded_number(self) -> None:
        for module_name in LANE_TEST_MODULES:
            path = module_name.replace(".", "/") + ".py"
            with self.subTest(module=path):
                self.assertEqual(self.counts[path], _count(module_name))

    def test_the_lane_total_is_the_sum(self) -> None:
        self.assertEqual(
            self.counts["laneTotal"],
            sum(_count(module_name) for module_name in LANE_TEST_MODULES))

    def test_the_obj3_total_is_the_sum_of_this_lane(self) -> None:
        self.assertEqual(
            self.counts["obj3LaneTotal"],
            sum(_count(module_name) for module_name in OBJ3_TEST_MODULES))

    def test_the_markdown_reports_the_same_numbers(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        for key in ("laneTotal", "assuranceSuiteTotal", "guiSuiteTotal"):
            with self.subTest(key=key):
                self.assertIn(str(self.counts[key]), text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
