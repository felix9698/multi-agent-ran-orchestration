"""A scripted stand-in for the patched OAI ``ci`` telnet shell.

Not a test module (repo convention: non-``test*.py`` files beside the tests are
shared harnesses).  Nothing here opens a socket, a process or a file: it is a
callable that takes one command line and returns the text the real shell would
print, implemented from the checked-in handlers
(``oai_patches/d2_actionspace_runtime_knobs.patch`` and ``rfatt_cmd`` in
``common/utils/telnetsrv/telnetsrv_ci.c``) -- the same source the codecs were
written from, including the ``%.1f`` / ``%.3f`` / ``%04x`` print formats, the
``(uncapped)`` spellings, the ``ERROR_MSG_RET`` refusal texts, and the detail
that ``prbcap`` lists a UE only while its cap is non-zero.

It keeps state, so a write is visible to the next read: that is what makes an
apply/readback/rollback round trip a real round trip rather than an assertion
about a mock's call list.

Faults are injected the way the wire fails, not the way a stub fails:

``raise_on``
    the transport dies on the *n*-th matching command (a dropped connection);
``garbage_for``
    the shell answers something outside its own grammar;
``ignore_writes``
    the command is acknowledged with the value that is really live, which is
    how a write that silently did not take looks from outside.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence


class ScriptedGnbError(OSError):
    """The scripted transport dies mid-command."""


class ScriptedGnbTelnet:
    """One gNB's ``ci`` knobs, as the telnet shell would print them."""

    def __init__(
        self,
        *,
        tx_att_db: float = 12.0,
        dl_mcs: Sequence[int] = (0, 28),
        ul_mcs: Sequence[int] = (0, 28),
        cell_prb_cap: int = 0,
        ues: Optional[Mapping[int, Mapping[str, Any]]] = None,
        garbage_for: Optional[Mapping[str, str]] = None,
        raise_on: Optional[Mapping[str, int]] = None,
        ignore_writes: Sequence[str] = (),
    ) -> None:
        self.tx_att_db = float(tx_att_db)
        self.dl_min_mcs, self.dl_max_mcs = int(dl_mcs[0]), int(dl_mcs[1])
        self.ul_min_mcs, self.ul_max_mcs = int(ul_mcs[0]), int(ul_mcs[1])
        self.cell_prb_cap = int(cell_prb_cap)
        self.ues: Dict[int, Dict[str, Any]] = {
            int(rnti): {"pf": float(state.get("pf", 1.0)),
                        "cap": int(state.get("cap", 0))}
            for rnti, state in dict(ues or {}).items()
        }
        self.garbage_for = dict(garbage_for or {})
        self.raise_on = dict(raise_on or {})
        self.ignore_writes = tuple(ignore_writes)
        self.sent: List[str] = []
        self._seen: Dict[str, int] = {}

    # -- the injected transport surface ------------------------------------

    def __call__(self, line: str) -> str:
        self.sent.append(line)
        for prefix, nth in self.raise_on.items():
            if line.startswith(prefix):
                self._seen[prefix] = self._seen.get(prefix, 0) + 1
                if self._seen[prefix] == nth:
                    raise ScriptedGnbError(
                        f"connection reset while sending {line!r}")
        for prefix, text in self.garbage_for.items():
            if line.startswith(prefix):
                return text
        head, _, argument = line.partition(" ")
        if head != "ci":
            return "unknown command\n"
        command, _, rest = argument.partition(" ")
        handler = {
            "rfatt": self._rfatt,
            "mcs": self._mcs,
            "prbcap": self._prbcap,
            "sched_prio": self._sched_prio,
        }.get(command)
        if handler is None:
            return f"{command}: unknown command"
        return handler(rest.strip(), line)

    # -- the four handlers -------------------------------------------------

    def _rfatt(self, argument: str, line: str) -> str:
        if not argument:
            return f"current TX attenuation {self.tx_att_db:.1f} dB"
        att = float(argument.split()[0])
        if att < 0.0 or att > 60.0:
            return f"attenuation {att:.1f} out of range [0,60] dB"
        if not self._ignored(line):
            self.tx_att_db = att
        return f"TX attenuation set to {self.tx_att_db:.1f} dB"

    def _mcs(self, argument: str, line: str) -> str:
        if not argument:
            return (f"DL MCS cap [{self.dl_min_mcs}..{self.dl_max_mcs}] "
                    f"UL MCS cap [{self.ul_min_mcs}..{self.ul_max_mcs}]")
        fields = argument.split()
        max_mcs = int(fields[0])
        min_mcs = int(fields[1]) if len(fields) > 1 else -1
        if max_mcs < 0 or max_mcs > 28:
            return f"max_mcs {max_mcs} out of range [0,28]"
        if not self._ignored(line):
            self.dl_max_mcs = max_mcs
            if 0 <= min_mcs <= max_mcs:
                self.dl_min_mcs = min_mcs
            if self.dl_min_mcs > self.dl_max_mcs:
                self.dl_min_mcs = self.dl_max_mcs
        return f"DL MCS cap set to [{self.dl_min_mcs}..{self.dl_max_mcs}]"

    def _prbcap(self, argument: str, line: str) -> str:
        if not argument:
            if self.cell_prb_cap == 0:
                lines = ["DL PRB cap 0 (uncapped)"]
            else:
                lines = [f"DL PRB cap {self.cell_prb_cap}"]
            for rnti, state in sorted(self.ues.items()):
                if state["cap"] > 0:
                    lines.append(f"UE {rnti:04x} DL PRB cap {state['cap']}")
            return "\n".join(lines)
        fields = argument.split()
        n_prb = int(fields[0])
        if n_prb < 0 or n_prb > 275:
            return f"prb cap {n_prb} out of range [0,275]"
        if len(fields) == 1:
            if not self._ignored(line):
                self.cell_prb_cap = n_prb
            if self.cell_prb_cap == 0:
                return "DL PRB cap set to 0 (uncapped)"
            return f"DL PRB cap set to {self.cell_prb_cap}"
        rnti = self._fetch_rnti(fields[1])
        if rnti is None:
            return "RNTI needs to be [1,0xfffe]"
        if rnti not in self.ues:
            return f"could not find UE with RNTI {rnti:04x}"
        if not self._ignored(line):
            self.ues[rnti]["cap"] = n_prb
        live = self.ues[rnti]["cap"]
        if live == 0:
            return f"UE {rnti:04x} DL PRB cap set to 0 (uncapped)"
        return f"UE {rnti:04x} DL PRB cap set to {live}"

    def _sched_prio(self, argument: str, line: str) -> str:
        if not argument:
            return "\n".join(
                f"UE {rnti:04x} PF weight {state['pf']:.3f}"
                for rnti, state in sorted(self.ues.items()))
        fields = argument.split()
        weight = float(fields[0])
        if weight <= 0.0 or weight > 100.0:
            return f"weight {weight:.3f} out of range (0,100]"
        if len(fields) == 1:
            if len(self.ues) != 1:
                return ("could not identify UE (no UE, no such RNTI, or "
                        "multiple UEs)")
            rnti = next(iter(self.ues))
        else:
            resolved = self._fetch_rnti(fields[1])
            if resolved is None:
                return "RNTI needs to be [1,0xfffe]"
            rnti = resolved
        if rnti not in self.ues:
            return f"could not find UE with RNTI {rnti:04x}"
        if not self._ignored(line):
            self.ues[rnti]["pf"] = weight
        return f"UE {rnti:04x} PF weight set to {self.ues[rnti]['pf']:.3f}"

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _fetch_rnti(text: str) -> Optional[int]:
        try:
            rnti = int(text, 16)
        except ValueError:
            return None
        return rnti if 1 <= rnti < 0xFFFE else None

    def _ignored(self, line: str) -> bool:
        return any(line.startswith(prefix) for prefix in self.ignore_writes)
