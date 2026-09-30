"""T4 verification of the frozen, colour-independent status contract."""

import json
import unittest
from pathlib import Path

from gui.operator import status


SPEC = Path(__file__).resolve().parents[2] / "docs" / "phase-b-gui" / "status-vocabulary.1.0.0.json"


class StatusVocabularyTests(unittest.TestCase):
    def test_specified_contract_values_resolve_to_a_known_status(self):
        specification = json.loads(SPEC.read_text(encoding="utf-8"))
        for table, mapping in specification["contractMappings"].items():
            for upstream in mapping:
                with self.subTest(table=table, upstream=upstream):
                    self.assertIn(status.map_contract_value(table, upstream).status, status.STATUS_SPECS)

    def test_an_unmapped_value_is_unknown_not_a_success(self):
        resolved = status.map_contract_value("enforceStatus", "FUTURE_VALUE")
        self.assertEqual(resolved.status, status.UNKNOWN)
        self.assertNotEqual(resolved.status, status.OK)

    def test_status_encoding_is_not_colour_only(self):
        specs = tuple(status.STATUS_SPECS.values())
        self.assertEqual(len({item.glyph for item in specs}), len(specs))
        self.assertEqual(len({item.shape for item in specs}), len(specs))


if __name__ == "__main__":
    unittest.main()
