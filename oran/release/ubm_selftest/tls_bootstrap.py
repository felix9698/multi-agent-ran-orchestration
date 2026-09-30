"""Loopback TLS material for the self-test harness.

Certificates and keys are minted at run time into a caller-owned directory with
mode ``0600`` and are never written into the repository or any release archive.
Only *reference URIs* ever appear in reports (``file://`` per the frozen
integration-values vocabulary).  If ``openssl`` is unavailable the bootstrap
fails closed; it never falls back to plaintext.
"""

from __future__ import annotations

import os
import shutil
import ssl
import subprocess
from dataclasses import dataclass
from pathlib import Path


class TlsBootstrapError(RuntimeError):
    """TLS material could not be produced; the caller must not continue."""


def openssl_available() -> bool:
    return shutil.which("openssl") is not None


_OPENSSL_CONFIG = """
[req]
distinguished_name = dn
x509_extensions = v3
prompt = no
[dn]
CN = %(cn)s
[v3]
subjectAltName = IP:127.0.0.1,IP:::1,DNS:localhost
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth,clientAuth
"""


@dataclass(frozen=True)
class LoopbackTlsMaterial:
    directory: Path
    certificate: Path
    private_key: Path
    #: Trust anchor when the leaf is CA-signed rather than self-signed.  The
    #: release runtime's own ``bootstrap`` mints a CA plus one leaf, and the
    #: self-test then serves the lower A1 listener with that same leaf so both
    #: sides verify against one anchor without weakening verification.
    trust_anchor: Path | None = None

    @property
    def truststore(self) -> Path:
        # Self-signed leaf: the certificate is its own trust anchor.
        return self.trust_anchor or self.certificate

    def reference_map(self) -> dict[str, str]:
        """Reference URI -> filesystem path, in the frozen ``file://`` scheme."""
        return {
            "file://ubm-selftest/tls/certificate": str(self.certificate),
            "file://ubm-selftest/tls/private-key": str(self.private_key),
            "file://ubm-selftest/tls/truststore": str(self.truststore),
        }

    def server_context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(self.certificate), str(self.private_key))
        return context

    def client_context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = False  # loopback IP SAN; verification stays on
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(str(self.truststore))
        return context


def bootstrap_loopback_tls(directory: Path, *, common_name: str = "ubm-selftest-loopback",
                           days: int = 2) -> LoopbackTlsMaterial:
    if not openssl_available():
        raise TlsBootstrapError(
            "openssl is required to mint loopback TLS material; refusing to run in plaintext")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    certificate = directory / "loopback-cert.pem"
    private_key = directory / "loopback-key.pem"
    config = directory / "openssl.cnf"
    config.write_text(_OPENSSL_CONFIG % {"cn": common_name}, encoding="utf-8")
    completed = subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(private_key), "-out", str(certificate),
         "-days", str(days), "-config", str(config)],
        capture_output=True, text=True, check=False)
    if completed.returncode != 0 or not certificate.exists() or not private_key.exists():
        raise TlsBootstrapError("openssl failed to mint loopback material: %s" % completed.stderr)
    os.chmod(private_key, 0o600)
    os.chmod(certificate, 0o600)
    config.unlink()
    return LoopbackTlsMaterial(
        directory=directory, certificate=certificate, private_key=private_key)
