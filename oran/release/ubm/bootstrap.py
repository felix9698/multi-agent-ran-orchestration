"""Runtime bootstrap: TLS material, secret map, integration values, startup.

None of these are release bytes.  ``bin/ubm bootstrap`` writes them into an
operator-chosen runtime directory so a deployment can start without a secret
value ever entering the archive: only reference URIs are recorded anywhere.

The integration-values document is generated from the *frozen schema's own key
list* and the deployment vector, so it can never drift from the 80 keys the
contract requires, and every credential slot is a reference from the vocabulary
``integration-values.1.0.0.schema.json#/$defs/SecretReference`` declares.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from oran.conformance.contracts import ContractBundle

from .tls import bootstrap_loopback_tls

BUNDLE_MANIFEST_JCS_SHA256 = (
    "6f9908ca9cee29ca5fa7b629f4daa0f502b5b8ce244519a76b9c88c6c1710ce3")

#: TLS references the frozen deployment binding assigns to upper material.
SERVER_REFERENCES = {
    "r1.https.pushServerCertificateRef": "env://ubm/tls/r1-server-certificate",
    "r1.https.pushServerPrivateKeyRef": "env://ubm/tls/r1-server-private-key",
    "a1.https.notificationReceiverServerCertificateRef":
        "env://ubm/tls/rapp-server-certificate",
    "a1.https.notificationReceiverServerPrivateKeyRef":
        "env://ubm/tls/rapp-server-private-key",
    "o1.https.providerServerCertificateRef":
        "env://ubm/tls/o1-provider-server-certificate",
    "o1.https.providerServerPrivateKeyRef":
        "env://ubm/tls/o1-provider-server-private-key",
    "o1.https.notificationReceiverServerCertificateRef":
        "env://ubm/tls/o1-consumer-server-certificate",
    "o1.https.notificationReceiverServerPrivateKeyRef":
        "env://ubm/tls/o1-consumer-server-private-key",
    "r1.https.truststoreRef": "env://ubm/tls/client-truststore",
    "a1.https.truststoreRef": "env://ubm/tls/client-truststore",
    "o1.https.truststoreRef": "env://ubm/tls/client-truststore",
}


def integration_values(vector: Mapping[str, Any], *, bundle: ContractBundle,
                       vector_path: Path, vector_sha256: str) -> dict[str, Any]:
    schema = bundle.schema("integration-values.1.0.0.schema.json")
    required = list(schema["properties"]["values"]["required"])
    file_data = vector["o1"]["fileDataReporting"]
    known: dict[str, Any] = {
        "deployment.testVectorPath": str(vector_path),
        "deployment.testVectorSha256": vector_sha256,
        "r1.apiRoot": vector["r1"]["apiRoot"],
        "r1.rAppId": vector["r1"]["rAppId"],
        "r1.dme.policyEvidencePushBaseUri":
            vector["r1"]["dme"]["policyEvidencePushBaseUri"],
        "a1.apiRoot": vector["a1"]["apiRoot"],
        "a1.notificationDestination": vector["a1"]["statusCallbackRoot"],
        "o1.netconf.endpoint": vector["o1"]["netconf"]["endpoint"],
        "o1.fileDataReporting.mnsRoot": file_data["mnsRoot"],
        "o1.fileDataReporting.mnsVersion": file_data["mnsVersion"],
        "o1.fileDataReporting.mnsAgentDn": file_data["mnsAgentDn"],
        "o1.fileDataReporting.consumerReference": file_data["consumerReference"],
        "o1.sftp.allowedAuthorities": list(vector["o1"]["sftp"]["allowedAuthorities"]),
        "o1.perfMetricJob.managedObjectDn":
            vector["o1"]["perfMetricJob"]["managedObjectDn"],
        "o1.perfMetricJob.managedObjectUri":
            vector["o1"]["perfMetricJob"]["managedObjectUri"],
        # R-2: the two documents govern disjoint reference vocabularies.  The
        # vector keeps the lower's secret:// values byte-for-byte; the
        # integration-values file must use env://, so the NETCONF/SFTP slots -
        # unused by all four bilateral scenarios - stay declared-but-unbound
        # here rather than being copied across vocabularies.
        "r1.https.oauthCredentialRef": "env://ubm/oauth/r1-client",
    }
    known.update(SERVER_REFERENCES)
    properties = schema["properties"]["values"]["properties"]
    values: dict[str, Any] = {}
    for key in required:
        if key in known:
            values[key] = known[key]
            continue
        values[key] = _placeholder(key, properties[key], vector)
    return {
        "schemaVersion": "oran-aic-integration-values/1.0.0",
        "contractProfile": "oran-aic/1.0.0",
        "deploymentMode": "MERGED",
        "bundleManifestJcsSha256": BUNDLE_MANIFEST_JCS_SHA256,
        "values": values,
    }


def _placeholder(key: str, definition: Mapping[str, Any],
                 vector: Mapping[str, Any]) -> Any:
    reference = str(definition.get("$ref", ""))
    slug = key.replace(".", "-").lower()
    if reference.endswith("SecretReference"):
        # Unused by the four bilateral scenarios; declared as a reference only.
        return "env://ubm/unused/" + slug
    if reference.endswith("Sha256"):
        return hashlib.sha256(key.encode("utf-8")).hexdigest()
    if reference.endswith("ArtifactPath"):
        return "unused/" + slug + ".json"
    if reference.endswith("HttpsUri") or reference.endswith("HttpsBase"):
        return vector["r1"]["apiRoot"].rstrip("/") + "/ubm/v1/unused/" + slug
    if reference.endswith("Identifier"):
        return "ubm-unused-" + slug
    return "ubm-unused-" + slug


def bootstrap_runtime(*, out_dir: Path, vector_path: Path,
                      contract_authority: Path, run_id: str,
                      integration_control_surface: str | None = None,
                      release_manifest_path: Path | None = None,
                      ) -> dict[str, str]:
    """Write TLS material, secret map, integration values and startup.json."""
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    bundle = ContractBundle(Path(contract_authority))
    raw = Path(vector_path).read_bytes()
    vector = json.loads(raw.decode("utf-8"))
    vector_sha256 = hashlib.sha256(raw).hexdigest()

    mapping = dict(bootstrap_loopback_tls(target / "tls"))
    oauth_path = target / "tls" / "r1-oauth-token"
    oauth_path.write_text(_deterministic_token(run_id) + "\n", encoding="utf-8")
    oauth_path.chmod(0o600)
    mapping["env://ubm/oauth/r1-client"] = str(oauth_path)
    secret_map = target / "secret-map.json"
    secret_map.write_text(json.dumps(mapping, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")
    secret_map.chmod(0o600)

    document = integration_values(
        vector, bundle=bundle, vector_path=Path(vector_path).resolve(),
        vector_sha256=vector_sha256)
    values_path = target / "integration-values.json"
    values_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")

    startup = {
        "profile": "bilateral-mock",
        "vectorPath": str(Path(vector_path).resolve()),
        "vectorSha256": vector_sha256,
        "integrationValuesPath": str(values_path.resolve()),
        "contractAuthority": str(Path(contract_authority).resolve()),
        "stateDir": str((target / "state").resolve()),
        "secretMapPath": str(secret_map.resolve()),
        "releaseManifestPath": str(
            (Path(release_manifest_path) if release_manifest_path
             else target / "RELEASE-MANIFEST.json").resolve()),
        "runId": run_id,
    }
    if integration_control_surface:
        startup["integrationControlSurface"] = integration_control_surface
    startup_path = target / "startup.json"
    startup_path.write_text(json.dumps(startup, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8")
    return {
        "startup": str(startup_path),
        "secretMap": str(secret_map),
        "integrationValues": str(values_path),
        "vectorSha256": vector_sha256,
    }


def _deterministic_token(run_id: str) -> str:
    return hashlib.sha256(("ubm-oauth:" + str(run_id)).encode("utf-8")).hexdigest()
