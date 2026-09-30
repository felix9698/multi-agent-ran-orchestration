"""Hermetic gates for the vendored O-RAN AIC contract kernel."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jsonschema import ValidationError

from oran.contract.digests import (
    PINNED_AUTHORITY_BUNDLE_MANIFEST_SHA256,
    PINNED_JCS_DIGESTS,
    ContractIntegrityError,
    contract_root,
    selected_contract_root,
    verify_contract_authority,
    verify_contract_integrity,
)
from oran.contract.jcs import canonicalize, canonicalize_bytes
from oran.contract.validator import ContractValidator


class ContractDigestTest(unittest.TestCase):
    def test_vendored_handoff_is_complete_and_pinned(self):
        verify_contract_integrity()

    def test_one_byte_bundle_mutation_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            copied_root = Path(directory) / "oran-aic"
            shutil.copytree(contract_root(), copied_root)
            target = copied_root / "shared-contract-bundle" / "golden" / "o1" / "valid-prb.xml"
            payload = bytearray(target.read_bytes())
            payload[0] ^= 1
            target.write_bytes(payload)
            with self.assertRaises(ContractIntegrityError):
                verify_contract_integrity(copied_root, Path(directory) / "no-source")

    def test_default_runtime_authority_is_corrected_1_0_1(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ORAN_CONTRACT_AUTHORITY", None)
            self.assertEqual("1.0.1", selected_contract_root().name)

    def test_explicit_historical_authority_remains_fully_pinned(self):
        with tempfile.TemporaryDirectory() as directory:
            copied_root = Path(directory) / "1.0.0"
            shutil.copytree(contract_root(), copied_root)
            target = copied_root / "shared-contract-bundle" / "golden" / "o1" / "valid-prb.xml"
            target.write_bytes(target.read_bytes() + b"mutation")
            with self.assertRaises(ContractIntegrityError):
                verify_contract_authority(copied_root)

    def test_selected_1_0_1_never_falls_back_to_1_0_0(self):
        corrected = contract_root("1.0.1")
        with tempfile.TemporaryDirectory() as directory:
            copied_root = Path(directory) / "1.0.1"
            shutil.copytree(corrected, copied_root)
            (copied_root / "shared-contract-bundle" /
             "scenario-runner-contract.1.0.1.json").unlink()
            with self.assertRaisesRegex(
                    ContractIntegrityError,
                    r"scenario-runner-contract\.1\.0\.1\.json") as caught:
                verify_contract_authority(copied_root)
            self.assertNotIn("scenario-runner-contract.1.0.0.json", str(caught.exception))

    def test_incorrect_1_0_1_manifest_pin_fails_closed(self):
        corrected = contract_root("1.0.1")
        with patch.dict(PINNED_AUTHORITY_BUNDLE_MANIFEST_SHA256,
                        {"1.0.1": "0" * 64}, clear=False):
            with self.assertRaisesRegex(ContractIntegrityError,
                                        "pinned bundle manifest byte SHA-256 mismatch"):
                verify_contract_authority(corrected)

    def test_none_1_0_1_manifest_pin_fails_closed(self):
        corrected = contract_root("1.0.1")
        with patch.dict(PINNED_AUTHORITY_BUNDLE_MANIFEST_SHA256,
                        {"1.0.1": None}, clear=False):
            with self.assertRaisesRegex(ContractIntegrityError,
                                        "invalid pinned bundle manifest SHA-256"):
                verify_contract_authority(corrected)

    def test_missing_1_0_1_manifest_pin_fails_closed(self):
        corrected = contract_root("1.0.1")
        with patch.dict(PINNED_AUTHORITY_BUNDLE_MANIFEST_SHA256, {}, clear=True):
            with self.assertRaisesRegex(ContractIntegrityError,
                                        "authority digest configuration is absent"):
                verify_contract_authority(corrected)

    def test_issued_1_0_1_authority_manifest_is_present_and_runtime_pinned(self):
        corrected = contract_root("1.0.1")
        manifest = corrected / "shared-contract-bundle" / "bundle-manifest.1.0.1.json"
        self.assertTrue(manifest.is_file())
        actual = hashlib.sha256(manifest.read_bytes()).hexdigest()
        self.assertEqual(actual, PINNED_AUTHORITY_BUNDLE_MANIFEST_SHA256["1.0.1"])
        self.assertEqual(corrected, verify_contract_authority(corrected))

    def test_1_0_1_manifest_pin_transitively_rejects_member_mutation(self):
        corrected = contract_root("1.0.1")
        with tempfile.TemporaryDirectory() as directory:
            authority = Path(directory) / "1.0.1"
            shutil.copytree(corrected, authority)
            bundle = authority / "shared-contract-bundle"
            manifest_path = bundle / "bundle-manifest.1.0.1.json"
            entries = []
            for path in sorted(bundle.rglob("*")):
                if path.is_file() and path != manifest_path:
                    payload = path.read_bytes()
                    entries.append({
                        "path": path.relative_to(bundle).as_posix(),
                        "byteCount": len(payload),
                        "byteSha256": hashlib.sha256(payload).hexdigest(),
                    })
            manifest_path.write_text(json.dumps({"files": entries}), encoding="utf-8")
            pin = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            with patch.dict(PINNED_AUTHORITY_BUNDLE_MANIFEST_SHA256,
                            {"1.0.1": pin}, clear=False):
                verify_contract_authority(authority)
                catalog = bundle / "scenario-catalog.1.0.1.json"
                catalog.write_bytes(catalog.read_bytes() + b"\n")
                with self.assertRaisesRegex(ContractIntegrityError,
                                            "manifest-listed bundle file mismatch"):
                    verify_contract_authority(authority)

    def test_corrected_authority_rejects_invalid_schema_and_broken_fixture_reference(self):
        corrected = contract_root("1.0.1")
        with tempfile.TemporaryDirectory() as directory:
            invalid_schema_root = Path(directory) / "invalid" / "1.0.1"
            shutil.copytree(corrected, invalid_schema_root)
            schema_path = (invalid_schema_root / "shared-contract-bundle" /
                           "deployment-test-vector.1.0.0.schema.json")
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            schema["type"] = "not-a-json-schema-type"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            with self.assertRaisesRegex(ContractIntegrityError,
                                        "invalid contract schema"):
                verify_contract_authority(invalid_schema_root)

            broken_ref_root = Path(directory) / "broken-ref" / "1.0.1"
            shutil.copytree(corrected, broken_ref_root)
            catalog_path = (broken_ref_root / "shared-contract-bundle" /
                            "scenario-catalog.1.0.1.json")
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            catalog["fixtureRegistry"]["validPrbXml"] = "missing.xml"
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            with self.assertRaisesRegex(ContractIntegrityError,
                                        "missing contract reference"):
                verify_contract_authority(broken_ref_root)

    def test_corrected_authority_rejects_broken_catalog_json_pointer(self):
        corrected = contract_root("1.0.1")
        with tempfile.TemporaryDirectory() as directory:
            copied_root = Path(directory) / "1.0.1"
            shutil.copytree(corrected, copied_root)
            catalog_path = (copied_root / "shared-contract-bundle" /
                            "scenario-catalog.1.0.1.json")
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            catalog["standardInitialStates"]["INSTALL_AIC_POLICY_TYPE"][
                "definitionRef"] = (
                    "scenario-runner-contract.1.0.1.json#/initialStates/DOES_NOT_EXIST")
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            with self.assertRaisesRegex(ContractIntegrityError,
                                        "unresolved JSON pointer"):
                verify_contract_authority(copied_root)


class JcsTest(unittest.TestCase):
    def test_reproduces_all_section_19_pinned_digests(self):
        bundle = contract_root() / "shared-contract-bundle"
        for relative, expected in PINNED_JCS_DIGESTS.items():
            with self.subTest(relative=relative):
                value = json.loads((bundle / relative).read_text(encoding="utf-8"))
                self.assertEqual(hashlib.sha256(canonicalize_bytes(value)).hexdigest(), expected)

    def test_ecmascript_shortest_round_trip_float_edges(self):
        expected = {4310.2: "4310.2", 2980.5: "2980.5", 0.0: "0", 1e21: "1e+21", 1e-6: "0.000001", 1e-7: "1e-7"}
        for value, rendered in expected.items():
            self.assertEqual(canonicalize(value), rendered)
        self.assertEqual(canonicalize(-0.0), "0")

    def test_object_keys_sort_by_utf16_code_units(self):
        self.assertEqual(canonicalize({"\ue000": 2, "\U00010000": 1}), '{"𐀀":1,"":2}')


class ContractSchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.validator = ContractValidator()
        golden_path = contract_root() / "shared-contract-bundle" / "golden" / "golden-vectors.1.0.0.json"
        cls.golden = json.loads(golden_path.read_text(encoding="utf-8"))["canonicalObjects"]

    def test_golden_objects_validate(self):
        mapping = {
            "policy": "AIC_UECellSteering_1.0.0.policy.schema.json",
            "appliedVerifiedStatus": "AIC_UECellSteering_1.0.0.status.schema.json",
            "noActionStatus": "AIC_UECellSteering_1.0.0.status.schema.json",
            "afterEvidence": "aic.policy-evidence.1.0.0.schema.json",
            "afterEvidenceCell2": "aic.policy-evidence.1.0.0.schema.json",
            "capabilityManifest": "aic.ran-capability.1.0.0.schema.json",
        }
        for name, schema in mapping.items():
            with self.subTest(name=name):
                self.validator.validate(schema, self.golden[name])

    def test_filename_urn_and_component_alias_resolve_to_one_schema(self):
        filename = "AIC_UECellSteering_1.0.0.policy.schema.json"
        urn = "urn:oran-aic:schema:AIC_UECellSteering:policy:1.0.0"
        alias = "AIC_UECellSteering_1.0.0.policy"
        self.assertIs(self.validator.schema(filename), self.validator.schema(urn))
        self.assertIs(self.validator.schema(filename), self.validator.schema(alias))

    def test_unknown_field_uuid_datetime_and_offset_are_rejected(self):
        policy = copy.deepcopy(self.golden["policy"])
        policy["unknown"] = True
        with self.assertRaises(ValidationError):
            self.validator.validate("AIC_UECellSteering_1.0.0.policy.schema.json", policy)
        bad_uuid = copy.deepcopy(self.golden["policy"])
        bad_uuid["trace"]["intentId"] = "not-a-uuid"
        with self.assertRaises(ValidationError):
            self.validator.validate("AIC_UECellSteering_1.0.0.policy.schema.json", bad_uuid)
        bad_datetime = copy.deepcopy(self.golden["policy"])
        bad_datetime["validity"]["notBefore"] = "2026-08-04T00:00:00+09:00"
        with self.assertRaises(ValidationError):
            self.validator.validate("AIC_UECellSteering_1.0.0.policy.schema.json", bad_datetime)

    def test_pin_to_cell_forbids_improvement_threshold(self):
        policy = copy.deepcopy(self.golden["policy"])
        policy["steeringObjective"]["kind"] = "PIN_TO_CELL"
        policy["steeringObjective"]["actionEnvelope"]["allowedCells"] = policy["steeringObjective"]["actionEnvelope"]["allowedCells"][:1]
        with self.assertRaises(ValidationError):
            self.validator.validate("AIC_UECellSteering_1.0.0.policy.schema.json", policy)
