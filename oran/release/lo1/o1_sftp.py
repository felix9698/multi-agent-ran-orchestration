"""Pinned-host-key, secret-reference-backed in-process SFTP retrieval."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import socket
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Protocol
from urllib.parse import unquote, urlsplit


class SftpError(RuntimeError):
    pass


class SftpLocationError(SftpError):
    pass


class SftpTrustError(SftpError):
    pass


@dataclass(frozen=True)
class SftpTarget:
    host: str
    port: int
    username: str
    path: str
    authority: str


@dataclass(frozen=True)
class SftpFetchResult:
    raw: bytes
    authority: str
    host_key_pin_digest: str


class SftpBackend(Protocol):
    def open_transport(self, host: str, port: int, timeout: float) -> Any: ...
    def presented_host_key(self, transport: Any) -> tuple[str, bytes]: ...
    def expected_host_keys(self, path: Path, host: str, port: int) -> Mapping[str, bytes]: ...
    def authenticate(self, transport: Any, username: str, credential_path: Path) -> None: ...
    def open_sftp(self, transport: Any) -> Any: ...
    def read_file(self, sftp: Any, path: str, max_bytes: int) -> bytes: ...


def parse_sftp_location(location: str) -> SftpTarget:
    try:
        parsed = urlsplit(location)
        port = parsed.port
    except ValueError as exc:
        raise SftpLocationError("SFTP location has an invalid port") from exc
    path = unquote(parsed.path)
    if (
        parsed.scheme != "sftp"
        or not parsed.hostname
        or port is None
        or parsed.username == ""
        or parsed.password is not None
        or not path.startswith("/")
        or ".." in PurePosixPath(path).parts
        or "\x00" in path
    ):
        raise SftpLocationError(
            "SFTP location must be sftp://[user@]host:port/absolute/path without password or traversal"
        )
    host = parsed.hostname
    authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    return SftpTarget(host, port, unquote(parsed.username or ""), path, authority)


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _utc(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise SftpLocationError("SFTP file availability timestamps must be RFC3339 UTC Z")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise SftpLocationError("SFTP file availability timestamp is malformed") from exc


def validate_retrieval_window(file_info: Mapping[str, Any], *, retrieved_at: str) -> None:
    """Refuse before transport when fileInfo does not admit this retrieval instant."""
    ready = _utc(file_info.get("fileReadyTime"))
    expiration = _utc(file_info.get("fileExpirationTime"))
    observed = _utc(retrieved_at)
    if ready >= expiration or not ready <= observed < expiration:
        raise SftpLocationError("retrieval is outside the fileReady/fileExpiration interval")


class ParamikoBackend:
    """Lazy Paramiko adapter; no subprocess fallback exists."""

    @staticmethod
    def _paramiko():
        try:
            import paramiko
        except ImportError as exc:
            raise SftpError("the in-process Paramiko dependency is unavailable") from exc
        return paramiko

    def open_transport(self, host: str, port: int, timeout: float) -> Any:
        paramiko = self._paramiko()
        sock = socket.create_connection((host, port), timeout=timeout)
        try:
            transport = paramiko.Transport(sock)
            transport.start_client(timeout=timeout)
            return transport
        except Exception:
            sock.close()
            raise

    def presented_host_key(self, transport: Any) -> tuple[str, bytes]:
        key = transport.get_remote_server_key()
        return key.get_name(), key.asbytes()

    def expected_host_keys(self, path: Path, host: str, port: int) -> Mapping[str, bytes]:
        if _is_loopback(host):
            data_lines = [
                line.split()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
            if len(data_lines) == 1 and len(data_lines[0]) == 2:
                pinned_host, encoded = data_lines[0]
                if pinned_host == host and re.fullmatch(r"sha256:[a-f0-9]{64}", encoded):
                    return {"sha256": bytes.fromhex(encoded.removeprefix("sha256:"))}
        paramiko = self._paramiko()
        keys = paramiko.HostKeys()
        keys.load(str(path))
        found = keys.lookup(f"[{host}]:{port}") or {}
        return {name: key.asbytes() for name, key in found.items()}

    def authenticate(self, transport: Any, username: str, credential_path: Path) -> None:
        paramiko = self._paramiko()
        failures = []
        for key_class in (
            paramiko.Ed25519Key,
            paramiko.ECDSAKey,
            paramiko.RSAKey,
            paramiko.DSSKey,
        ):
            try:
                key = key_class.from_private_key_file(str(credential_path))
                transport.auth_publickey(username, key)
                if not transport.is_authenticated():
                    raise SftpTrustError("SFTP public-key authentication was not accepted")
                return
            except (SftpTrustError, paramiko.AuthenticationException):
                raise
            except Exception as exc:
                failures.append(exc)
        raise SftpTrustError("credentialRef did not resolve to a supported SSH private key") from failures[-1]

    def open_sftp(self, transport: Any) -> Any:
        return self._paramiko().SFTPClient.from_transport(transport)

    def read_file(self, sftp: Any, path: str, max_bytes: int) -> bytes:
        with sftp.open(path, "rb") as handle:
            return handle.read(max_bytes + 1)


class PinnedSftpClient:
    MAX_BYTES = 16 * 1024 * 1024

    def __init__(
        self,
        *,
        allowed_authorities: tuple[str, ...] | list[str],
        known_hosts_path: Path,
        credential_path: Path,
        guard: Any,
        backend: SftpBackend | None = None,
    ) -> None:
        self.allowed_authorities = frozenset(str(item) for item in allowed_authorities)
        self.known_hosts_path = Path(known_hosts_path)
        self.credential_path = Path(credential_path)
        self.guard = guard
        self.backend = backend or ParamikoBackend()
        if guard is None or not hasattr(guard, "note_ssh_open"):
            raise SftpTrustError("SFTP refuses to run without the egress guard's SSH seam")
        if not self.known_hosts_path.is_file() or not self.credential_path.is_file():
            raise SftpTrustError("SFTP secretRef did not resolve to required regular files")

    def fetch(self, location: str, *, expected_size: int, timeout: float = 30.0) -> SftpFetchResult:
        target = parse_sftp_location(location)
        if target.authority not in self.allowed_authorities:
            raise SftpTrustError("SFTP authority is outside the gate-admitted allowlist")
        username = target.username
        if not username:
            # notifyFileReady.fileLocation is producer-selected and the frozen
            # contract (including its golden notification) permits an SFTP URI
            # without userinfo.  The local emulator owns the historical `o1`
            # account; the accepted live Provider exposes its read-only account
            # as `o1sftp`.  Authentication remains bound to credentialRef, the
            # exact authority allowlist and the pinned host key.
            username = "o1" if _is_loopback(target.host) else "o1sftp"
        if not isinstance(expected_size, int) or not 0 < expected_size <= self.MAX_BYTES:
            raise SftpError("SFTP expected size is invalid or exceeds the profile limit")
        transport = None
        sftp = None
        operation_error: BaseException | None = None
        self.guard.note_ssh_open(target.host, target.port, library="paramiko.Transport")
        try:
            transport = self.backend.open_transport(target.host, target.port, timeout)
            algorithm, presented = self.backend.presented_host_key(transport)
            expected_keys = self.backend.expected_host_keys(
                self.known_hosts_path, target.host, target.port
            )
            expected = expected_keys.get(algorithm)
            expected_digest = expected_keys.get("sha256")
            raw_key_matches = expected is not None and hmac.compare_digest(
                hashlib.sha256(expected).digest(), hashlib.sha256(presented).digest()
            )
            digest_pin_matches = expected_digest is not None and hmac.compare_digest(
                expected_digest, hashlib.sha256(presented).digest()
            )
            if not raw_key_matches and not digest_pin_matches:
                raise SftpTrustError("presented SFTP host key differs from the pinned known_hosts key")
            pin_digest = hashlib.sha256(presented).hexdigest()
            self.backend.authenticate(transport, username, self.credential_path)
            self.guard.note_ssh_open(target.host, target.port, library="paramiko.SFTPClient")
            sftp = self.backend.open_sftp(transport)
            raw = self.backend.read_file(sftp, target.path, self.MAX_BYTES)
            if len(raw) != expected_size or len(raw) > self.MAX_BYTES:
                raise SftpError("SFTP payload is partial, oversized, or differs from fileInfo.fileSize")
            return SftpFetchResult(raw=raw, authority=target.authority, host_key_pin_digest=pin_digest)
        except BaseException as exc:
            operation_error = exc
            raise
        finally:
            close_errors = []
            if sftp is not None:
                try:
                    sftp.close()
                except Exception as exc:
                    close_errors.append(exc)
            if transport is not None:
                try:
                    transport.close()
                except Exception as exc:
                    close_errors.append(exc)
            if operation_error is None and close_errors:
                raise SftpError("SFTP session cleanup failed") from close_errors[0]
