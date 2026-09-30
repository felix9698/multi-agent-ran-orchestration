"""Hermetic coverage for final-merge integration values loading."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from oran.contract.integration_values import IntegrationValuesError, SCHEMA_NAME, load_integration_values
from oran.contract.validator import ContractValidator


class IntegrationValuesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.validator = ContractValidator()
        cls.required = cls.validator.schema(SCHEMA_NAME)["properties"]["values"]["required"]

    def valid_document(self) -> dict[str, object]:
        values: dict[str, object] = {}
        for key in self.required:
            values[key] = self.value_for(key)
        return {
            "schemaVersion": "oran-aic-integration-values/1.0.0",
            "contractProfile": "oran-aic/1.0.0",
            "deploymentMode": "MERGED",
            "bundleManifestJcsSha256": "6f9908ca9cee29ca5fa7b629f4daa0f502b5b8ce244519a76b9c88c6c1710ce3",
            "values": values,
        }

    @staticmethod
    def value_for(key: str) -> object:
        if key.endswith("Sha256"):
            return "a" * 64
        if key.endswith("Path"):
            return "artifacts/value.json"
        if key.endswith("Ref"):
            return "env://integration-value"
        if key == "o1.netconf.endpoint":
            return "ssh://netconf.example.test:830"
        if key == "o1.sftp.allowedAuthorities":
            return ["sftp.example.test:22"]
        if key.endswith("Dn"):
            return "ManagedElement=example"
        if key == "o1.fileDataReporting.mnsVersion":
            return "v1"
        if key.endswith("ClientId") or key == "r1.rAppId":
            return "client-1"
        if key.endswith("TokenEndpoint") or key.endswith("managedObjectUri"):
            return "https://auth.example.test/token"
        if key.endswith("apiRoot") or key.endswith("BaseUri") or key.endswith("Destination") or key.endswith("mnsRoot") or key.endswith("consumerReference"):
            return "https://service.example.test/api"
        return "integration-value"

    def load(self, document: dict[str, object]) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "integration-values.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            return load_integration_values(path, validator=self.validator)

    def test_populated_exact_80_key_document_loads(self):
        document = self.valid_document()
        self.assertEqual(len(document["values"]), 80)
        self.assertEqual(self.load(document), document)

    def test_missing_or_extra_value_key_is_rejected(self):
        missing = self.valid_document()
        missing["values"].pop(self.required[0])
        with self.assertRaises(IntegrationValuesError):
            self.load(missing)

        extra = self.valid_document()
        extra["values"]["unexpected.value"] = "forbidden"
        with self.assertRaises(IntegrationValuesError):
            self.load(extra)

    def test_all_permitted_secret_reference_schemes_load(self):
        for scheme in ("env", "file", "vault", "k8s", "keychain"):
            with self.subTest(scheme=scheme):
                document = self.valid_document()
                document["values"]["r1.https.truststoreRef"] = f"{scheme}://integration-value"
                self.load(document)

    def test_invalid_secret_references_are_rejected(self):
        for reference in ("https://secret.example.test", "env://contains whitespace", "env://password=value", "plaintext-secret"):
            with self.subTest(reference=reference):
                document = self.valid_document()
                document["values"]["r1.https.truststoreRef"] = reference
                with self.assertRaises(IntegrationValuesError):
                    self.load(document)

    def test_sftp_authority_port_boundaries(self):
        for port in (1, 80, 65535):
            with self.subTest(port=port):
                document = self.valid_document()
                document["values"]["o1.sftp.allowedAuthorities"] = [f"sftp.example.test:{port}"]
                self.load(document)
        for port in (0, 65536):
            with self.subTest(port=port):
                document = self.valid_document()
                document["values"]["o1.sftp.allowedAuthorities"] = [f"sftp.example.test:{port}"]
                with self.assertRaises(IntegrationValuesError):
                    self.load(document)
