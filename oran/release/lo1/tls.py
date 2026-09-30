"""TLS termination and secret reference resolution for the live-O1 profile.

Certificates, keys, known-hosts pins and credentials are never release bytes.
They are addressed only through the reference vocabularies the frozen documents
declare -- ``secret://``, ``vault://`` and ``k8s-secret://`` in the deployment
vector, plus ``env://``, ``file://``, ``k8s://`` and ``keychain://`` -- and a
start-time secret map resolves a reference to a path outside the release tree.
Resolved paths and material are never logged and never captured; the capture
records the reference URI only.

An unresolvable reference is a refusal (G-ID-07), never a downgrade to an
anonymous or unpinned connection.
"""

from __future__ import annotations

import json
import re
import ssl
from pathlib import Path
from typing import Mapping

VECTOR_REFERENCE = re.compile(r"^(secret|vault|k8s-secret)://")
ANY_REFERENCE = re.compile(r"^(secret|vault|k8s-secret|env|file|k8s|keychain)://")

MINIMUM_TLS_VERSION = ssl.TLSVersion.TLSv1_2


class TlsConfigurationError(RuntimeError):
    """A reference is unknown, unresolvable or outside the vocabulary."""


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

    def resolves_inside(self, reference: str, root: Path) -> bool:
        """True when the material would live inside the release tree.

        Material inside the package is exactly the defect G-ID-07 refuses: a
        packaged secret is a secret every recipient of the archive holds.
        """
        try:
            target = self.resolve_path(reference).resolve()
        except TlsConfigurationError:
            return False
        try:
            target.relative_to(Path(root).resolve())
        except ValueError:
            return False
        return True


def build_server_ssl_context(*, certificate_ref: str, private_key_ref: str,
                             truststore_ref: str | None,
                             resolver: SecretResolver,
                             require_client_certificate: bool = False,
                             ) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = MINIMUM_TLS_VERSION
    try:
        context.load_cert_chain(
            certfile=str(resolver.resolve_path(certificate_ref)),
            keyfile=str(resolver.resolve_path(private_key_ref)))
        if require_client_certificate and not truststore_ref:
            raise TlsConfigurationError(
                "mutual TLS requires an explicit client truststore reference")
        if truststore_ref is not None and resolver.has(truststore_ref):
            context.load_verify_locations(
                cafile=str(resolver.resolve_path(truststore_ref)))
        elif require_client_certificate:
            raise TlsConfigurationError(
                "mutual TLS client truststore does not resolve")
    except (ssl.SSLError, OSError) as exc:
        raise TlsConfigurationError(
            "the resolved server TLS material is unusable") from exc
    context.verify_mode = (ssl.CERT_REQUIRED if require_client_certificate
                           else ssl.CERT_NONE)
    return context


def build_client_ssl_context(*, truststore_ref: str,
                             resolver: SecretResolver,
                             client_certificate_ref: str | None = None,
                             client_private_key_ref: str | None = None,
                             ) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = MINIMUM_TLS_VERSION
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    try:
        context.load_verify_locations(
            cafile=str(resolver.resolve_path(truststore_ref)))
        if bool(client_certificate_ref) != bool(client_private_key_ref):
            raise TlsConfigurationError(
                "mutual TLS requires both client certificate and private key")
        if client_certificate_ref and client_private_key_ref:
            context.load_cert_chain(
                certfile=str(resolver.resolve_path(client_certificate_ref)),
                keyfile=str(resolver.resolve_path(client_private_key_ref)))
    except (ssl.SSLError, OSError) as exc:
        raise TlsConfigurationError(
            "the resolved truststore is unusable; an unusable trust anchor is a "
            "refusal, never a downgrade to an unverified connection") from exc
    return context
