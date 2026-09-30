"""Loopback TLS material for the self-test, minted in process at run time.

``delivery.mutualTls`` is true in the frozen PM file profile, so the
notification hop is HTTPS in both directions and neither side may fall back to
plaintext.  Certificates and keys are minted into a caller-owned, run-scoped
directory with mode ``0600``; nothing is written into the repository, the
release tree or the capture root, and only *reference URIs* ever appear in any
report.

The material is produced with ``cryptography`` rather than an ``openssl``
subprocess on purpose: the release gates count a process spawn as an
observable egress event, and a self-test that spawns a binary to get its own
certificates would put noise into exactly the measurement it is trying to make.
"""

from __future__ import annotations

import datetime as _datetime
import ipaddress
import os
import ssl
from dataclasses import dataclass
from pathlib import Path

try:  # pragma: no cover - exercised by the availability test
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    CRYPTOGRAPHY_AVAILABLE = True
    CRYPTOGRAPHY_IMPORT_ERROR: str | None = None
except Exception as exc:  # pragma: no cover - environment without cryptography
    CRYPTOGRAPHY_AVAILABLE = False
    CRYPTOGRAPHY_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

LOOPBACK = "127.0.0.1"


class TlsMaterialError(RuntimeError):
    """TLS material could not be produced; the caller must not continue."""


@dataclass(frozen=True)
class LoopbackTls:
    directory: Path
    certificate: Path
    private_key: Path
    common_name: str

    def reference_map(self, *, prefix: str) -> dict[str, str]:
        return {
            f"file://{prefix}/certificate": str(self.certificate),
            f"file://{prefix}/private-key": str(self.private_key),
            f"file://{prefix}/truststore": str(self.certificate),
        }

    def server_context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(self.certificate), str(self.private_key))
        return context

    def client_context(self) -> ssl.SSLContext:
        """Client context that trusts THIS leaf and nothing else."""
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = False  # loopback IP SAN; verification stays on
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(str(self.certificate))
        return context


def mint_loopback_tls(directory: Path, *, common_name: str, days: int = 1) -> LoopbackTls:
    if not CRYPTOGRAPHY_AVAILABLE:
        raise TlsMaterialError(
            "loopback TLS material requires the cryptography package; refusing to "
            f"serve the notification hop in plaintext: {CRYPTOGRAPHY_IMPORT_ERROR}")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = _datetime.datetime.now(_datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _datetime.timedelta(minutes=5))
        .not_valid_after(now + _datetime.timedelta(days=days))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.IPAddress(ipaddress.ip_address(LOOPBACK)),
                x509.DNSName("localhost"),
            ]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    certificate_path = directory / f"{common_name}-cert.pem"
    key_path = directory / f"{common_name}-key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    os.chmod(certificate_path, 0o600)
    os.chmod(key_path, 0o600)
    return LoopbackTls(
        directory=directory,
        certificate=certificate_path,
        private_key=key_path,
        common_name=common_name,
    )
