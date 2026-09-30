"""Real loopback TLS termination for the bilateral profile (D-3).

Certificates and keys are never release bytes.  They are addressed only by the
reference vocabularies the frozen documents already declare - ``secret://``,
``vault://`` and ``k8s-secret://`` in the deployment vector, ``env://``,
``file://``, ``vault://``, ``k8s://`` and ``keychain://`` in integration-values -
and a start-time secret map resolves a reference to a path outside the release.
Resolved paths and material are never logged and never captured.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
from pathlib import Path
from typing import Mapping

VECTOR_REFERENCE = re.compile(r"^(secret|vault|k8s-secret)://")
INTEGRATION_REFERENCE = re.compile(r"^(?:env|file|vault|k8s|keychain)://")
ANY_REFERENCE = re.compile(r"^(secret|vault|k8s-secret|env|file|k8s|keychain)://")

MINIMUM_TLS_VERSION = ssl.TLSVersion.TLSv1_2


class TlsConfigurationError(RuntimeError):
    """A TLS reference is unknown, unresolvable or outside the vocabulary."""


class SecretResolver:
    """Maps declared reference URIs to filesystem paths supplied at start."""

    def __init__(self, mapping: Mapping[str, str]) -> None:
        resolved: dict[str, Path] = {}
        for reference, location in dict(mapping).items():
            text = str(reference)
            if not ANY_REFERENCE.match(text):
                raise TlsConfigurationError(
                    "secret map key is not a declared reference URI")
            resolved[text] = Path(str(location))
        self._mapping = resolved

    @classmethod
    def from_file(cls, path: Path) -> "SecretResolver":
        target = Path(path)
        try:
            document = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TlsConfigurationError("cannot read the secret map") from exc
        if not isinstance(document, dict):
            raise TlsConfigurationError("secret map must be one JSON object")
        base = target.resolve().parent
        mapping = {}
        for reference, location in document.items():
            candidate = Path(str(location))
            mapping[reference] = str(
                candidate if candidate.is_absolute() else base / candidate)
        return cls(mapping)

    def resolve_path(self, reference: str) -> Path:
        try:
            target = self._mapping[str(reference)]
        except KeyError:
            raise TlsConfigurationError(
                "secret map has no entry for a required reference") from None
        if not target.is_file():
            raise TlsConfigurationError(
                "secret map entry does not address a regular file")
        return target

    def references(self) -> tuple[str, ...]:
        return tuple(sorted(self._mapping))

    def has(self, reference: str) -> bool:
        return str(reference) in self._mapping


def build_server_ssl_context(*, certificate_ref: str, private_key_ref: str,
                             truststore_ref: str | None,
                             resolver: SecretResolver) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = MINIMUM_TLS_VERSION
    context.load_cert_chain(
        certfile=str(resolver.resolve_path(certificate_ref)),
        keyfile=str(resolver.resolve_path(private_key_ref)))
    if truststore_ref is not None and resolver.has(truststore_ref):
        context.load_verify_locations(
            cafile=str(resolver.resolve_path(truststore_ref)))
    context.verify_mode = ssl.CERT_NONE
    return context


def build_client_ssl_context(*, truststore_ref: str, resolver: SecretResolver,
                             client_certificate_ref: str | None = None,
                             client_key_ref: str | None = None) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = MINIMUM_TLS_VERSION
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=str(resolver.resolve_path(truststore_ref)))
    if client_certificate_ref is not None and client_key_ref is not None:
        context.load_cert_chain(
            certfile=str(resolver.resolve_path(client_certificate_ref)),
            keyfile=str(resolver.resolve_path(client_key_ref)))
    return context


_OPENSSL_CONFIG = """[req]
distinguished_name = dn
prompt = no
x509_extensions = v3_ca
[dn]
CN = upper-bilateral-mock-loopback-ca
[v3_ca]
basicConstraints = critical,CA:TRUE,pathlen:0
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
"""

_OPENSSL_LEAF_CONFIG = """[req]
distinguished_name = dn
prompt = no
[dn]
CN = localhost
[v3_leaf]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth,clientAuth
subjectAltName = IP:127.0.0.1,DNS:localhost
"""

#: Reference names the bilateral binding document declares for upper material.
SERVER_MATERIAL_REFERENCES = (
    "env://ubm/tls/r1-server-certificate",
    "env://ubm/tls/r1-server-private-key",
    "env://ubm/tls/rapp-server-certificate",
    "env://ubm/tls/rapp-server-private-key",
    "env://ubm/tls/o1-provider-server-certificate",
    "env://ubm/tls/o1-provider-server-private-key",
    "env://ubm/tls/o1-consumer-server-certificate",
    "env://ubm/tls/o1-consumer-server-private-key",
    "env://ubm/tls/client-truststore",
)


def bootstrap_loopback_tls(out_dir: Path) -> dict[str, str]:
    """Mint a loopback CA and one server leaf, then emit a secret map.

    The material is written with mode 0600 under ``out_dir`` - never into the
    release archive.  Absence of ``openssl`` is a hard failure; there is no
    plaintext fallback.
    """
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(target, 0o700)
    if _which("openssl") is None:
        raise TlsConfigurationError(
            "openssl is required to bootstrap loopback TLS; required references: "
            + ", ".join(SERVER_MATERIAL_REFERENCES))
    ca_key, ca_cert = target / "loopback-ca.key", target / "loopback-ca.pem"
    leaf_key, leaf_cert = target / "loopback-leaf.key", target / "loopback-leaf.pem"
    csr = target / "loopback-leaf.csr"
    ca_config, leaf_config = target / "ca.cnf", target / "leaf.cnf"
    ca_config.write_text(_OPENSSL_CONFIG, encoding="utf-8")
    leaf_config.write_text(_OPENSSL_LEAF_CONFIG, encoding="utf-8")
    _openssl(["req", "-x509", "-newkey", "rsa:2048", "-nodes",
              "-keyout", str(ca_key), "-out", str(ca_cert),
              "-days", "365", "-config", str(ca_config)])
    _openssl(["req", "-new", "-newkey", "rsa:2048", "-nodes",
              "-keyout", str(leaf_key), "-out", str(csr),
              "-config", str(leaf_config)])
    _openssl(["x509", "-req", "-in", str(csr), "-CA", str(ca_cert),
              "-CAkey", str(ca_key), "-CAcreateserial", "-out", str(leaf_cert),
              "-days", "365", "-extfile", str(leaf_config), "-extensions",
              "v3_leaf"])
    for path in (ca_key, leaf_key):
        os.chmod(path, 0o600)
    for path in (ca_cert, leaf_cert):
        os.chmod(path, 0o644)
    mapping = {
        "env://ubm/tls/r1-server-certificate": str(leaf_cert),
        "env://ubm/tls/r1-server-private-key": str(leaf_key),
        "env://ubm/tls/rapp-server-certificate": str(leaf_cert),
        "env://ubm/tls/rapp-server-private-key": str(leaf_key),
        "env://ubm/tls/o1-provider-server-certificate": str(leaf_cert),
        "env://ubm/tls/o1-provider-server-private-key": str(leaf_key),
        "env://ubm/tls/o1-consumer-server-certificate": str(leaf_cert),
        "env://ubm/tls/o1-consumer-server-private-key": str(leaf_key),
        "env://ubm/tls/client-truststore": str(ca_cert),
        "secret://phase-a-mock/pki/truststore": str(ca_cert),
    }
    secret_map = target / "secret-map.json"
    secret_map.write_text(
        json.dumps(mapping, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(secret_map, 0o600)
    return mapping


def _which(program: str) -> str | None:
    for element in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(element) / program
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _openssl(arguments: list[str]) -> None:
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [_which("openssl") or "openssl", *arguments],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if completed.returncode != 0:
        raise TlsConfigurationError(
            "openssl failed while bootstrapping loopback TLS material")
