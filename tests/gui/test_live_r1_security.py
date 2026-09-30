"""The Live console's R1 transport security, and its refusals.

``R1Client`` has always refused to exist in secure mode without a TLS context
*and* an OAuth authorization hook.  ``LiveIntegration.r1_client()`` never
supplied either, so the only reachable Live R1 was ``insecure_dev`` over
loopback HTTP - the gap the first real deployment session hit, with
``R1Error: secure R1 requires TLS/mTLS context and OAuth authorization hook``.

What is asserted here is the whole of the fix and the whole of its blast radius:
a secure client is built from the deployment's *own pinned* credential
references; a reference that is absent, a placeholder, a non-file vocabulary or
an unreadable file is a refusal that names the key; the credential keys are not
- and must never become - runtime-overlayable; and the loopback development path
is untouched.

Hermetic: the only material is a throwaway CA and client leaf minted into a
temporary directory by the local ``openssl``.  Nothing is contacted.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from oran.rapp.gui_entry import LiveIntegration, RUNTIME_ENDPOINT_KEYS
from oran.rapp.headless import _endpoint_overlay
from oran.rapp.r1_client import OAUTH_SCHEME, R1Error
from oran.rapp.r1_security import (CLIENT_CERTIFICATE_KEY,
                                   CLIENT_PRIVATE_KEY_KEY,
                                   OAUTH_CREDENTIAL_KEY, R1_SECURITY_KEYS,
                                   R1Security, R1SecurityError, TRUSTSTORE_KEY,
                                   build_r1_security)

OPENSSL = shutil.which("openssl")
TOKEN = "session-issued-r1-access-token-value"

_CA_CONFIG = """[req]
distinguished_name = dn
prompt = no
x509_extensions = v3_ca
[dn]
CN = live-r1-security-test-ca
[v3_ca]
basicConstraints = critical,CA:TRUE,pathlen:0
keyUsage = critical,keyCertSign,cRLSign
"""

_CLIENT_CONFIG = """[req]
distinguished_name = dn
prompt = no
[dn]
CN = rapp-aic-r1-consumer
[v3_client]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = clientAuth
"""


def _openssl(args):
    subprocess.run([OPENSSL, *args], check=True, capture_output=True)


def mint_client_material(target: Path) -> dict:
    """Mint a CA and one client leaf; return the paths as ``file://`` references."""
    target.mkdir(parents=True, exist_ok=True)
    ca_key, ca_cert = target / "ca.key", target / "ca.crt"
    client_key, client_cert = target / "client.key", target / "client.crt"
    csr = target / "client.csr"
    ca_config, client_config = target / "ca.cnf", target / "client.cnf"
    ca_config.write_text(_CA_CONFIG, encoding="utf-8")
    client_config.write_text(_CLIENT_CONFIG, encoding="utf-8")
    _openssl(["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout",
              str(ca_key), "-out", str(ca_cert), "-days", "2", "-config",
              str(ca_config)])
    _openssl(["req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout",
              str(client_key), "-out", str(csr), "-config", str(client_config)])
    _openssl(["x509", "-req", "-in", str(csr), "-CA", str(ca_cert), "-CAkey",
              str(ca_key), "-CAcreateserial", "-out", str(client_cert), "-days",
              "2", "-extfile", str(client_config), "-extensions", "v3_client"])
    credential = target / "r1-oauth-credential"
    credential.write_text(TOKEN + "\n", encoding="utf-8")
    for path in (ca_key, client_key, credential):
        os.chmod(path, 0o600)
    return {
        TRUSTSTORE_KEY: ca_cert.as_uri(),
        CLIENT_CERTIFICATE_KEY: client_cert.as_uri(),
        CLIENT_PRIVATE_KEY_KEY: client_key.as_uri(),
        OAUTH_CREDENTIAL_KEY: credential.as_uri(),
    }


def secure_values(credentials=None) -> dict:
    """The values a secure R1 consumer reads, all HTTPS, all pinned."""
    values = {
        "r1.apiRoot": "https://upper-r1.invalid/r1",
        "r1.rAppId": "rapp-aic-oran-lab-001",
        "r1.dme.policyEvidencePushBaseUri": "https://upper-rapp.invalid/push",
    }
    values.update(credentials or {})
    return values


def integration(values, *, state_dir: Path, insecure_dev=False) -> LiveIntegration:
    """One ``LiveIntegration`` with no deployment machinery around it.

    The frozen dataclass is the composition root; building it directly is what
    keeps this about ``r1_client()`` and not about document loading, which
    ``test_live_composition`` already covers end to end.
    """
    return LiveIntegration(
        integration_path=str(state_dir / "integration-values.json"),
        document={"contractProfile": "oran-aic/1.0.0"},
        values=values, capability_manifest={},
        state_path=str(state_dir / "rapp-r1-state.json"),
        evidence_path=str(state_dir / "rapp-evidence.jsonl"),
        insecure_dev=insecure_dev)


class R1CredentialReferenceRefusals(unittest.TestCase):
    """Every way a deployment can fail to deliver R1 credentials, by name."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def resolvable(self) -> dict:
        """Four references that resolve to files, without minting real TLS material.

        Enough to exercise reference resolution on its own: the TLS build is
        covered by :class:`SecureR1ClientComposition`, which needs openssl.
        """
        references = {}
        for index, key in enumerate(R1_SECURITY_KEYS):
            path = self.root / f"material-{index}"
            path.write_text("placeholder material\n", encoding="utf-8")
            references[key] = path.as_uri()
        return references

    def test_each_absent_reference_is_refused_by_the_key_that_owes_it(self):
        for key in R1_SECURITY_KEYS:
            values = secure_values(self.resolvable())
            del values[key]
            with self.assertRaises(R1SecurityError) as caught:
                build_r1_security(values)
            self.assertIn(key, str(caught.exception))

    def test_the_deployment_sessions_unresolved_placeholder_is_refused(self):
        """``file://UNRESOLVED-upper-r1-oauth-credential`` is not a credential.

        This is the exact value the 2026-08-19 deployment session recorded for
        ``r1.https.oauthCredentialRef`` because no OAuth issuer existed to issue
        one.  A placeholder must fail closed rather than resolve to something.
        """
        values = secure_values(self.resolvable())
        values[OAUTH_CREDENTIAL_KEY] = "file://UNRESOLVED-upper-r1-oauth-credential"
        with self.assertRaises(R1SecurityError) as caught:
            build_r1_security(values)
        self.assertIn(OAUTH_CREDENTIAL_KEY, str(caught.exception))
        self.assertIn("no material has been issued", str(caught.exception))

    def test_material_committed_inside_this_tree_is_not_a_credential(self):
        """A key beside the code is a key every reader of the tree holds.

        The frozen reference vocabulary exists so credentials live outside the
        repository; a reference that points back into it is refused rather than
        loaded, which is the same reason the live-O1 profile's G-ID-07 refuses a
        packaged secret.
        """
        values = secure_values(self.resolvable())
        inside = Path(__file__).resolve()
        values[TRUSTSTORE_KEY] = inside.as_uri()
        with self.assertRaisesRegex(R1SecurityError, "inside this source tree"):
            build_r1_security(values)

    def test_a_reference_that_names_no_file_is_refused(self):
        values = secure_values(self.resolvable())
        values[TRUSTSTORE_KEY] = (self.root / "never-issued.crt").as_uri()
        with self.assertRaisesRegex(R1SecurityError, "does not resolve to a regular file"):
            build_r1_security(values)

    def test_a_directory_is_not_credential_material(self):
        values = secure_values(self.resolvable())
        values[CLIENT_CERTIFICATE_KEY] = self.root.as_uri()
        with self.assertRaisesRegex(R1SecurityError, "does not resolve to a regular file"):
            build_r1_security(values)

    def test_secret_store_vocabularies_are_refused_rather_than_guessed(self):
        """``env:``/``vault:``/``k8s:``/``keychain:`` resolution is not invented.

        The frozen values schema permits all five reference schemes, but only
        ``file://`` carries its own location.  Guessing where the other four
        live - an environment variable's contents? a logical key in some map? -
        would be inventing trust material, so each is refused with its scheme
        named.
        """
        for reference in ("env://upper/oauth/r1-client",
                          "vault://kv/aic/r1-client",
                          "k8s://aic/r1-client",
                          "keychain://aic/r1-client"):
            values = secure_values(self.resolvable())
            values[OAUTH_CREDENTIAL_KEY] = reference
            with self.assertRaises(R1SecurityError) as caught:
                build_r1_security(values)
            scheme = reference.split(":", 1)[0]
            self.assertIn(f"{scheme}://", str(caught.exception))

    def test_a_reference_outside_the_frozen_vocabulary_is_refused(self):
        values = secure_values(self.resolvable())
        values[TRUSTSTORE_KEY] = "https://upper-r1.invalid/truststore.pem"
        with self.assertRaisesRegex(
                R1SecurityError, "outside the frozen reference vocabulary"):
            build_r1_security(values)

    def test_material_on_another_host_has_not_been_delivered_here(self):
        values = secure_values(self.resolvable())
        values[TRUSTSTORE_KEY] = "file://vault.example.invalid/etc/ca.crt"
        with self.assertRaisesRegex(R1SecurityError, "remote authority"):
            build_r1_security(values)

    def test_the_episode_entry_uses_the_same_builder_as_the_console(self):
        """One way of establishing R1 trust, not one per caller.

        The console's read-only client and the authoritative episode entry are
        the same composition against the same deployment; if only one of them
        could go secure, a Live session could bind and then fail at submit, and
        the two would be two transports with two trust stories.  The allowlist
        is shared by re-export for exactly this reason - so is this builder.
        """
        from oran.rapp import headless, r1_security

        self.assertIs(r1_security.build_r1_security, headless.build_r1_security)

    def test_credential_keys_are_not_and_must_not_become_overlayable(self):
        """The overlay allowlist stays endpoints-only.

        An overlay that could replace a truststore could point a fully
        digest-validated deployment at another R1 and still pass every check the
        document makes, so the fix reads these keys from the pinned document and
        this gate keeps the allowlist from growing to include them.
        """
        for key in R1_SECURITY_KEYS:
            self.assertNotIn(key, RUNTIME_ENDPOINT_KEYS)
            with self.assertRaisesRegex(ValueError, key):
                _endpoint_overlay({key: "file:///tmp/forged"})


@unittest.skipUnless(OPENSSL, "openssl is required to mint throwaway TLS material")
class SecureR1ClientComposition(unittest.TestCase):
    """A secure console client, built from the deployment's own references."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.credentials = mint_client_material(cls.root / "secrets")
        cls.state_dir = cls.root / "console-state"
        cls.state_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_build_r1_security_yields_a_verified_mutual_context(self):
        security = build_r1_security(secure_values(self.credentials))
        self.assertIsInstance(security, R1Security)
        import ssl

        self.assertTrue(security.ssl_context.check_hostname)
        self.assertEqual(ssl.CERT_REQUIRED, security.ssl_context.verify_mode)
        self.assertGreaterEqual(security.ssl_context.minimum_version,
                                ssl.TLSVersion.TLSv1_2)
        subjects = {tuple(pair for rdn in cert["subject"] for pair in rdn)
                    for cert in security.ssl_context.get_ca_certs()}
        self.assertIn((("commonName", "live-r1-security-test-ca"),), subjects)

    def test_the_authorization_hook_returns_the_issued_token(self):
        security = build_r1_security(secure_values(self.credentials))
        header = security.oauth_header_provider(
            "GET", "https://upper-r1.invalid/r1/x", "1.0.0")
        self.assertEqual(OAUTH_SCHEME, header.split(" ", 1)[0])
        self.assertEqual(TOKEN, header.split(" ", 1)[1])

    def test_the_hook_rereads_the_credential_so_rotation_takes_effect(self):
        security = build_r1_security(secure_values(self.credentials))
        credential = Path(self.credentials[OAUTH_CREDENTIAL_KEY][len("file://"):])
        original = credential.read_text(encoding="utf-8")
        self.addCleanup(credential.write_text, original, encoding="utf-8")
        credential.write_text("rotated-token\n", encoding="utf-8")
        header = security.oauth_header_provider(
            "GET", "https://upper-r1.invalid/r1/x", "1.0.0")
        self.assertEqual("rotated-token", header.split(" ", 1)[1])

    def test_an_emptied_credential_is_a_refusal_not_a_bare_scheme(self):
        security = build_r1_security(secure_values(self.credentials))
        credential = Path(self.credentials[OAUTH_CREDENTIAL_KEY][len("file://"):])
        original = credential.read_text(encoding="utf-8")
        self.addCleanup(credential.write_text, original, encoding="utf-8")
        credential.write_text("   \n", encoding="utf-8")
        with self.assertRaisesRegex(R1SecurityError, "credential is empty"):
            security.oauth_header_provider("GET", "https://x.invalid", "1.0.0")

    def test_the_secure_console_client_is_built_instead_of_refused(self):
        client = integration(secure_values(self.credentials),
                             state_dir=self.state_dir).r1_client()
        self.assertFalse(client.insecure_dev)
        self.assertIsNotNone(client.transport.ssl_context)
        self.assertIsNotNone(client.oauth_header_provider)

    def test_a_secure_deployment_without_credentials_still_fails_closed(self):
        """The refusal moves from unexplained to named; it does not disappear."""
        bare = integration(secure_values(), state_dir=self.state_dir)
        with self.assertRaises(R1SecurityError) as caught:
            bare.r1_client()
        self.assertIn(TRUSTSTORE_KEY, str(caught.exception))
        # And the underlying client refusal is still there for anyone who builds
        # one without material, which is what makes this fail-closed by
        # construction rather than by this call site remembering to check.
        with self.assertRaisesRegex(R1Error, "secure R1 requires"):
            from oran.rapp.r1_client import R1Client

            R1Client(api_root="https://upper-r1.invalid/r1",
                     r_app_id="rapp-aic-oran-lab-001",
                     policy_evidence_push_base_uri="https://upper-rapp.invalid/push",
                     state_path=str(self.state_dir / "unused.json"))

    def test_no_secret_value_reaches_a_message_or_the_recorded_references(self):
        security = build_r1_security(secure_values(self.credentials))
        self.assertEqual(dict(self.credentials), dict(security.references))
        self.assertNotIn(TOKEN, json.dumps(dict(security.references)))
        values = secure_values(self.credentials)
        values[OAUTH_CREDENTIAL_KEY] = "file://UNRESOLVED-upper-r1-oauth-credential"
        with self.assertRaises(R1SecurityError) as caught:
            build_r1_security(values)
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_the_episode_entry_hands_the_built_security_to_the_client(self):
        """``run_once`` reaches ``R1Client`` with a real context and a real hook.

        The console's client is asserted above; this is the write path.  Every
        collaborator except the security builder is replaced, so what is being
        observed is exactly the wiring: which objects the episode entry passes
        to the transport it is about to submit a policy through.
        """
        from unittest import mock

        from oran.rapp import headless
        # The deployment-owned policy values, taken from the contract's own
        # golden objects rather than invented here.
        from tests.gui.test_live_composition import _policy_context

        values = secure_values(self.credentials)
        values.update({
            "backend.capabilityManifestPath": "capability.json",
            "backend.capabilityManifestSha256": "0" * 64,
        })
        with mock.patch.object(headless, "load_integration_values",
                               return_value=dict(values)), \
             mock.patch.object(headless, "_load_pinned_json",
                               return_value={"nearRtRicId": "ric-1"}), \
             mock.patch.object(headless, "validate"), \
             mock.patch.object(headless, "resolve_binding", return_value=None), \
             mock.patch.object(headless, "R1Client") as client, \
             mock.patch.object(headless, "CombinedAssurance"), \
             mock.patch.object(headless, "EvidenceLedger"), \
             mock.patch("oran.rapp.coordinator_adapter.RAppCoordinatorAdapter") as adapter:
            adapter.return_value.process_intent.return_value = {
                "terminal_outcome": "pending_not_admitted"}
            headless.run_once(
                integration_path=str(self.root / "values.json"),
                request={
                    "statePath": str(self.state_dir / "episode-state.json"),
                    "evidencePath": str(self.state_dir / "episode-evidence.jsonl"),
                    "intentText": "keep UE downlink throughput above 1 Mbps",
                    "policyContext": _policy_context(),
                    "identifiers": {"run_id": "run-secure-r1"},
                })
        kwargs = client.call_args.kwargs
        import ssl

        self.assertIsInstance(kwargs["ssl_context"], ssl.SSLContext)
        self.assertEqual(ssl.CERT_REQUIRED, kwargs["ssl_context"].verify_mode)
        header = kwargs["oauth_header_provider"](
            "POST", values["r1.apiRoot"], "1.0.0")
        self.assertEqual(TOKEN, header.split(" ", 1)[1])
        self.assertFalse(kwargs["insecure_dev"])

    def test_the_loopback_development_path_needs_no_credential_material(self):
        loopback = integration(
            {"r1.apiRoot": "http://127.0.0.1:18080/r1",
             "r1.rAppId": "rapp-aic-oran-lab-001",
             "r1.dme.policyEvidencePushBaseUri": "http://127.0.0.1:18080/push"},
            state_dir=self.state_dir, insecure_dev=True)
        client = loopback.r1_client()
        self.assertTrue(client.insecure_dev)
        self.assertIsNone(client.transport.ssl_context)
        self.assertIsNone(client.oauth_header_provider)


if __name__ == "__main__":
    unittest.main()
