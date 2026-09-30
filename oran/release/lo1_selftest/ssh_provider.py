"""SSH-borne Provider surfaces: the NETCONF subsystem and the SFTP subsystem.

Both are real SSH transports, because ``o1-netconf-yang-profile.1.0.0.json``
declares ``NETCONF_1_1_OVER_SSH`` with ``serverHostKeyPinningRequired`` and
``clientPublicKeyAuthenticationRequired``, and because the egress guard the
core runtime installs has to be able to observe an ``SSH_TRANSPORT_OPEN`` --
which only exists if a real SSH library is on the path.

Key material rules (``emulator-boundary.1.0.0.json#/sshKeyMaterial``):

* the keypair is generated at start into a run-scoped temporary directory that
  is outside the release tree, outside the capture root and outside the
  repository;
* the host-key pin is the digest of the key just generated, so the pin is a
  runtime value and never a stored one;
* the client credential reaches the consumer only through a ``secretRef`` that
  resolves inside the same run-scoped directory;
* everything is removed at teardown.

The NETCONF handler answers **only** requests that are byte-equal to one of the
registered golden fixtures.  A one-byte difference is an ``rpc-error`` and an
unknown route, which is what makes ``LO1-ST-E04`` a real falsification rather
than an assertion about intent.

**Framing is implemented here from RFC 6242, and deliberately not shared with
the consumer.**  1.0.1 was withdrawn because this emulator mirrored the
client's end-of-message framing after the hello: two peers agreed with each
other, the self-test passed, and both were non-conformant.  A shared codec
would rebuild that agreement, so this module carries its own parser -- a
blocking pull parser that asks the channel for exactly the octets a chunk
header declares, structurally unlike the consumer's incremental push state
machine -- and its own ``LO1-PFRAME-nnn`` reason codes.  It imports nothing
from ``oran/release/lo1`` and nothing there imports it.

The one thing this emulator must refuse is precisely the withdrawn behaviour:
after both peers advertise ``urn:ietf:params:netconf:base:1.1``, an
end-of-message framed payload is ``LO1-PFRAME-005`` and the session ends
unanswered.
"""

from __future__ import annotations

import os
import socket
import threading
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

warnings.filterwarnings("ignore", message=".*TripleDES.*")

try:  # pragma: no cover - exercised by the availability test
    import paramiko
    from paramiko import SFTPServer, SFTPServerInterface, SFTPAttributes
    from paramiko.sftp import SFTP_OK, SFTP_NO_SUCH_FILE, SFTP_PERMISSION_DENIED
    PARAMIKO_AVAILABLE = True
    PARAMIKO_IMPORT_ERROR: str | None = None
except Exception as exc:  # pragma: no cover - environment without paramiko
    paramiko = None  # type: ignore[assignment]
    SFTPServer = SFTPServerInterface = SFTPAttributes = object  # type: ignore[assignment]
    SFTP_OK = SFTP_NO_SUCH_FILE = SFTP_PERMISSION_DENIED = -1  # type: ignore[assignment]
    PARAMIKO_AVAILABLE = False
    PARAMIKO_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

LOOPBACK = "127.0.0.1"

#: RFC 6242 §4.3.  This Provider frames its ``<hello>`` with it -- and refuses
#: it for everything after the hello once ``base:1.1`` is on both sides.
NETCONF_FRAME_TERMINATOR = b"]]>]]>"

#: RFC 6241 §8.1.  Compared as a string; the framing follows from both peers
#: advertising it, never from a configuration flag.
NETCONF_BASE_1_1 = "urn:ietf:params:netconf:base:1.1"

#: The two framings this Provider can be in, recorded in the observations so a
#: run can be asked which one it actually used.
PROVIDER_FRAMING_EOM = "END_OF_MESSAGE"
PROVIDER_FRAMING_CHUNKED = "CHUNKED"

#: What this Provider will hold for one reassembled message before refusing.
PROVIDER_MESSAGE_LIMIT_OCTETS = 4 * 1024 * 1024

#: Replies are emitted in several small chunks on purpose.  Every reply on
#: every run therefore exercises a consumer's reassembly path; a consumer that
#: only ever handled one chunk per message would fail on the first exchange
#: instead of on some rare large payload in production.
PROVIDER_REPLY_CHUNK_OCTETS = 512

#: RFC 6242 §4.2 caps ``chunk-size`` at 4294967295, so at most ten digits.
PROVIDER_MAX_CHUNK_OCTETS = 4294967295
PROVIDER_MAX_SIZE_DIGITS = 10

#: How far this Provider reads before deciding that a header which does not
#: start with LF is a stray end-of-message sentinel rather than plain garbage.
_PROVIDER_CLASSIFY_OCTETS = 8192
_PROVIDER_CLASSIFY_SECONDS = 2.0

#: Every framing refusal this Provider can make.  Its own namespace: sharing
#: the consumer's codes would hide which side of the wire made the judgement.
PROVIDER_FRAMING_REASONS: dict[str, str] = {
    "LO1-PFRAME-001": "CHUNK_HEADER_MALFORMED",
    "LO1-PFRAME-002": "CHUNK_SIZE_LEADING_ZERO",
    "LO1-PFRAME-003": "TRUNCATED_CHUNK_STREAM",
    "LO1-PFRAME-004": "END_OF_CHUNKS_ABSENT",
    "LO1-PFRAME-005": "EOM_FRAMING_AFTER_HELLO",
    "LO1-PFRAME-006": "MESSAGE_EXCEEDS_PROVIDER_LIMIT",
}


class SshProviderError(RuntimeError):
    """The SSH-borne Provider surface could not be brought up; fail closed."""


class ProviderFramingViolation(SshProviderError):
    """A client's octet stream violated RFC 6242.  The session ends unanswered."""

    def __init__(self, reason_code: str, detail: str) -> None:
        if reason_code not in PROVIDER_FRAMING_REASONS:
            raise KeyError("undeclared provider framing reason %s" % reason_code)
        self.reason_code = reason_code
        self.reason = PROVIDER_FRAMING_REASONS[reason_code]
        super().__init__("%s (%s): %s" % (reason_code, self.reason, detail))


def require_paramiko() -> None:
    if not PARAMIKO_AVAILABLE:
        raise SshProviderError(
            "the Provider emulator speaks real SSH for NETCONF and SFTP and refuses "
            "to downgrade to a plaintext stand-in; paramiko is unavailable: "
            f"{PARAMIKO_IMPORT_ERROR}")


@dataclass
class EphemeralKeyMaterial:
    """Host and client keys, generated per run into a run-scoped directory."""

    directory: Path
    host_key: Any
    client_key: Any
    client_key_path: Path
    known_hosts_path: Path
    host_key_sha256: str

    def secret_map(self, *, prefix: str) -> dict[str, str]:
        """``secretRef`` -> filesystem path.  Never key bytes, only references."""
        return {
            f"file://{prefix}/known-hosts": str(self.known_hosts_path),
            f"file://{prefix}/credential": str(self.client_key_path),
        }

    def remove(self) -> None:
        for path in (self.client_key_path, self.known_hosts_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def generate_key_material(directory: Path, *, bits: int = 2048) -> EphemeralKeyMaterial:
    require_paramiko()
    import hashlib

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    host_key = paramiko.RSAKey.generate(bits)
    client_key = paramiko.RSAKey.generate(bits)
    client_key_path = directory / "client-credential"
    client_key.write_private_key_file(str(client_key_path))
    os.chmod(client_key_path, 0o600)
    known_hosts_path = directory / "known-hosts"
    pin = hashlib.sha256(host_key.asbytes()).hexdigest()
    # The pin is the digest of the key just generated: a runtime value, written
    # as a digest rather than as key material so nothing here is a stored key.
    known_hosts_path.write_text(f"{LOOPBACK} sha256:{pin}\n", encoding="utf-8")
    os.chmod(known_hosts_path, 0o600)
    return EphemeralKeyMaterial(
        directory=directory,
        host_key=host_key,
        client_key=client_key,
        client_key_path=client_key_path,
        known_hosts_path=known_hosts_path,
        host_key_sha256=pin,
    )


# --------------------------------------------------------------------- SFTP


def _build_sftp_interface(root: Path, observer: Callable[[str], None]):
    require_paramiko()

    class _Handle(paramiko.SFTPHandle):
        def __init__(self, handle) -> None:  # noqa: D401 - paramiko protocol
            super().__init__(0)
            self.readfile = handle

        def close(self) -> None:
            try:
                self.readfile.close()
            except Exception:  # pragma: no cover - best effort teardown
                pass

    class _Interface(SFTPServerInterface):
        def _real(self, path: str) -> Path:
            name = os.path.basename(path)
            if not name or name in (".", ".."):
                raise ValueError("directory traversal is refused")
            return Path(root) / name

        def list_folder(self, path):
            entries = []
            for child in sorted(Path(root).glob("*")):
                attributes = SFTPAttributes.from_stat(child.stat())
                attributes.filename = child.name
                entries.append(attributes)
            observer(f"LIST {path}")
            return entries

        def stat(self, path):
            try:
                target = self._real(path)
            except ValueError:
                return SFTP_PERMISSION_DENIED
            if not target.is_file():
                return SFTP_NO_SUCH_FILE
            return SFTPAttributes.from_stat(target.stat())

        lstat = stat

        def open(self, path, flags, attr):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT):
                return SFTP_PERMISSION_DENIED
            try:
                target = self._real(path)
            except ValueError:
                return SFTP_PERMISSION_DENIED
            if not target.is_file():
                return SFTP_NO_SUCH_FILE
            observer(f"GET {target.name}")
            return _Handle(open(target, "rb"))

        def remove(self, path):
            return SFTP_PERMISSION_DENIED

        def rename(self, oldpath, newpath):
            return SFTP_PERMISSION_DENIED

        def mkdir(self, path, attr):
            return SFTP_PERMISSION_DENIED

        def rmdir(self, path):
            return SFTP_PERMISSION_DENIED

        def chattr(self, path, attr):
            return SFTP_PERMISSION_DENIED

    return _Interface


# ------------------------------------------------------------------ NETCONF


@dataclass
class NetconfExchange:
    request: bytes
    fixture_ref: str | None
    fixture_sha256: str | None
    reply: bytes
    outcome: str


@dataclass
class NetconfObservations:
    hello: bytes = b""
    #: The client half of the RFC 6241 capability exchange, when one arrived.
    client_hello: bytes = b""
    advertised_capabilities: tuple[str, ...] = ()
    exchanges: list[NetconfExchange] = field(default_factory=list)
    unknown_routes: int = 0
    #: Which RFC 6242 framing the session settled on after the hello exchange.
    framing_mode: str = PROVIDER_FRAMING_EOM
    #: One entry per refusal, by reason code, plus the last rendered detail.
    framing_violations: list[str] = field(default_factory=list)
    last_framing_violation: str = ""
    #: How many chunks each accepted request arrived in, and each reply left in.
    request_chunk_counts: list[int] = field(default_factory=list)
    reply_chunk_counts: list[int] = field(default_factory=list)


def frame_in_chunks(payload: bytes, *,
                    chunk_octets: int = PROVIDER_REPLY_CHUNK_OCTETS) -> bytes:
    """RFC 6242 §4.2 encoder.  Written for this Provider, shared with nobody.

    ``chunk-data`` is ``1*OCTET``, so a zero-length payload has no encoding and
    is a programming error here rather than a bare end-of-chunks on the wire.
    """
    body = bytes(payload)
    if not body:
        raise ProviderFramingViolation(
            "LO1-PFRAME-001",
            "a zero-length reply has no RFC 6242 chunked encoding")
    width = max(1, int(chunk_octets))
    pieces = [body[at:at + width] for at in range(0, len(body), width)]
    return b"".join(
        b"\n#" + str(len(piece)).encode("ascii") + b"\n" + piece
        for piece in pieces) + b"\n##\n"


def chunks_in(framed: bytes) -> int:
    """How many chunks an encoded message carries.  Evidence, not transport."""
    count = 0
    at = 0
    while at < len(framed):
        if framed[at:at + 4] == b"\n##\n":
            return count
        if framed[at:at + 2] != b"\n#":
            raise ProviderFramingViolation(
                "LO1-PFRAME-001", "not a chunk header at offset %d" % at)
        end = framed.index(b"\n", at + 2)
        size = int(framed[at + 2:end])
        at = end + 1 + size
        count += 1
    raise ProviderFramingViolation("LO1-PFRAME-004", "no end-of-chunks marker")


class NetconfWireReader:
    """This Provider's RFC 6242 reader: a blocking pull parser.

    It asks the channel for *exactly* the octets the stream says come next --
    one for a marker, N for a chunk body -- rather than pushing arbitrary
    reads into a state machine.  That shape is the point: the consumer's
    decoder is written the other way round, so a defect in one cannot be a
    defect in the other by construction, and a stream both accept is a stream
    two independently written parsers accepted.
    """

    def __init__(self, channel: Any, *,
                 limit: int = PROVIDER_MESSAGE_LIMIT_OCTETS) -> None:
        self._channel = channel
        self._limit = int(limit)
        self._held = bytearray()
        self._closed = False
        #: Chunks the last message returned by :meth:`read_chunked` arrived in.
        self.last_chunk_count = 0

    # -- octets -----------------------------------------------------------
    def _pull(self) -> bool:
        if self._closed:
            return False
        block = self._channel.recv(65536)
        if not block:
            self._closed = True
            return False
        self._held += block
        return True

    def _exactly(self, count: int, *, what: str) -> bytes:
        while len(self._held) < count:
            if not self._pull():
                raise ProviderFramingViolation(
                    "LO1-PFRAME-003",
                    "the client ended the stream needing %d more octet(s) of %s"
                    % (count - len(self._held), what))
        taken = bytes(self._held[:count])
        del self._held[:count]
        return taken

    def _optional(self) -> bytes:
        """One octet, or ``b""`` when the client hung up cleanly."""
        while not self._held:
            if not self._pull():
                return b""
        taken = bytes(self._held[:1])
        del self._held[:1]
        return taken

    # -- hello framing ----------------------------------------------------
    def read_end_of_message(self) -> bytes | None:
        """RFC 6242 §4.3.  ``None`` when the client hung up between frames."""
        while True:
            at = self._held.find(NETCONF_FRAME_TERMINATOR)
            if at >= 0:
                frame = bytes(self._held[:at])
                del self._held[:at + len(NETCONF_FRAME_TERMINATOR)]
                return frame
            if len(self._held) > self._limit:
                raise ProviderFramingViolation(
                    "LO1-PFRAME-006",
                    "an unterminated frame passed the %d octet limit"
                    % self._limit)
            if not self._pull():
                if not self._held:
                    return None
                raise ProviderFramingViolation(
                    "LO1-PFRAME-003",
                    "the client ended the stream %d octets into an "
                    "unterminated frame" % len(self._held))

    # -- chunked framing --------------------------------------------------
    def read_chunked(self) -> bytes | None:
        """RFC 6242 §4.2.  ``None`` when the client hung up between messages."""
        message = bytearray()
        chunks = 0
        while True:
            first = self._optional()
            if first == b"":
                if chunks == 0 and not message:
                    return None
                raise ProviderFramingViolation(
                    "LO1-PFRAME-004",
                    "the client ended the stream after %d chunk(s) without "
                    "the end-of-chunks marker" % chunks)
            if first != b"\n":
                raise self._classify(first)
            if self._exactly(1, what="a chunk header") != b"#":
                raise ProviderFramingViolation(
                    "LO1-PFRAME-001", "a chunk header must begin LF '#'")
            marker = self._exactly(1, what="a chunk size")
            if marker == b"#":
                if self._exactly(1, what="the end-of-chunks marker") != b"\n":
                    raise ProviderFramingViolation(
                        "LO1-PFRAME-001",
                        "the end-of-chunks marker must be LF '#' '#' LF")
                if chunks == 0:
                    raise ProviderFramingViolation(
                        "LO1-PFRAME-001",
                        "end-of-chunks arrived before any chunk")
                self.last_chunk_count = chunks
                return bytes(message)
            size = self._read_size(marker, held=len(message))
            message += self._exactly(size, what="chunk data")
            chunks += 1

    def _read_size(self, first: bytes, *, held: int) -> int:
        if not first.isdigit():
            raise ProviderFramingViolation(
                "LO1-PFRAME-001",
                "the chunk size begins with the non-digit %r" % first)
        if first == b"0":
            raise ProviderFramingViolation(
                "LO1-PFRAME-002",
                "the chunk size begins with a zero digit; RFC 6242 spells it "
                "with a non-zero first digit")
        digits = bytearray(first)
        while True:
            octet = self._exactly(1, what="a chunk size")
            if octet == b"\n":
                break
            if not octet.isdigit():
                raise ProviderFramingViolation(
                    "LO1-PFRAME-001",
                    "the chunk size contains the non-digit %r" % octet)
            digits += octet
            if len(digits) > PROVIDER_MAX_SIZE_DIGITS:
                raise ProviderFramingViolation(
                    "LO1-PFRAME-001",
                    "the chunk size is longer than %d digits"
                    % PROVIDER_MAX_SIZE_DIGITS)
        size = int(digits.decode("ascii"))
        if size > PROVIDER_MAX_CHUNK_OCTETS:
            raise ProviderFramingViolation(
                "LO1-PFRAME-001",
                "the chunk size %d passes the RFC 6242 maximum" % size)
        if held + size > self._limit:
            raise ProviderFramingViolation(
                "LO1-PFRAME-006",
                "the message would reach %d octets, past this Provider's %d "
                "octet limit" % (held + size, self._limit))
        return size

    def _classify(self, first: bytes) -> ProviderFramingViolation:
        """Name the violation before refusing it.

        A client that kept ``]]>]]>`` after advertising ``base:1.1`` is the
        exact defect this release was withdrawn for, so it gets its own code
        rather than being folded into "malformed header".
        """
        seen = bytearray(first)
        seen += self._held
        self._held.clear()
        deadline_reads = 0
        while NETCONF_FRAME_TERMINATOR not in seen and len(seen) < _PROVIDER_CLASSIFY_OCTETS:
            try:
                self._channel.settimeout(_PROVIDER_CLASSIFY_SECONDS)
                block = self._channel.recv(4096)
            except Exception:  # noqa: BLE001 - a stall ends the classification
                break
            if not block:
                break
            seen += block
            deadline_reads += 1
            if deadline_reads > 16:
                break
        if NETCONF_FRAME_TERMINATOR in seen:
            return ProviderFramingViolation(
                "LO1-PFRAME-005",
                "the client framed a post-hello message with ]]>]]> after both "
                "peers advertised base:1.1; RFC 6242 allows that framing only "
                "for the hello exchange")
        return ProviderFramingViolation(
            "LO1-PFRAME-001",
            "a chunk header must begin with LF; the client sent %r"
            % bytes(seen[:32]))


def _is_hello(frame: bytes) -> bool:
    """Is this frame the peer's ``<hello>`` rather than an ``<rpc>``?

    RFC 6241 §8.1 has both peers send ``<hello>`` on session start, and a hello
    is answered by nothing.  Counting it as an unknown *route* would make
    ``unknownRouteCount`` -- which ``G-EMU-1`` reads as "a request that is not
    byte-equal to a registered fixture" -- non-zero for a conformant client.
    """
    from xml.etree import ElementTree

    try:
        root = ElementTree.fromstring(frame.decode("utf-8"))
    except Exception:  # noqa: BLE001 - an unparseable frame is not a hello
        return False
    return str(root.tag).rsplit("}", 1)[-1] == "hello"


def _advertised_capabilities(frame: bytes) -> set[str]:
    """What the client's ``<hello>`` advertised.  Read, never assumed."""
    from xml.etree import ElementTree

    try:
        root = ElementTree.fromstring(frame.decode("utf-8"))
    except Exception:  # noqa: BLE001 - an unparseable hello advertises nothing
        return set()
    return {
        (node.text or "").strip() for node in root.iter()
        if str(node.tag).rsplit("}", 1)[-1] == "capability"
        and (node.text or "").strip()}


class NetconfSubsystem:
    """Byte-exact NETCONF responder built from the frozen fixtures alone."""

    def __init__(
        self,
        *,
        capabilities: tuple[str, ...],
        fixtures: Mapping[bytes, tuple[str, str]],
        replies: Mapping[str, bytes],
        observations: NetconfObservations,
        lock: threading.Lock,
        reply_selector: Callable[[str, bytes], bytes] | None = None,
    ) -> None:
        self.capabilities = capabilities
        self.fixtures = dict(fixtures)
        self.replies = dict(replies)
        self.observations = observations
        self._lock = lock
        #: Optional: lets a reply depend on the datastore state the frozen
        #: lifecycle declares, so one fixture can legitimately answer two
        #: different ordinals (``get-perfmetricjob`` is LOCKED at ordinal 4 and
        #: UNLOCKED/ENABLED at ordinal 7).  A static map cannot do both.
        self._reply_selector = reply_selector

    def hello(self) -> bytes:
        capabilities = "".join(
            f"    <capability>{item}</capability>\n" for item in self.capabilities)
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<hello xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">\n'
            "  <capabilities>\n"
            f"{capabilities}"
            "  </capabilities>\n"
            "  <session-id>1</session-id>\n"
            "</hello>\n"
        ).encode("utf-8")

    def serve(self, channel) -> None:
        hello = self.hello()
        with self._lock:
            self.observations.hello = hello
            self.observations.advertised_capabilities = self.capabilities
        # RFC 6242 §4.3: the hello, and only the hello, is end-of-message
        # framed.  What comes after it is what this exchange decides.
        channel.sendall(hello + NETCONF_FRAME_TERMINATOR)
        reader = NetconfWireReader(channel)
        try:
            self._session(channel, reader)
        except ProviderFramingViolation as violation:
            # Fail closed and stay silent: a framing violation ends the
            # session, and answering it would teach a non-conformant peer that
            # its framing works here.
            with self._lock:
                self.observations.framing_violations.append(violation.reason_code)
                self.observations.last_framing_violation = str(violation)
        except Exception:  # pragma: no cover - transport teardown
            pass
        finally:
            try:
                channel.close()
            except Exception:  # pragma: no cover
                pass

    def _session(self, channel, reader: NetconfWireReader) -> None:
        client_hello = reader.read_end_of_message()
        if client_hello is None:
            return
        if not _is_hello(client_hello):
            raise ProviderFramingViolation(
                "LO1-PFRAME-001",
                "the first message of a NETCONF session must be the client "
                "<hello>, end-of-message framed")
        with self._lock:
            self.observations.client_hello = client_hello
        chunked = (NETCONF_BASE_1_1 in _advertised_capabilities(client_hello)
                   and NETCONF_BASE_1_1 in set(self.capabilities))
        with self._lock:
            self.observations.framing_mode = (
                PROVIDER_FRAMING_CHUNKED if chunked else PROVIDER_FRAMING_EOM)
        while True:
            frame = (reader.read_chunked() if chunked
                     else reader.read_end_of_message())
            if frame is None:
                return
            if frame.strip() == b"":
                continue
            if _is_hello(frame):
                # RFC 6241 §8.1 has exactly one hello per peer; a second one is
                # answered by nothing, as the first one was.
                continue
            reply = self.answer(frame)
            if chunked:
                framed = frame_in_chunks(reply)
                with self._lock:
                    self.observations.request_chunk_counts.append(
                        reader.last_chunk_count)
                    self.observations.reply_chunk_counts.append(chunks_in(framed))
                channel.sendall(framed)
            else:
                channel.sendall(reply + NETCONF_FRAME_TERMINATOR)

    def answer(self, frame: bytes) -> bytes:
        registered = self.fixtures.get(frame)
        if registered is None:
            with self._lock:
                self.observations.unknown_routes += 1
                self.observations.exchanges.append(NetconfExchange(
                    request=frame, fixture_ref=None, fixture_sha256=None,
                    reply=self._rpc_error(), outcome="RPC_ERROR"))
            return self._rpc_error()
        relative, digest = registered
        reply = self.replies[relative]
        if self._reply_selector is not None:
            reply = self._reply_selector(relative, reply)
        with self._lock:
            self.observations.exchanges.append(NetconfExchange(
                request=frame, fixture_ref=relative, fixture_sha256=digest,
                reply=reply, outcome="OK"))
        return reply

    @staticmethod
    def _rpc_error() -> bytes:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<rpc-reply xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">\n'
            "  <rpc-error>\n"
            "    <error-type>protocol</error-type>\n"
            "    <error-tag>operation-not-supported</error-tag>\n"
            "    <error-severity>error</error-severity>\n"
            "    <error-message>request is not byte-equal to a registered "
            "golden fixture</error-message>\n"
            "  </rpc-error>\n"
            "</rpc-reply>\n"
        ).encode("utf-8")


# ------------------------------------------------------------------- server


class SshProviderListener:
    """One loopback SSH listener serving the subsystems it is given."""

    def __init__(
        self,
        *,
        key_material: EphemeralKeyMaterial,
        sftp_root: Path | None = None,
        netconf: NetconfSubsystem | None = None,
        sftp_observer: Callable[[str], None] | None = None,
    ) -> None:
        require_paramiko()
        self.key_material = key_material
        self.sftp_root = Path(sftp_root) if sftp_root else None
        self.netconf = netconf
        self._sftp_observer = sftp_observer or (lambda _event: None)
        self._socket = socket.socket()
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind((LOOPBACK, 0))
        self._socket.listen(8)
        self._port = self._socket.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._transports: list[Any] = []

    @property
    def authority(self) -> str:
        return f"{LOOPBACK}:{self._port}"

    @property
    def port(self) -> int:
        return self._port

    def start(self) -> None:
        self._thread.start()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._socket.settimeout(0.2)
                connection, _ = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve, args=(connection,), daemon=True).start()

    def _serve(self, connection: socket.socket) -> None:
        transport = paramiko.Transport(connection)
        transport.add_server_key(self.key_material.host_key)
        if self.sftp_root is not None:
            transport.set_subsystem_handler(
                "sftp", SFTPServer, _build_sftp_interface(self.sftp_root, self._sftp_observer))
        server = _ServerInterface(
            authorized_key=self.key_material.client_key, netconf=self.netconf)
        try:
            transport.start_server(server=server)
        except Exception:  # pragma: no cover - client aborted the handshake
            return
        self._transports.append(transport)
        while not self._stop.is_set() and transport.is_active():
            self._stop.wait(0.2)

    def stop(self) -> None:
        self._stop.set()
        for transport in list(self._transports):
            try:
                transport.close()
            except Exception:  # pragma: no cover
                pass
        try:
            self._socket.close()
        except OSError:  # pragma: no cover
            pass
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)


class _ServerInterface(paramiko.ServerInterface if PARAMIKO_AVAILABLE else object):
    def __init__(self, *, authorized_key, netconf: NetconfSubsystem | None) -> None:
        self._authorized_key = authorized_key
        self._netconf = netconf

    def get_allowed_auths(self, username):  # noqa: D401 - paramiko protocol
        return "publickey"

    def check_auth_publickey(self, username, key):
        if key == self._authorized_key:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_password(self, username, password):
        # Password authentication is refused outright: the frozen profile
        # requires client public-key authentication.
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_subsystem_request(self, channel, name):
        if name == "sftp":
            return super().check_channel_subsystem_request(channel, name)
        if name == "netconf" and self._netconf is not None:
            threading.Thread(
                target=self._netconf.serve, args=(channel,), daemon=True).start()
            return True
        return False
