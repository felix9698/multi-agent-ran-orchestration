"""The prompt carries the object, not the whitespace around it.

Both arms that use a grid -- three-agent and internal monolith -- send ``T``
and ``C`` on every decision, so anything the serialiser adds per call is paid
per decision by exactly the separation the ablation exists to price.  These
tests pin the two places that were adding size without adding information.
"""
import json
import unittest

from assurance.coordination.agents import _user_prompt
from assurance.coordination.tc import ControlCandidate


class TheUserPromptCarriesTheObjectNotItsFormatting(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = {
            "input.target_contract": {
                "alternatives": [{"targetId": f"T{i}", "levels": {"I1.r1": i}}
                                 for i in range(9)],
                "weights": {"I1.r1": 3.0, "I2.r1": 2.0},
            },
            "input.control_candidates": [
                {"controlId": f"C{i}", "configuration": {"dlPrbCap@1": str(i)}}
                for i in range(8)],
        }
        self.schema = {"controlId": "string", "targetId": "string"}

    def test_the_payload_round_trips_through_the_prompt(self) -> None:
        """Compact separators must not change what the model receives."""
        prompt = _user_prompt(self.payload, self.schema)
        body = prompt.split("INPUTS:\n", 1)[1].split("\n\nOUTPUT SCHEMA:\n")[0]
        self.assertEqual(json.loads(body), self.payload)

    def test_the_schema_round_trips_too(self) -> None:
        prompt = _user_prompt(self.payload, self.schema)
        body = prompt.split("\n\nOUTPUT SCHEMA:\n", 1)[1]
        body = body.split("\n\nReturn only the JSON object.")[0]
        self.assertEqual(json.loads(body), self.schema)

    def test_it_spends_no_bytes_on_indentation(self) -> None:
        """An indented copy of the same object was 42% larger when measured."""
        prompt = _user_prompt(self.payload, self.schema)
        indented = len(json.dumps(self.payload, indent=1, default=str))
        compact = len(json.dumps(self.payload, separators=(",", ":"), default=str))
        self.assertLess(compact, indented)
        self.assertIn(json.dumps(self.payload, separators=(",", ":"),
                                 default=str), prompt)

    def test_it_still_names_the_three_sections(self) -> None:
        """SINGLE_CALL.md fixes the shape: INPUTS, OUTPUT SCHEMA, the instruction."""
        prompt = _user_prompt(self.payload, self.schema)
        self.assertTrue(prompt.startswith("INPUTS:\n"))
        self.assertIn("\n\nOUTPUT SCHEMA:\n", prompt)
        self.assertTrue(prompt.endswith("\n\nReturn only the JSON object."))


class AnEmptyCandidateFieldIsNotSent(unittest.TestCase):
    """A baseline candidate predicts nothing; ``predicted: {}`` says so at a price."""

    def test_empty_positions_are_omitted(self) -> None:
        record = ControlCandidate(control_id="C0",
                                  configuration={"dlPrbCap@1": "0"}).to_record()
        for key in ("predicted", "uncertainty", "effectEstimate", "evidenceRefs",
                    "functions", "interactions", "predictedTarget", "rationale",
                    "applicability"):
            self.assertNotIn(key, record)

    def test_identity_is_always_written(self) -> None:
        """Absence of a configuration would be a different statement, not an omission.

        An empty ``controlId`` cannot be constructed at all -- the candidate
        refuses it -- so the case to pin is the empty *configuration*: the
        baseline candidate that changes nothing still has to say so.
        """
        record = ControlCandidate(control_id="C0", configuration={}).to_record()
        self.assertEqual(record["controlId"], "C0")
        self.assertEqual(record["configuration"], {})

    def test_populated_positions_survive(self) -> None:
        record = ControlCandidate(
            control_id="C1", configuration={"dlPrbCap@1": "6"},
            predicted={"dlGoodputMbps": 4.8}, applicability=("cap on ue1",),
            rationale="cap the incumbent").to_record()
        self.assertEqual(record["predicted"], {"dlGoodputMbps": 4.8})
        self.assertEqual(record["applicability"], ["cap on ue1"])
        self.assertEqual(record["rationale"], "cap the incumbent")

    def test_the_round_trip_is_unchanged_by_the_omission(self) -> None:
        """``from_record`` defaults every omitted key, so nothing is lost."""
        original = ControlCandidate(control_id="C0",
                                    configuration={"dlPrbCap@1": "0"})
        revived = ControlCandidate.from_record(original.to_record())
        self.assertEqual(revived, original)


if __name__ == "__main__":
    unittest.main()
