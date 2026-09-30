"""The vendored bundle is meta-checked once per root, not once per R1 request."""

import os
import unittest
from unittest.mock import patch

from oran.contract import validator
from oran.contract.digests import selected_contract_root
from oran.rapp.contract_support import load_schema


class TheBundleIsCheckedOncePerRoot(unittest.TestCase):
    def test_repeated_requests_share_one_validator(self):
        self.assertIs(validator.default_validator(), validator.default_validator())
        with patch.object(validator.ContractValidator, "__init__",
                          side_effect=AssertionError("bundle rebuilt")):
            load_schema("AIC_UECellSteering_1.0.0.policy")

    def test_another_authority_gets_its_own_validator(self):
        current = validator.default_validator()
        other = selected_contract_root().parent / "1.0.0"
        if not (other / "shared-contract-bundle").is_dir():
            self.skipTest("no second authority vendored")
        with patch.dict(os.environ, {"ORAN_CONTRACT_AUTHORITY": str(other)}):
            self.assertIsNot(validator.default_validator(), current)

    def test_a_returned_schema_cannot_poison_the_cache(self):
        load_schema("AIC_UECellSteering_1.0.0.policy")["poisoned"] = True
        self.assertNotIn("poisoned", load_schema("AIC_UECellSteering_1.0.0.policy"))


if __name__ == "__main__":
    unittest.main()
