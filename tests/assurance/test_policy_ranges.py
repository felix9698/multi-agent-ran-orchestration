"""v48 (owner 2026-09-26): with AIC_POLICY_RANGE=1 models see numeric policy fields as a range
and their number is snapped onto the frozen grid; unset, nothing changes."""
import os
import unittest
from unittest import mock

from assurance.coordination import agents, tc

CATALOG = tc.FunctionCatalog(functions=(
    tc.FunctionSpec(function_id="atten", scopes=("cell@8",), axis="txAttenuationDb@8",
                    policy_fields={"txAttenuationDb": {"values": ["6.0", "6.5", "7.0", "7.5"],
                                                       "unit": "dB", "baseline": "6.0"}}),
    tc.FunctionSpec(function_id="steer", scopes=("ue@ue1",), axis="servingCell@<ue>",
                    policy_fields={"servingCell": {"values": ["1", "2"], "unit": "nci",
                                                   "baseline": "1"}}),
))


def candidate(function_id, scope, field, value):
    return tc.ControlCandidate.from_record({"controlId": "x", "functions": [
        {"functionId": function_id, "scope": scope, "policy": {field: value}}]})


class PolicyRanges(unittest.TestCase):
    def test_off_is_unchanged(self):
        with mock.patch.dict(os.environ, {"AIC_POLICY_RANGE": ""}):
            record = CATALOG.function("atten").policy_fields["txAttenuationDb"].to_record()
            self.assertEqual(["6.0", "6.5", "7.0", "7.5"], record["values"])
            self.assertIs(tc.snap_function_rows("x", CATALOG), "x")
            with self.assertRaises(tc.ControlValidationError):
                tc.translate_functions(candidate("atten", "cell@8", "txAttenuationDb", "6.8"), CATALOG)
            self.assertIn("<one listed value>", agents._user_prompt({}, agents._BASIC_SCHEMA))

    def test_on_shows_range_and_snaps(self):
        with mock.patch.dict(os.environ, {"AIC_POLICY_RANGE": "1"}):
            record = CATALOG.function("atten").policy_fields["txAttenuationDb"].to_record()
            self.assertEqual(["6.0", "6.5", "7.0", "7.5"], record["values"])   # records keep the grid
            self.assertEqual(CATALOG.to_record(), tc.FunctionCatalog.from_record(CATALOG.to_record()).to_record())
            prompt = agents._user_prompt({"input.function_catalog": CATALOG.to_record()}, {})
            self.assertIn('"txAttenuationDb":{"unit":"dB","baseline":"6.0","min":"6.0","max":"7.5"}', prompt)
            self.assertIn('"servingCell":{"values":["1","2"]', prompt)
            rows = tc.snap_function_rows([{"functionId": "atten", "scope": "cell@8",
                                           "policy": {"txAttenuationDb": 6.8}}], CATALOG)
            self.assertEqual("7.0", rows[0]["policy"]["txAttenuationDb"])
            self.assertEqual({"txAttenuationDb@8": "7.5", "servingCell@ue1": "1.4"},
                             tc.snap_configuration({"txAttenuationDb@8": "7.4", "servingCell@ue1": "1.4"},
                                                   {"txAttenuationDb@8": ("6.0", "7.5"), "servingCell@ue1": ("1", "2")}))
            got = tc.translate_functions(candidate("atten", "cell@8", "txAttenuationDb", 6.8), CATALOG)
            self.assertEqual("7.0", got["txAttenuationDb@8"])
            got = tc.translate_functions(candidate("atten", "cell@8", "txAttenuationDb", "6.25"), CATALOG)
            self.assertEqual("6.0", got["txAttenuationDb@8"])       # tie -> lower
            for bad in ("9", "5.9", "nan", "high"):
                with self.assertRaises(tc.ControlValidationError):
                    tc.translate_functions(candidate("atten", "cell@8", "txAttenuationDb", bad), CATALOG)
            with self.assertRaises(tc.ControlValidationError):     # a cell id is never snapped
                tc.translate_functions(candidate("steer", "ue@ue1", "servingCell", "1.4"), CATALOG)
            self.assertNotIn('"<one listed value>"', agents._user_prompt({}, agents._BASIC_SCHEMA))


if __name__ == "__main__":
    unittest.main()
