"""Gate 5 stage 2: what the live console needs beyond a family's contract bundle.

Three seams, all hermetic -- no radio, no NETCONF, no HTTPS:

* the direction a bundle actually expresses, which is not always the direction
  that was asked for;
* the Operator sentence, which must parse back to the family and the cell the
  bundle carries;
* the six-contract projection the live policy builder derives a policy from.

The direction tests are the interesting ones.  ``TrafficSteeringPreference``
fixes its cell pair in module constants and ignores the scope; the QoS families
and ``UELevelTarget`` read the scope.  That difference is asserted here,
because it is the reason the runner refuses a submission whose sentence and
contract would name different cells -- see
``docs/integration/evidence/GATE5-OTA-RECORD.md``.
"""

from __future__ import annotations

import unittest
from typing import Any, Mapping

from assurance.advisors.grammar import parse_utterance
from assurance.contracts.live_binding import load_assurance_live_binding
from assurance.contracts.target import ComparisonOperator
from assurance.objectives import FAMILY_MODULES
from tools.g5ota.objective_live import (
    FAMILY_UTTERANCES,
    ObjectiveWiringError,
    bundle_contracts,
    bundle_direction,
    family_grammar,
    family_utterance,
    live_scope,
)

BINDING = "deployment/assurance-live-binding.1.0.0.json"
CELL_A, CELL_B = 12345678, 87654321
UE = 150

#: Every project family mapped onto the released cell-steering wire kind.
ACTUABLE = (
    "TrafficSteeringPreference",
    "UELevelTarget",
    "QoSTarget",
    "QoSandTSP",
)


def _bundle(family: str, *, home: int, target: int) -> Any:
    binding = load_assurance_live_binding(BINDING)
    return FAMILY_MODULES[family]().contract_bundle(
        scope=live_scope(amf_ue_ngap_id=UE, home_nci=home, target_nci=target),
        deployment_binding=binding.r1.deployment,
    )


class TheScopeIsBuiltFromWhatWasObserved(unittest.TestCase):

    def test_it_carries_the_ue_and_both_cell_identities_as_strings(self) -> None:
        scope = live_scope(amf_ue_ngap_id=UE, home_nci=CELL_A, target_nci=CELL_B)
        self.assertEqual(
            scope,
            {
                "ueId": "150",
                "cellId": "NRCellDU-1",
                "targetServingCell": "87654321",
                "homeServingCell": "12345678",
            },
        )


class TheBundleStatesItsOwnDirection(unittest.TestCase):

    def test_the_forward_direction_is_expressed_by_both_families(self) -> None:
        for family in ACTUABLE:
            with self.subTest(family=family):
                direction = bundle_direction(
                    _bundle(family, home=CELL_A, target=CELL_B))
                self.assertEqual((direction.baseline, direction.target),
                                 (CELL_A, CELL_B))

    def test_qos_and_ue_level_honour_a_reversed_scope_while_tsp_stays_fixed(self) -> None:
        steering = bundle_direction(
            _bundle("TrafficSteeringPreference", home=CELL_B, target=CELL_A))
        self.assertEqual((steering.baseline, steering.target), (CELL_A, CELL_B))

        for family in ("UELevelTarget", "QoSTarget", "QoSandTSP"):
            with self.subTest(family=family):
                directional = bundle_direction(
                    _bundle(family, home=CELL_B, target=CELL_A))
                self.assertEqual(
                    (directional.baseline, directional.target), (CELL_B, CELL_A)
                )


class TheSentenceIsWrittenFromTheBundle(unittest.TestCase):

    def _grammar(self, family: str, bundle: Any) -> Mapping[str, Any]:
        return family_grammar(family, bundle)

    def test_every_actuable_family_has_a_sentence(self) -> None:
        for family in ACTUABLE:
            self.assertIn(family, FAMILY_UTTERANCES)

    def test_the_sentence_parses_back_to_the_family_and_the_bundle_s_cell(
        self,
    ) -> None:
        for family in ACTUABLE:
            with self.subTest(family=family):
                bundle = _bundle(family, home=CELL_A, target=CELL_B)
                direction = bundle_direction(bundle)
                sentence = family_utterance(family, direction.target, str(UE))
                selected, scope, constraints, unsupported = parse_utterance(
                    sentence,
                    objective_registry=self._grammar(family, bundle),
                    source_record="test",
                )
                self.assertEqual(selected, family)
                self.assertEqual(dict(scope), {"ueId": str(UE)})
                self.assertEqual(unsupported, ())
                self.assertEqual(len(constraints), 1)
                drafted = constraints[0]
                self.assertEqual(drafted.operator, ComparisonOperator.EQUAL)
                self.assertEqual(int(drafted.bound.value), direction.target)
                self.assertEqual(drafted.bound.unit, "nci")
                self.assertEqual(
                    drafted.measurement_ref,
                    self._grammar(family, bundle)[family].measurement_ref)

    def test_the_cell_precedes_the_scope_token_in_every_sentence(self) -> None:
        """A UE label containing a digit must not be read as the cell.

        The grammar takes the first number in the sentence as the bound, so the
        ordering is load-bearing rather than stylistic.
        """
        for family in ACTUABLE:
            with self.subTest(family=family):
                sentence = family_utterance(family, CELL_B, str(UE))
                self.assertLess(sentence.index(str(CELL_B)),
                                sentence.index("ueId="))

    def test_the_grammar_binds_the_bundle_s_own_minimum_measurement(self) -> None:
        for family in ACTUABLE:
            with self.subTest(family=family):
                bundle = _bundle(family, home=CELL_A, target=CELL_B)
                entry = self._grammar(family, bundle)[family]
                expected = [
                    m.contract_id for m in bundle.measurements
                    if str(m.contract_id).endswith("-min")
                ]
                self.assertEqual([entry.measurement_ref], expected)

    def test_an_unmapped_family_with_no_sentence_is_refused(self) -> None:
        with self.assertRaises(ObjectiveWiringError):
            family_utterance("QoETarget", CELL_B, str(UE))


class TheSixContractsAreSelectedNotSubstituted(unittest.TestCase):

    def test_the_projection_names_the_bundle_s_own_contracts(self) -> None:
        for family in ACTUABLE:
            with self.subTest(family=family):
                bundle = _bundle(family, home=CELL_A, target=CELL_B)
                contracts = bundle_contracts(bundle)
                self.assertEqual(
                    sorted(contracts),
                    ["actuator", "case_policy", "harm", "measurement_max",
                     "measurement_min", "target"],
                )
                self.assertIs(contracts["target"], bundle.target)
                self.assertIs(contracts["harm"], bundle.harm)
                self.assertIs(contracts["case_policy"], bundle.case_policy)
                self.assertIn(contracts["actuator"], bundle.actuators)
                self.assertIn(contracts["measurement_min"], bundle.measurements)
                self.assertIn(contracts["measurement_max"], bundle.measurements)
                self.assertTrue(
                    str(contracts["measurement_min"].contract_id).endswith("-min"))
                self.assertTrue(
                    str(contracts["measurement_max"].contract_id).endswith("-max"))

    def test_an_ambiguous_bundle_is_refused_rather_than_picked_from(self) -> None:
        import dataclasses

        bundle = _bundle("UELevelTarget", home=CELL_A, target=CELL_B)
        doubled = dataclasses.replace(
            bundle, actuators=bundle.actuators + bundle.actuators)
        with self.assertRaises(ObjectiveWiringError) as raised:
            bundle_contracts(doubled)
        self.assertIn("actuator binding", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
