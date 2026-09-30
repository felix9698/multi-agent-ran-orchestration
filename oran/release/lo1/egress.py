"""Runtime egress guard and external-target call ledger (G-EGRESS-1).

Every counter the capture publishes under ``/externalCalls`` is read off this
guard, so "no hardware call" and "no forbidden egress" are *measurements against
a published definition* rather than literals a reader would have to take on
trust.  The definition itself comes from
``release-gates.1.0.0.json#/hardwareControlSurfaces``.

Socket-level interception alone is insufficient for this release.  SC-084 speaks
NETCONF and SFTP over SSH, and a third-party SSH library may open a transport
through a socket the guard sees but attribute it to no target, while an ``ssh``
subprocess would route around the in-process guard entirely.  The guard
therefore arms at least three points -- ``SOCKET_CONNECT``, ``SUBPROCESS_SPAWN``
and ``SSH_TRANSPORT_OPEN`` -- and records which ones were armed.

Port 830 is deliberately *not* on the hardware list here, unlike the bilateral
release: this release may legitimately speak NETCONF to the lower Provider.  A
NETCONF connect to any authority the gate did not admit is an
``EXTERNAL_LIVE_TARGET`` violation instead, which is equally fail-closed.
"""

from __future__ import annotations

import json
import socket
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .capture import ObservedClock

__all__ = [
    "EXTERNAL_LIVE_TARGET",
    "EgressGuard",
    "EgressViolation",
    "load_hardware_control_surfaces",
]

#: The classification given to an outbound attempt aimed at an authority the
#: gate never admitted, when that authority is not a declared hardware control
#: surface.  ``externalCalls.externalLiveTargetCalls`` counts exactly these, and
#: ``externalCalls.hardwareCalls`` counts exactly the hardware ones; the two
#: partition the refused attempts and neither can be derived from the other.
EXTERNAL_LIVE_TARGET = "EXTERNAL_LIVE_TARGET"

GATES_FILE_NAME = "release-gates.1.0.0.json"

PROTOCOLS = ("HTTPS", "NETCONF_SSH", "SFTP_SSH")
ROLES = ("LOWER_LIVE_O1_PROVIDER", "LOWER_IMPLEMENTATION_UNDER_TEST",
         "UPPER_SELF", "CONTRACT_FAITHFUL_EMULATOR", "UNATTRIBUTED")

MANDATORY_METHODS = ("SOCKET_CONNECT", "SUBPROCESS_SPAWN", "SSH_TRANSPORT_OPEN")

#: Fallback used only when the declared bytes cannot be read.  The guard reports
#: the source it actually used, so a fallback is never silently mistaken for the
#: declared definition.
_FALLBACK_HARDWARE = {
    "tcpPorts": [22, 9091, 49152, 49153],
    "executables": ["uhd_find_devices", "uhd_usrp_probe", "nr-softmodem",
                    "nr-uesoftmodem", "telnet", "ssh", "scp", "sftp"],
}


class EgressViolation(RuntimeError):
    """An outbound attempt left the deployment's declared authority set."""


def _authority(host: Any, port: Any) -> str:
    text = str(host)
    return "[%s]:%d" % (text, int(port)) if ":" in text else "%s:%d" % (text, int(port))


def _gates_search_roots() -> tuple[Path, ...]:
    here = Path(__file__).resolve()
    return (
        here.parents[3] / "docs" / "upper-live-o1-harness",
        here.parents[4] / "spec",
        here.parent / "spec",
    )


def default_gates_path() -> Path | None:
    for root in _gates_search_roots():
        candidate = root / GATES_FILE_NAME
        if candidate.is_file():
            return candidate
    return None


def load_hardware_control_surfaces(spec_path: Path | None) -> tuple[dict[str, Any], str]:
    """Read the declared hardware-control definition; report where it came from."""
    target = spec_path if spec_path is not None else default_gates_path()
    if target is not None:
        try:
            document = json.loads(Path(target).read_text(encoding="utf-8"))
            declared = document["hardwareControlSurfaces"]
            return ({"tcpPorts": [int(port) for port in declared["tcpPorts"]],
                     "executables": [str(name) for name in declared["executables"]]},
                    "release-gates.1.0.0.json#/hardwareControlSurfaces")
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return (dict(_FALLBACK_HARDWARE),
            "built-in fallback; the declared definition was unreadable")


class _LedgerEntry:
    __slots__ = ("authority", "protocol", "role", "allowed", "call_count",
                 "first_at", "last_at")

    def __init__(self, authority: str, protocol: str, role: str, allowed: bool,
                 at: str) -> None:
        self.authority = authority
        self.protocol = protocol
        self.role = role
        self.allowed = allowed
        self.call_count = 0
        self.first_at = at
        self.last_at = at

    def as_dict(self) -> dict[str, Any]:
        return {
            "authority": self.authority,
            "protocol": self.protocol,
            "role": self.role,
            "allowed": self.allowed,
            "callCount": self.call_count,
            "firstAt": self.first_at,
            "lastAt": self.last_at,
        }


class _ScenarioCounters:
    """The per-scenario slice of what the guard saw."""

    def __init__(self) -> None:
        self.attempted_authorities: list[str] = []
        self.connection_attempts = 0
        self.approved_attempts = 0
        self.forbidden_attempts = 0
        self.hardware_calls = 0
        #: Attempts classified EXTERNAL_LIVE_TARGET.  Distinct in meaning from
        #: ``hardware_calls``: this counts reaching for a live target outside
        #: the deployment's declared authority allowlist, while
        #: ``hardware_calls`` counts reaching for a declared hardware control
        #: surface.  The two partition the refused attempts and never overlap,
        #: so neither can be read off the other.
        self.external_live_target_calls = 0
        self.violations: list[dict[str, Any]] = []
        self.resolved_names: list[str] = []
        self.ledger: dict[tuple[str, str], _LedgerEntry] = {}


class EgressGuard:
    """Classify, count and refuse outbound attempts for one running service."""

    def __init__(self, *, allowlist: Sequence[str],
                 hardware: Mapping[str, Any] | None = None,
                 hardware_source: str = "not declared") -> None:
        self.allowlist = tuple(sorted({str(item) for item in allowlist}))
        surfaces = dict(hardware or _FALLBACK_HARDWARE)
        self.hardware_ports = frozenset(int(port) for port in surfaces.get("tcpPorts", ()))
        self.hardware_executables = frozenset(
            str(name) for name in surfaces.get("executables", ()))
        self.hardware_source = str(hardware_source)
        self._lock = threading.RLock()
        self._installed = False
        self._ever_installed = False
        self._clock = ObservedClock()
        self._socket_connect: Callable[..., Any] | None = None
        self._socket_close: Callable[..., Any] | None = None
        self._popen_init: Callable[..., Any] | None = None
        self._getaddrinfo: Callable[..., Any] | None = None
        self._methods: tuple[str, ...] = ()
        self._mechanisms: dict[str, str] = {}
        #: authority -> (protocol, role), declared from the deployment vector by
        #: the composition root.  An undeclared target is UNATTRIBUTED, which is
        #: a finding rather than a default that flatters the run.
        self._declared: dict[str, tuple[str, str]] = {}
        self._scenario = _ScenarioCounters()
        self._process = _ScenarioCounters()
        self.attribution_ambiguities: list[dict[str, Any]] = []
        self._live_connections: dict[str, list[tuple[str, int]]] = {}

    # -- declaration ------------------------------------------------------
    def bind_clock(self, clock: ObservedClock) -> None:
        """Share the run's observed clock so ledger instants line up."""
        with self._lock:
            self._clock = clock

    def declare_target(self, authority: str, *, protocol: str, role: str) -> None:
        """Declare what a known authority IS, from the deployment vector."""
        if protocol not in PROTOCOLS:
            raise EgressViolation("unknown target protocol %s" % protocol)
        if role not in ROLES:
            raise EgressViolation("unknown target role %s" % role)
        with self._lock:
            self._declared[str(authority)] = (protocol, role)

    # -- observation -----------------------------------------------------
    def begin_scenario(self) -> None:
        """Start a fresh per-scenario slice; process totals keep accumulating."""
        with self._lock:
            self._scenario = _ScenarioCounters()
            self.attribution_ambiguities = []

    def methods_armed(self) -> tuple[str, ...]:
        with self._lock:
            return self._methods

    def observations(self, *, scope: str = "scenario") -> dict[str, Any]:
        with self._lock:
            counters = self._scenario if scope == "scenario" else self._process
            return {
                "guardInstalled": self._ever_installed,
                "guardArmedNow": self._installed,
                "guardMethods": list(self._methods),
                "interceptionMechanisms": dict(self._mechanisms),
                "scope": scope,
                "hardwareDefinitionSource": self.hardware_source,
                "authorityAllowlist": list(self.allowlist),
                "attemptedAuthorities": sorted(counters.attempted_authorities),
                "resolvedNames": sorted(counters.resolved_names),
                "connectionAttempts": counters.connection_attempts,
                "approvedConnectionAttempts": counters.approved_attempts,
                "forbiddenEgressAttempts": counters.forbidden_attempts,
                "hardwareCalls": counters.hardware_calls,
                "externalLiveTargetCalls": counters.external_live_target_calls,
                "violations": [dict(item) for item in counters.violations],
                "targetLedger": [entry.as_dict() for _key, entry in sorted(
                    counters.ledger.items())],
                "peerAttributionAmbiguities": [
                    dict(item) for item in self.attribution_ambiguities],
            }

    # -- classification --------------------------------------------------
    def _classify(self, authority: str, port: int) -> str:
        if authority in self.allowlist:
            return "APPROVED"
        return "HARDWARE_CONTROL" if port in self.hardware_ports else EXTERNAL_LIVE_TARGET

    def _record(self, authority: str, port: int, protocol: str, kind: str) -> str:
        """Count one outbound attempt in both scopes and return its class."""
        classification = self._classify(authority, int(port))
        allowed = classification == "APPROVED"
        at = self._clock.now()
        declared_protocol, role = self._declared.get(
            authority, (protocol, "UNATTRIBUTED"))
        for counters in (self._process, self._scenario):
            counters.connection_attempts += 1
            if authority not in counters.attempted_authorities:
                counters.attempted_authorities.append(authority)
            key = (authority, declared_protocol)
            entry = counters.ledger.get(key)
            if entry is None:
                entry = _LedgerEntry(authority, declared_protocol, role, allowed, at)
                counters.ledger[key] = entry
            entry.call_count += 1
            entry.last_at = at
            entry.allowed = entry.allowed and allowed
            if allowed:
                counters.approved_attempts += 1
                continue
            counters.forbidden_attempts += 1
            if classification == "HARDWARE_CONTROL":
                counters.hardware_calls += 1
            elif classification == EXTERNAL_LIVE_TARGET:
                counters.external_live_target_calls += 1
            counters.violations.append(
                {"kind": kind, "authority": authority,
                 "classification": classification})
        return classification

    def note_connect(self, host: Any, port: Any) -> str:
        """Record and classify one attempt; raise when it is not approved."""
        authority = _authority(host, port)
        with self._lock:
            classification = self._record(authority, int(port), "HTTPS", "CONNECT")
            if classification == "APPROVED":
                return classification
        raise EgressViolation(
            "outbound connection to %s is outside the deployment's declared "
            "authority allowlist %s (classified %s)"
            % (authority, list(self.allowlist), classification))

    def note_ssh_open(self, host: Any, port: Any, *, library: str) -> str:
        """Record and classify one SSH transport open (NETCONF or SFTP)."""
        authority = _authority(host, port)
        with self._lock:
            protocol = self._declared.get(authority, ("NETCONF_SSH", ""))[0]
            if protocol == "HTTPS":
                protocol = "NETCONF_SSH"
            classification = self._record(
                authority, int(port), protocol, "SSH_OPEN")
            if classification == "APPROVED":
                return classification
        raise EgressViolation(
            "SSH transport open to %s through %s is outside the declared "
            "authority allowlist (classified %s)"
            % (authority, str(library), classification))

    def note_spawn(self, argv: Any) -> None:
        """Record and classify one process spawn; raise on hardware tooling.

        The harness speaks SSH in process, so spawning ``ssh``/``scp``/``sftp``
        is always a violation: a subprocess would route around every in-process
        interception point this guard arms.
        """
        if isinstance(argv, (str, bytes)):
            candidates: list[Any] = [argv]
        elif isinstance(argv, Iterable):
            candidates = list(argv)[:1]
        else:  # pragma: no cover - defensive
            candidates = []
        for candidate in candidates:
            name = Path(str(candidate)).name
            if name not in self.hardware_executables:
                continue
            with self._lock:
                for counters in (self._process, self._scenario):
                    counters.hardware_calls += 1
                    counters.forbidden_attempts += 1
                    counters.violations.append(
                        {"kind": "SPAWN", "executable": name,
                         "classification": "HARDWARE_CONTROL"})
            raise EgressViolation(
                "spawning %s is a hardware-control call; the live-O1 profile "
                "never touches hardware" % name)

    def note_resolve(self, hostname: Any) -> None:
        """Record a name resolution.  Resolution alone is never a violation.

        Resolved names are kept apart from ``attemptedAuthorities``: a hostname
        is not an authority, and mixing the two would let a name that was only
        looked up read as a target that was contacted.
        """
        text = str(hostname)
        if not text:
            return
        with self._lock:
            for counters in (self._process, self._scenario):
                if text not in counters.resolved_names:
                    counters.resolved_names.append(text)

    def note_local_source(self, descriptor: Any, local: Any, remote: Any) -> None:
        """Record one LIVE outbound connection by both of its endpoints."""
        try:
            fileno = int(descriptor)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return
        if fileno < 0:
            return
        with self._lock:
            key = _authority(local[0], local[1])
            self._live_connections.setdefault(key, []).append(
                (_authority(remote[0], remote[1]), fileno))

    def forget_descriptor(self, descriptor: Any) -> None:
        """Release a closed descriptor; its source port may now be reused."""
        try:
            fileno = int(descriptor)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return
        if fileno < 0:
            return
        with self._lock:
            for key in list(self._live_connections):
                live = [entry for entry in self._live_connections[key]
                        if entry[1] != fileno]
                if live:
                    self._live_connections[key] = live
                else:
                    del self._live_connections[key]

    def attribute_peer(self, client_address: Any, listener_authority: str) -> str:
        """``UPPER_SELF``, ``COUNTERPART`` or ``UNATTRIBUTED``.

        A request is attributed to the upper only when a live connection this
        process opened has exactly this local endpoint *and* is connected to the
        listener that received the request.  Ambiguity is reported, never
        optimistically resolved: guessing ``UPPER_SELF`` is exactly how a forged
        origin would slip through.
        """
        if not isinstance(client_address, tuple) or len(client_address) < 2:
            return "UNATTRIBUTED"
        key = _authority(client_address[0], client_address[1])
        with self._lock:
            entries = self._live_connections.get(key) or []
            live = [(remote, fileno) for remote, fileno in entries
                    if _descriptor_still_is(fileno, key, remote)]
            if live:
                self._live_connections[key] = live
            else:
                self._live_connections.pop(key, None)
                return "COUNTERPART"
            remotes = {remote for remote, _fileno in live}
            if len(remotes) > 1:
                self.attribution_ambiguities.append(
                    {"clientAddress": key, "liveRemotes": sorted(remotes),
                     "listener": listener_authority})
                return "UNATTRIBUTED"
            return "UPPER_SELF" if remotes.pop() == listener_authority else "COUNTERPART"

    # -- lifecycle -------------------------------------------------------
    def install(self) -> "EgressGuard":
        with self._lock:
            if self._installed:
                raise EgressViolation("the egress guard is already installed")
            guard = self
            self._socket_connect = socket.socket.connect
            self._socket_close = socket.socket._real_close  # noqa: SLF001
            self._popen_init = subprocess.Popen.__init__
            self._getaddrinfo = socket.getaddrinfo
            original_connect = self._socket_connect
            original_close = self._socket_close
            original_init = self._popen_init
            original_getaddrinfo = self._getaddrinfo

            def _connect(sock: socket.socket, address: Any) -> Any:
                inet = sock.family in (socket.AF_INET, socket.AF_INET6)
                if inet and isinstance(address, tuple) and len(address) >= 2:
                    guard.note_connect(address[0], address[1])
                result = original_connect(sock, address)
                if inet and isinstance(address, tuple) and len(address) >= 2:
                    try:
                        local = sock.getsockname()
                        descriptor = sock.fileno()
                    except OSError:  # pragma: no cover - closed under us
                        local, descriptor = None, -1
                    if isinstance(local, tuple) and len(local) >= 2:
                        guard.note_local_source(descriptor, local, address)
                return result

            def _real_close(sock: socket.socket, *args: Any, **kwargs: Any) -> Any:
                try:
                    descriptor = sock.fileno()
                except OSError:  # pragma: no cover - already closed
                    descriptor = -1
                guard.forget_descriptor(descriptor)
                return original_close(sock, *args, **kwargs)

            def _init(self_popen: Any, args: Any = (), *rest: Any, **kwargs: Any) -> Any:
                guard.note_spawn(args)
                return original_init(self_popen, args, *rest, **kwargs)

            def _resolve(host: Any, *rest: Any, **kwargs: Any) -> Any:
                guard.note_resolve(host)
                return original_getaddrinfo(host, *rest, **kwargs)

            socket.socket.connect = _connect  # type: ignore[assignment]
            socket.socket._real_close = _real_close  # type: ignore[assignment]  # noqa: SLF001
            subprocess.Popen.__init__ = _init  # type: ignore[assignment]
            socket.getaddrinfo = _resolve  # type: ignore[assignment]
            methods = ["SOCKET_CONNECT", "SUBPROCESS_SPAWN", "DNS_RESOLVE"]
            mechanisms = {
                "SOCKET_CONNECT": "socket.socket.connect",
                "SUBPROCESS_SPAWN": "subprocess.Popen.__init__",
                "DNS_RESOLVE": "socket.getaddrinfo",
            }
            for method, mechanism in self._install_ssh_interception():
                methods.append(method)
                mechanisms[method] = mechanism
            self._methods = tuple(sorted(set(methods)))
            self._mechanisms = mechanisms
            missing = [name for name in MANDATORY_METHODS if name not in self._methods]
            if missing:  # pragma: no cover - defensive; the seam is unconditional
                raise EgressViolation(
                    "the egress guard could not arm %s" % ", ".join(missing))
            self._installed = True
            self._ever_installed = True
            return self

    def _install_ssh_interception(self) -> list[tuple[str, str]]:
        """Arm the SSH transport and SFTP client points.

        When a third-party SSH library is importable its transport and SFTP
        client constructors are wrapped, so a client that never called the
        guard is still observed.  When it is not, the mandatory in-process seam
        (:meth:`note_ssh_open`, which every SSH client in this release must call
        before opening a transport) is what is armed, and the mechanism string
        records which of the two it was -- the point is never silently dropped.
        """
        armed: list[tuple[str, str]] = [
            ("SSH_TRANSPORT_OPEN", "oran.release.lo1.egress.EgressGuard.note_ssh_open"),
            ("SFTP_CLIENT_OPEN", "oran.release.lo1.egress.EgressGuard.note_ssh_open"),
        ]
        try:  # pragma: no cover - exercised only where paramiko is installed
            import paramiko  # type: ignore
        except Exception:  # noqa: BLE001 - any import failure means "not present"
            return armed
        guard = self
        original_start = paramiko.Transport.start_client

        def _start_client(transport: Any, *args: Any, **kwargs: Any) -> Any:
            peer = getattr(transport, "sock", None)
            try:
                host, port = peer.getpeername()[:2]
            except Exception:  # noqa: BLE001 - defensive
                host, port = "unknown", 0
            guard.note_ssh_open(host, port, library="paramiko")
            return original_start(transport, *args, **kwargs)

        paramiko.Transport.start_client = _start_client  # type: ignore[assignment]
        self._paramiko_start_client = original_start  # type: ignore[attr-defined]
        return [
            ("SSH_TRANSPORT_OPEN", "paramiko.Transport.start_client"),
            ("SFTP_CLIENT_OPEN",
             "oran.release.lo1.egress.EgressGuard.note_ssh_open"),
        ]

    def uninstall(self) -> None:
        with self._lock:
            if not self._installed:
                return
            if self._socket_connect is not None:
                socket.socket.connect = self._socket_connect  # type: ignore[assignment]
            if self._socket_close is not None:
                socket.socket._real_close = self._socket_close  # type: ignore[assignment]  # noqa: SLF001
            if self._popen_init is not None:
                subprocess.Popen.__init__ = self._popen_init  # type: ignore[assignment]
            if self._getaddrinfo is not None:
                socket.getaddrinfo = self._getaddrinfo  # type: ignore[assignment]
            original_start = getattr(self, "_paramiko_start_client", None)
            if original_start is not None:  # pragma: no cover - paramiko only
                import paramiko  # type: ignore

                paramiko.Transport.start_client = original_start  # type: ignore[assignment]
                self._paramiko_start_client = None  # type: ignore[attr-defined]
            self._socket_connect = None
            self._socket_close = None
            self._popen_init = None
            self._getaddrinfo = None
            self._installed = False

    def __enter__(self) -> "EgressGuard":
        return self.install()

    def __exit__(self, *_exc: Any) -> None:
        self.uninstall()


def _descriptor_still_is(descriptor: int, local: str, remote: str) -> bool:
    """Does ``descriptor`` STILL name a socket with exactly these endpoints?

    Asking the kernel is authoritative; a registry that only removes on an
    observed close leaks entries and lets a recycled descriptor inherit an
    identity.  ``socket.socket(fileno=...)`` adopts the descriptor, so it is
    detached again immediately: ``detach`` releases ownership WITHOUT closing.
    """
    if descriptor < 0:
        return False
    probe: socket.socket | None = None
    try:
        probe = socket.socket(fileno=descriptor)
        return (_authority(*probe.getsockname()[:2]) == local
                and _authority(*probe.getpeername()[:2]) == remote)
    except (OSError, ValueError):
        return False
    finally:
        if probe is not None:
            probe.detach()
