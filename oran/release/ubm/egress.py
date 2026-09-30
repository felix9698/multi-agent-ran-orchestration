"""Runtime egress guard: the source of the capture's external-call counters.

``externalCalls.externalLiveTargetCalls`` and ``externalCalls.hardwareCalls``
used to be literal zeros in the capture writer.  Even when true, a literal is a
*claim*: a reader combining this capture with their own evidence would be
taking an unverifiable constant.  This module makes them **observations**.

Every outbound connection the process attempts passes through here, because the
guard wraps ``socket.socket.connect`` itself rather than any one client class --
a call that bypassed :mod:`oran.release.ubm.clients` would still be seen.  Each
attempt is classified against the deployment's own authority allowlist (the four
upper listeners plus the lower A1 origin, all derived from the deployment
vector), and an attempt outside it raises immediately, so a stray live-target
call aborts the request instead of being tallied after the fact.  The counters
the capture publishes are read off that classification.

Hardware is classified the same way and from declared bytes:
``release-gates.1.0.0.json#/hardwareControlSurfaces`` names the TCP ports and
executables that constitute hardware control on this testbed, so "no hardware
call" is a measurement against a published definition rather than an assertion.
Process spawns are guarded too, because that is the one hardware path that is
not a socket.

The guard is fail-closed in both directions: it refuses to arm twice, and
:meth:`EgressGuard.observations` reports whether it was installed at all, so a
capture written with the guard absent cannot be mistaken for a clean run.
"""

from __future__ import annotations

import socket
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

__all__ = [
    "EgressGuard",
    "EgressViolation",
    "load_hardware_control_surfaces",
]

#: Fallback used only when the spec file cannot be read; the guard reports the
#: source it actually used, so a fallback is never silently indistinguishable
#: from the declared definition.
_FALLBACK_HARDWARE = {
    "tcpPorts": [22, 830, 9091, 49152, 49153],
    "executables": ["uhd_find_devices", "uhd_usrp_probe", "nr-softmodem",
                    "nr-uesoftmodem", "ssh", "telnet"],
}


class EgressViolation(RuntimeError):
    """An outbound attempt left the deployment's declared authority set."""


def _descriptor_still_is(descriptor: int, local: str, remote: str) -> bool:
    """Does ``descriptor`` STILL name a socket with exactly these endpoints?

    Authoritative rather than bookkeeping: a close hook cannot see a socket
    reclaimed by refcount (CPython closes that descriptor in C, below any
    Python ``close``), so a registry that only removes on an observed close
    leaks entries and lets a recycled descriptor inherit an identity.  Asking
    the kernel removes that whole class of error.

    ``socket.socket(fileno=...)`` adopts the descriptor, so it is detached
    again immediately -- ``detach`` releases ownership WITHOUT closing, leaving
    the real owner untouched.
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


def _authority(host: Any, port: Any) -> str:
    text = str(host)
    return "[%s]:%d" % (text, int(port)) if ":" in text else "%s:%d" % (text, int(port))


def load_hardware_control_surfaces(spec_path: Path | None) -> tuple[dict[str, Any], str]:
    """Read the declared hardware-control definition; report where it came from."""
    if spec_path is not None:
        try:
            import json

            document = json.loads(Path(spec_path).read_text(encoding="utf-8"))
            declared = document["hardwareControlSurfaces"]
            return ({"tcpPorts": [int(port) for port in declared["tcpPorts"]],
                     "executables": [str(name) for name in declared["executables"]]},
                    "release-gates.1.0.0.json#/hardwareControlSurfaces")
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return (dict(_FALLBACK_HARDWARE),
            "built-in fallback; the declared definition was unreadable")


class _ScenarioCounters:
    """The per-scenario slice of what the guard saw."""

    def __init__(self) -> None:
        self.attempted_authorities: list[str] = []
        self.connection_attempts = 0
        self.approved_attempts = 0
        self.external_live_target_calls = 0
        self.hardware_calls = 0
        self.violations: list[dict[str, Any]] = []


class EgressGuard:
    """Classify, count and refuse outbound attempts for one running service."""

    def __init__(self, *, allowlist: Sequence[str],
                 hardware: Mapping[str, Any] | None = None,
                 hardware_source: str = "not declared") -> None:
        self.allowlist = tuple(sorted(set(str(item) for item in allowlist)))
        surfaces = dict(hardware or _FALLBACK_HARDWARE)
        self.hardware_ports = frozenset(int(port) for port in surfaces.get("tcpPorts", ()))
        self.hardware_executables = frozenset(
            str(name) for name in surfaces.get("executables", ()))
        self.hardware_source = str(hardware_source)
        self._lock = threading.RLock()
        self._installed = False
        #: Whether the guard was ever armed.  ``guardInstalled`` reports THIS,
        #: not the instantaneous state: a capture is asking "were these counters
        #: measured?", and a guard that was armed for the whole scenario and
        #: then disarmed at shutdown measured them.
        self._ever_installed = False
        self._socket_connect: Callable[..., Any] | None = None
        self._socket_close: Callable[..., Any] | None = None
        self._popen_init: Callable[..., Any] | None = None
        # Two scopes.  The PROCESS totals never reset and belong to readiness.
        # The SCENARIO counters are what a capture publishes, because a capture
        # describes one scenario: a cumulative counter would make the document
        # depend on the order the scenarios happened to run in, and G-DET-4
        # would (correctly) report that as state leakage.
        self.attempted_authorities: list[str] = []
        self.connection_attempts = 0
        self.approved_attempts = 0
        self.external_live_target_calls = 0
        self.hardware_calls = 0
        self.violations: list[dict[str, Any]] = []
        self._scenario = _ScenarioCounters()
        #: Requests whose originator could not be established.  A finding, not a
        #: silent default: guessing UPPER_SELF here is exactly how a forged
        #: origin would slip through.
        self.attribution_ambiguities: list[dict[str, Any]] = []
        #: LIVE connections this process opened: local endpoint -> list of
        #: (remote endpoint, file descriptor).  A source port is reusable, so
        #: recording the local endpoint alone lets an unrelated connection that
        #: inherits the port be mistaken for ours.  Attribution therefore
        #: requires the local endpoint AND the remote endpoint AND an entry that
        #: has not been closed.
        #:
        #: The key is the descriptor rather than the socket object because
        #: ``ssl.SSLContext.wrap_socket`` detaches the original socket and
        #: returns a new object over the same descriptor -- holding the object
        #: would make every TLS connection look closed the moment it was
        #: wrapped.  ``socket.socket.close`` is wrapped so an entry disappears
        #: when its descriptor is released, and ``SSLSocket`` inherits that.
        self._live_connections: dict[str, list[tuple[str, int]]] = {}

    # -- observation -----------------------------------------------------
    def begin_scenario(self) -> None:
        """Start a fresh per-scenario slice; process totals keep accumulating."""
        with self._lock:
            self._scenario = _ScenarioCounters()
        #: Requests whose originator could not be established.  A finding, not a
        #: silent default: guessing UPPER_SELF here is exactly how a forged
        #: origin would slip through.
        self.attribution_ambiguities: list[dict[str, Any]] = []

    def observations(self, *, scope: str = "scenario") -> dict[str, Any]:
        """Guard observations. ``scope`` is ``"scenario"`` or ``"process"``."""
        with self._lock:
            counters = self._scenario if scope == "scenario" else self
            return {
                "guardInstalled": self._ever_installed,
                "guardArmedNow": self._installed,
                "guardMethod": "socket.socket.connect and subprocess.Popen are "
                               "wrapped for the lifetime of the service, so an "
                               "attempt that bypasses the release's own client "
                               "classes is still observed",
                "scope": scope,
                "hardwareDefinitionSource": self.hardware_source,
                "authorityAllowlist": list(self.allowlist),
                "attemptedAuthorities": sorted(counters.attempted_authorities),
                "connectionAttempts": counters.connection_attempts,
                "approvedConnectionAttempts": counters.approved_attempts,
                "externalLiveTargetCalls": counters.external_live_target_calls,
                "hardwareCalls": counters.hardware_calls,
                "violations": [dict(item) for item in counters.violations],
                "liveOutboundConnections": sum(
                    len(value) for value in self._live_connections.values()),
                "peerAttributionAmbiguities": [
                    dict(item) for item in self.attribution_ambiguities],
            }

    # -- classification --------------------------------------------------
    def _classify(self, authority: str, port: int) -> str:
        if authority in self.allowlist:
            return "APPROVED"
        return "HARDWARE_CONTROL" if port in self.hardware_ports else "EXTERNAL_LIVE_TARGET"

    def note_connect(self, host: Any, port: Any) -> str:
        """Record and classify one attempt; raise when it is not approved."""
        authority = _authority(host, port)
        with self._lock:
            classification = self._classify(authority, int(port))
            for counters in (self, self._scenario):
                counters.connection_attempts += 1
                if authority not in counters.attempted_authorities:
                    counters.attempted_authorities.append(authority)
                if classification == "APPROVED":
                    counters.approved_attempts += 1
                    continue
                counters.external_live_target_calls += 1
                if classification == "HARDWARE_CONTROL":
                    counters.hardware_calls += 1
                counters.violations.append(
                    {"kind": "CONNECT", "authority": authority,
                     "classification": classification})
            if classification == "APPROVED":
                return classification
        raise EgressViolation(
            "outbound connection to %s is outside the deployment's declared "
            "authority allowlist %s (classified %s)"
            % (authority, list(self.allowlist), classification))

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

        A request is attributed to the upper only when a **live** connection
        this process opened has exactly this local endpoint *and* is connected
        to the listener that received the request.  A recycled source port
        pointing somewhere else does not match, and an ambiguous local endpoint
        -- two live connections sharing it, which should be impossible -- is
        reported as ``UNATTRIBUTED`` rather than optimistically claimed.
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

    def note_spawn(self, argv: Any) -> None:
        """Record and classify one process spawn; raise on hardware tooling."""
        if isinstance(argv, (str, bytes)):
            candidates = [argv]
        elif isinstance(argv, Iterable):
            candidates = list(argv)[:1]
        else:  # pragma: no cover - defensive
            candidates = []
        for candidate in candidates:
            name = Path(str(candidate)).name
            if name not in self.hardware_executables:
                continue
            with self._lock:
                for counters in (self, self._scenario):
                    counters.hardware_calls += 1
                    counters.external_live_target_calls += 1
                    counters.violations.append(
                        {"kind": "SPAWN", "executable": name,
                         "classification": "HARDWARE_CONTROL"})
            raise EgressViolation(
                "spawning %s is a hardware-control call; the bilateral profile "
                "never touches hardware" % name)

    # -- lifecycle -------------------------------------------------------
    def install(self) -> "EgressGuard":
        with self._lock:
            if self._installed:
                raise EgressViolation("the egress guard is already installed")
            guard = self
            self._socket_connect = socket.socket.connect
            self._socket_close = socket.socket._real_close  # noqa: SLF001
            self._popen_init = subprocess.Popen.__init__
            original_connect = self._socket_connect
            original_close = self._socket_close
            original_init = self._popen_init

            def _connect(sock: socket.socket, address: Any) -> Any:  # noqa: ANN401
                inet = sock.family in (socket.AF_INET, socket.AF_INET6)
                if inet and isinstance(address, tuple) and len(address) >= 2:
                    guard.note_connect(address[0], address[1])
                result = original_connect(sock, address)
                if inet and isinstance(address, tuple) and len(address) >= 2:
                    try:
                        local = sock.getsockname()
                    except OSError:  # pragma: no cover - closed under us
                        local = None
                    if isinstance(local, tuple) and len(local) >= 2:
                        try:
                            descriptor = sock.fileno()
                        except OSError:  # pragma: no cover - closed under us
                            descriptor = -1
                        guard.note_local_source(descriptor, local, address)
                return result

            def _real_close(sock: socket.socket, *args: Any, **kwargs: Any) -> Any:
                # ``_real_close`` is the single funnel CPython uses for BOTH an
                # explicit ``close()`` and a garbage-collected ``__del__``.
                # Hooking ``close()`` alone leaks entries for sockets that were
                # never closed explicitly, and a leaked entry lets a recycled
                # descriptor inherit an identity -- which showed up as a
                # nondeterministic ``peer`` between two runs.
                try:
                    descriptor = sock.fileno()
                except OSError:  # pragma: no cover - already closed
                    descriptor = -1
                guard.forget_descriptor(descriptor)
                return original_close(sock, *args, **kwargs)

            def _init(self_popen: Any, args: Any = (), *rest: Any, **kwargs: Any) -> Any:
                guard.note_spawn(args)
                return original_init(self_popen, args, *rest, **kwargs)

            socket.socket.connect = _connect  # type: ignore[assignment]
            socket.socket._real_close = _real_close  # type: ignore[assignment]  # noqa: SLF001
            subprocess.Popen.__init__ = _init  # type: ignore[assignment]
            self._installed = True
            self._ever_installed = True
            return self

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
            self._socket_connect = None
            self._socket_close = None
            self._popen_init = None
            self._installed = False

    def __enter__(self) -> "EgressGuard":
        return self.install()

    def __exit__(self, *_exc: Any) -> None:
        self.uninstall()
