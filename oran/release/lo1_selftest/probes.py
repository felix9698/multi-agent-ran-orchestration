"""Executable probes.  Each one is a real process that exits non-zero on refusal.

The falsifiers do not assert about intent, they run these as subprocesses and
record the exit code.  Every probe therefore has exactly one job and one
fail-closed answer:

``emulator-rpc``
    Speak NETCONF to the emulator and send the **repository** golden fixtures
    byte for byte.  If the emulator was built from a drifted scratch copy, the
    bytes no longer match a registered fixture and the probe fails
    (``LO1-ST-E04``).

``emulator-capability``
    Read the emulator's advertised capability set from the NETCONF ``<hello>``
    and compare it against the **repository** profile's required list.  A
    capability dropped in a scratch copy fails the consumer's check
    (``LO1-ST-E05``).

``emulator-pm``
    Emit PM documents and validate them against the **repository** PM file
    profile, then compare their values against the golden samples and each
    other.  Profile drift changes the document and is detected rather than
    absorbed (``LO1-ST-E06``); a golden collision or two identical digests fail
    (``LO1-ST-E03``).

``egress-probe``
    The neutered-guard negative control for ``G-EGRESS-2``.  Armed, a deliberate
    out-of-allowlist connect must be observed and refused; disarmed, the same
    connect must be observed as *unrefused*, which is what proves the counter
    can be non-zero and that a zero is a measurement rather than a silence.
"""

from __future__ import annotations

import hashlib
import json
import socket
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence
from xml.etree import ElementTree

warnings.filterwarnings("ignore", message=".*TripleDES.*")

from .frozen import FrozenBundle, repository_bundle
from .pm import PmGenerator
from .provider_emulator import ProviderEmulator
from .ssh_provider import (
    NETCONF_BASE_1_1,
    NETCONF_FRAME_TERMINATOR,
    PARAMIKO_AVAILABLE,
    NetconfWireReader,
    frame_in_chunks,
    require_paramiko,
)
from .vector import UpperListeners, build_self_test_vector

if PARAMIKO_AVAILABLE:  # pragma: no branch
    import paramiko


class ProbeFailure(RuntimeError):
    """The probe observed the defect it exists to detect."""


@dataclass
class ProbeResult:
    probe: str
    ok: bool
    detail: str = ""
    observations: dict[str, Any] = field(default_factory=dict)


def _emulator_for(bundle_path: Path, work_dir: Path, *, repo_root: Path,
                  seed: int = 20260811) -> ProviderEmulator:
    bundle = FrozenBundle(Path(bundle_path))
    upper = UpperListeners.loopback(
        {"r1": 1, "rapp": 2, "a1_status": 3, "lower_a1": 4, "o1_consumer": 5})
    vector = build_self_test_vector(
        bundle, upper=upper, provider_mns_root="https://127.0.0.1:1",
        provider_sftp_authority="127.0.0.1:1",
        provider_netconf_endpoint="ssh://127.0.0.1:1")
    return ProviderEmulator(
        bundle_path=Path(bundle_path), vector=vector, work_dir=Path(work_dir),
        seed=seed, consumer_notification_uri="https://127.0.0.1:1/pending")


def _netconf_session(emulator: ProviderEmulator):
    require_paramiko()
    authority = emulator.endpoints.netconf.split("//", 1)[1]
    host, _, port = authority.partition(":")
    connection = socket.create_connection((host, int(port)), timeout=10)
    transport = paramiko.Transport(connection)
    transport.start_client(timeout=10)
    presented = hashlib.sha256(
        transport.get_remote_server_key().asbytes()).hexdigest()
    if presented != emulator.endpoints.host_key_sha256:
        transport.close()
        connection.close()
        raise ProbeFailure("the NETCONF host key does not match the runtime pin")
    transport.auth_publickey("lo1-selftest", emulator.client_private_key())
    channel = transport.open_session()
    channel.invoke_subsystem("netconf")
    return transport, connection, channel


def _read_frame(channel, timeout: float = 10.0) -> bytes:
    channel.settimeout(timeout)
    buffer = b""
    while NETCONF_FRAME_TERMINATOR not in buffer:
        chunk = channel.recv(65536)
        if not chunk:
            break
        buffer += chunk
    return buffer.split(NETCONF_FRAME_TERMINATOR, 1)[0]


def _probe_client_hello(capabilities: Sequence[str]) -> bytes:
    """The probe's half of the capability exchange, end-of-message framed."""
    rendered = "".join(
        f"    <capability>{item}</capability>\n" for item in capabilities)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<hello xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">\n'
        "  <capabilities>\n"
        f"{rendered}"
        "  </capabilities>\n"
        "</hello>\n"
    ).encode("utf-8")


def _open_netconf_conversation(channel, capabilities: Sequence[str],
                               timeout: float = 10.0):
    """Hello exchange, then the framing RFC 6242 says the session must use.

    The probes are Provider-side tooling and reuse the Provider's own reader
    and encoder.  The consumer under test shares neither, which is the whole
    point of the split.
    """
    channel.settimeout(timeout)
    server_hello = _read_frame(channel, timeout)
    channel.sendall(_probe_client_hello(capabilities) + NETCONF_FRAME_TERMINATOR)
    reader = NetconfWireReader(channel)
    chunked = NETCONF_BASE_1_1 in set(capabilities)
    return server_hello, reader, chunked


def _exchange(channel, reader, chunked: bool, request: bytes) -> bytes:
    channel.sendall(frame_in_chunks(request) if chunked
                    else request + NETCONF_FRAME_TERMINATOR)
    frame = reader.read_chunked() if chunked else reader.read_end_of_message()
    return b"" if frame is None else frame


def probe_emulator_rpc(*, repo_root: Path, bundle_path: Path,
                       work_dir: Path) -> ProbeResult:
    """Send the REPOSITORY golden fixtures to an emulator built from ``bundle_path``."""
    repository = repository_bundle(Path(repo_root))
    emulator = _emulator_for(bundle_path, work_dir, repo_root=Path(repo_root))
    accepted, rejected = [], []
    try:
        emulator.start()
        transport, connection, channel = _netconf_session(emulator)
        try:
            _server_hello, reader, chunked = _open_netconf_conversation(
                channel, repository.required_netconf_capabilities())
            for fixture in repository.golden_netconf_fixtures():
                reply = _exchange(channel, reader, chunked, fixture.raw)
                if b"<rpc-error>" in reply:
                    rejected.append(fixture.relative_path)
                else:
                    accepted.append(fixture.relative_path)
        finally:
            channel.close()
            transport.close()
            connection.close()
        unknown = emulator.unknown_route_count
    finally:
        emulator.stop()
    observations = {"accepted": accepted, "rejected": rejected,
                    "unknownRouteCount": unknown}
    if rejected or unknown:
        return ProbeResult(
            "emulator-rpc", False,
            f"{len(rejected)} registered fixture(s) refused, unknown routes={unknown}",
            observations)
    return ProbeResult("emulator-rpc", True, observations=observations)


def probe_emulator_capability(*, repo_root: Path, bundle_path: Path,
                             work_dir: Path) -> ProbeResult:
    """Compare the emulator's advertised set to the REPOSITORY profile's required set."""
    repository = repository_bundle(Path(repo_root))
    required = set(repository.required_netconf_capabilities())
    emulator = _emulator_for(bundle_path, work_dir, repo_root=Path(repo_root))
    try:
        emulator.start()
        transport, connection, channel = _netconf_session(emulator)
        try:
            hello = _read_frame(channel)
        finally:
            channel.close()
            transport.close()
            connection.close()
    finally:
        emulator.stop()
    tree = ElementTree.fromstring(hello.decode("utf-8"))
    advertised = {
        (node.text or "").strip()
        for node in tree.iter()
        if node.tag.rsplit("}", 1)[-1] == "capability"
    }
    missing = sorted(required - advertised)
    observations = {"advertised": sorted(advertised), "required": sorted(required),
                    "missing": missing}
    if missing:
        return ProbeResult(
            "emulator-capability", False,
            f"the Provider does not advertise {missing}", observations)
    return ProbeResult("emulator-capability", True, observations=observations)


def probe_emulator_pm(*, repo_root: Path, bundle_path: Path, work_dir: Path,
                      repeat: int = 2, seed: int | None = None) -> ProbeResult:
    """Emit PM documents and validate them against the REPOSITORY profile."""
    repository = repository_bundle(Path(repo_root))
    namespace = repository.pm_namespace()
    root_element = repository.pm_root_element()
    golden = repository.golden_sample_values()
    scratch = FrozenBundle(Path(bundle_path))
    generator = PmGenerator(scratch, seed=seed if seed is not None else 20260811)
    digests: list[str] = []
    collisions: list[str] = []
    drift: list[str] = []
    for index in range(repeat):
        start = f"2026-08-11T1{index}:00:00Z"
        end = f"2026-08-11T1{index}:01:00Z"
        emitted = generator.generate(
            window_start=start, window_end=end,
            cell_dns=("GNBDUFunction=oai-du,NRCellDU=1",
                      "GNBDUFunction=oai-du,NRCellDU=2"),
            job_id=str(repository.pa_file_profile["perfMetricJob"]["jobId"]))
        digests.append(hashlib.sha256(emitted.raw).hexdigest())
        try:
            tree = ElementTree.fromstring(emitted.raw.decode("utf-8"))
        except ElementTree.ParseError as exc:
            drift.append(f"unparseable: {exc}")
            continue
        if tree.tag != f"{{{namespace}}}{root_element}":
            drift.append(
                f"root {tree.tag} is not the declared {{{namespace}}}{root_element}")
            continue
        for node in tree.iter():
            if node.tag.rsplit("}", 1)[-1] == "r" and (node.text or "").strip() in golden:
                collisions.append((node.text or "").strip())
    observations = {"digests": digests, "collisions": collisions, "drift": drift}
    if drift:
        return ProbeResult("emulator-pm", False,
                           f"PM profile drift detected: {drift[0]}", observations)
    if collisions:
        return ProbeResult("emulator-pm", False,
                           f"emitted value equals a golden sample: {collisions}",
                           observations)
    if len(set(digests)) != len(digests):
        return ProbeResult("emulator-pm", False,
                           "two consecutive emissions produced the same PM digest",
                           observations)
    return ProbeResult("emulator-pm", True, observations=observations)


# ------------------------------------------------------------------- egress


class LoopbackEgressObserver:
    """A self-test-owned socket observer.  It is not the runtime's egress guard.

    ``G-EGRESS-2`` asks for a *negative control*: with the observation point
    deliberately disarmed, a deliberate out-of-allowlist connect must still be
    seen to happen.  A counter that reads zero with the guard off is not a
    measurement, so the control has to be able to prove the counter can move.
    """

    def __init__(self, allowlist: Sequence[str]) -> None:
        self.allowlist = set(allowlist)
        self.armed = False
        self.observed: list[str] = []
        self.refused: list[str] = []
        self._original: Callable[..., Any] | None = None

    def arm(self) -> None:
        self._original = socket.socket.connect
        observer = self

        def connect(self_socket, address):  # type: ignore[no-untyped-def]
            authority = f"{address[0]}:{address[1]}" if isinstance(address, tuple) \
                else str(address)
            observer.observed.append(authority)
            if authority not in observer.allowlist:
                observer.refused.append(authority)
                raise ConnectionRefusedError(
                    f"egress to {authority} is outside the allowlist and is refused")
            return observer._original(self_socket, address)  # type: ignore[misc]

        socket.socket.connect = connect  # type: ignore[assignment]
        self.armed = True

    def disarm(self) -> None:
        if self._original is not None:
            socket.socket.connect = self._original  # type: ignore[assignment]
        self.armed = False


def probe_egress(*, armed: bool) -> ProbeResult:
    """Deliberate out-of-allowlist connect, with and without the observation point."""
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    target_port = closed.getsockname()[1]
    closed.close()  # deliberately unbound: the connect must fail at the transport
    target = f"127.0.0.1:{target_port}"
    observer = LoopbackEgressObserver(allowlist=["127.0.0.1:1"])
    refused_by_guard = False
    reached_transport = False
    if armed:
        observer.arm()
    try:
        handle = socket.socket()
        handle.settimeout(2.0)
        try:
            handle.connect(("127.0.0.1", target_port))
            reached_transport = True
        except ConnectionRefusedError as exc:
            if "outside the allowlist" in str(exc):
                refused_by_guard = True
            else:
                reached_transport = True
        except OSError:
            reached_transport = True
        finally:
            handle.close()
    finally:
        if armed:
            observer.disarm()
    observations = {
        "armed": armed,
        "target": target,
        "observedConnects": list(observer.observed),
        "refusedConnects": list(observer.refused),
        "reachedTransport": reached_transport,
    }
    if armed:
        if not refused_by_guard or not observer.refused:
            return ProbeResult(
                "egress-probe", False,
                "the armed observation point did not refuse an out-of-allowlist connect",
                observations)
        return ProbeResult("egress-probe", True, observations=observations)
    # Disarmed: the connect must have escaped the observation point entirely.
    if observer.observed or refused_by_guard:
        return ProbeResult(
            "egress-probe", False,
            "the disarmed observation point still counted a connect", observations)
    if not reached_transport:
        return ProbeResult(
            "egress-probe", False,
            "the disarmed control never reached the transport, so a zero count "
            "would not have been a measurement", observations)
    return ProbeResult("egress-probe", True, observations=observations)


# ------------------------------------------------------- packaged oracle scan


def probe_oracle_literals(*, repo_root: Path, roots: Sequence[Path] | None = None
                          ) -> ProbeResult:
    """``LO1-ST-O02``: no SC-084 expected scalar written as a literal outside the bundle.

    The scalars are READ from the frozen catalog, so this probe cannot be
    satisfied by keeping a stale list up to date.
    """
    repo_root = Path(repo_root)
    bundle = repository_bundle(repo_root)
    expected = bundle.expected()
    scalars: set[str] = set()
    for key, value in expected.items():
        if isinstance(value, list):
            rendered = json.dumps(value, separators=(",", ", "))
            scalars.add(rendered)
            scalars.add(json.dumps(value, separators=(",", ",")))
            scalars.add(str(value))
        elif isinstance(value, str) and len(value) >= 4:
            scalars.add(value)
    scanned = 0
    hits: list[str] = []
    files: list[Path] = []
    if roots:
        for root in roots:
            root = Path(root)
            files.extend(sorted(root.rglob("*.py")) if root.is_dir() else [root])
    else:
        # W3-owned paths only.  The design-owned test file belongs to DESIGN and
        # is not part of what this track packages.
        source = repo_root / "lib" / "oran" / "release" / "lo1_selftest"
        release_mode = source.is_dir()
        if not release_mode:
            source = repo_root / "oran" / "release" / "lo1_selftest"
        if not source.is_dir():
            return ProbeResult("oracle-literals", False,
                               "self-test source tree is absent", {"filesScanned": 0})
        files.extend(sorted(source.rglob("*.py")))
        if release_mode:
            tests = None
        else:
            tests = repo_root / "tests" / "lo1"
        if tests is not None:
            files.extend(sorted(tests.glob("test_selftest_*.py")))
            for name in ("test_falsifiers.py", "support.py"):
                candidate = tests / name
                if candidate.is_file():
                    files.append(candidate)
    for path in files:
        if "__pycache__" in path.parts or not path.is_file():
            continue
        scanned += 1
        text = path.read_text(encoding="utf-8", errors="replace")
        for scalar in scalars:
            quoted = [f'"{scalar}"', f"'{scalar}'"] if not scalar.startswith("[") \
                else [scalar]
            if any(form in text for form in quoted):
                try:
                    where = str(path.relative_to(repo_root))
                except ValueError:
                    where = str(path)
                hits.append(f"{where}:{scalar[:60]}")
    observations = {"filesScanned": scanned, "scalarCount": len(scalars),
                    "hits": hits}
    if hits:
        return ProbeResult("oracle-literals", False,
                           f"{len(hits)} oracle literal(s) outside the frozen bundle",
                           observations)
    return ProbeResult("oracle-literals", True, observations=observations)
