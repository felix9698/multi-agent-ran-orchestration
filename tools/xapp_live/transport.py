"""A per-command telnet client for the OAI gNB's ``ci`` shell.

One connection per command, because the OAI telnet server serves a single
client at a time: holding one open would starve every other tool on the
testbed, and the reconnect logic is simpler than keep-alive probing.  The
framing rules are the ones the hardware-verified client in
``executor/oai_executor.py`` established, and they are here rather than
imported because this path must not pull the legacy coordinator stack into a
Phase-3 run:

* read until the shell's prompt (``..._gnb> ``) or the read timeout;
* drop the echoed command line and the trailing prompt, and nothing else --
  a command whose real output is empty (the arg-less ``ci sched_prio`` with no
  UE connected) must come back as ``""``, not as the prompt text;
* only a single-token final line ending in ``>`` counts as the prompt, because
  legitimate output lines end in ``>`` too (usage text like
  ``ci prbcap <n> <rnti>``).

Failures are raised, never swallowed into an empty string:
:class:`assurance.xapps.live_actuation.TelnetActuationTransport` treats a raised
transport error during a write as an *unknown* outcome and latches, which is
the correct reading and only stays correct if this client does not invent a
response.
"""

from __future__ import annotations

import re
import socket

__all__ = ["TelnetLineTransport", "TelnetLineTransportError",
           "build_transport"]

#: The gNB is launched with ``--telnetsrv.listenport 9091``; 9090 is taken by
#: leftover Open5GS services on PC1 loopback and a bind failure kills
#: nr-softmodem.
DEFAULT_TELNET_PORT = 9091

_PROMPT = re.compile(r"\S*>\s*")


class TelnetLineTransportError(OSError):
    """The command could not be delivered, or its response could not be read."""


class TelnetLineTransport:
    """``send(line) -> response``: one command line to one gNB telnet shell."""

    def __init__(self, *, host: str = "127.0.0.1",
                 port: int = DEFAULT_TELNET_PORT,
                 timeout: float = 3.0) -> None:
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"

    def __call__(self, line: str) -> str:
        try:
            sock = socket.create_connection((self.host, self.port),
                                            timeout=self.timeout)
        except OSError as exc:
            raise TelnetLineTransportError(
                f"cannot reach the gNB telnet shell at {self.endpoint}: "
                f"{exc}") from exc
        try:
            with sock:
                sock.settimeout(self.timeout)
                self._read_until_prompt(sock)          # banner + first prompt
                sock.sendall((line + "\n").encode())
                return self._clean(line, self._read_until_prompt(sock))
        except OSError as exc:
            raise TelnetLineTransportError(
                f"{line!r} failed on {self.endpoint}: {exc}") from exc

    # -- framing -----------------------------------------------------------

    @staticmethod
    def _clean(command: str, response: str) -> str:
        lines = [text for text in response.splitlines()
                 if text.strip() and not text.strip().startswith(command)]
        if lines and _PROMPT.fullmatch(lines[-1]):
            lines.pop()
        return "\n".join(lines).strip()

    @staticmethod
    def _read_until_prompt(sock: socket.socket,
                           max_bytes: int = 65536) -> str:
        data = b""
        while len(data) < max_bytes:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
            if data.rstrip(b" ").endswith(b">") or data.endswith(b"> "):
                break
        return data.decode(errors="replace")


def build_transport(endpoint: str, *,
                    timeout: float = 3.0) -> TelnetLineTransport:
    """``host[:port]`` to a transport, with the project's default port."""
    host, _, port = endpoint.partition(":")
    if not host.strip():
        raise TelnetLineTransportError(
            f"{endpoint!r} names no host; a live write needs one gNB")
    return TelnetLineTransport(host=host.strip(),
                               port=int(port) if port else DEFAULT_TELNET_PORT,
                               timeout=timeout)
