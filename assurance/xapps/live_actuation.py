"""Live actuation backend for the specialist xApp executors (lab telnet path).

The hardware-free executors in :mod:`assurance.xapps.executors` write through a
:class:`~assurance.xapps.executors.HardwareFreeConfigStore` -- an in-memory
surface with a ``read(axis)`` / ``apply(axis, value)`` interface.  This module
supplies a second backend with the *same* interface, one that drives the real
gNB over the patched OAI ``ci`` telnet shell, so a specialist xApp can be
constructed with either backend and the executor logic is written once.

**What this path is, honestly.**  ``ci rfatt`` / ``ci mcs`` / ``ci prbcap`` /
``ci sched_prio`` (``oai_patches/d2_actionspace_runtime_knobs.patch``, rebased
for the Phase-1 build) are direct gNB controls.  By this repository's own
classification that is
:attr:`~assurance.contracts.capability.ActuatorPath.LAB_SETUP_PREPARATION`, not
the official ``R1 -> Non-RT RIC -> A1-P -> xApp -> FlexRIC -> E2SM -> OAI gNB``
chain (design section 9).  So:

* :attr:`TelnetActuationTransport.actuator_path` says ``LAB_SETUP_PREPARATION``
  and :class:`~assurance.gateway.registry.GatewayAdapterRegistry` refuses to
  register it.  The boundary is structural, not a habit;
* an effect produced here is a research measurement, never an objective effect
  and never OTA evidence for an objective family.  The manifests already record
  that (``assurance/xapps/registry.py`` ``_TELNET_KNOB_BLOCKER``), and nothing
  here changes a manifest's ``execution_path_state``.

**What it keeps.**  Every boundary the hardware-free path has:

* *single writer*.  Nothing is sent -- not even a read -- outside a
  :meth:`TelnetActuationTransport.permit_scope`, and a scope opens only after
  the executor's own
  :meth:`~assurance.xapps.executors.SpecialistXAppExecutor._require_permit`
  accepts the presented :class:`~assurance.gateway.token.KernelToken`.  That
  check is *called*, not re-implemented: no permit, wrong kind, expired lease
  or a permit bound to another assignment means zero bytes on the wire;
* *snapshot -> apply -> readback -> rollback*.  The executor's existing loop
  drives it; the transport answers ``read`` from the arg-less form of each
  command and ``apply`` from its setting form, and the pre-apply value the
  executor recorded is what its existing rollback restores;
* *fail closed*.  A response that is not the exact grammar of the checked-in
  knob is never guessed at.  A refusal ("out of range", "could not find UE")
  means nothing was written; an unparsable response or a transport error during
  a write means the outcome is **unknown**, which latches the transport against
  further applies while still permitting the rollback that resolves it.

**No transport is opened here.**  ``send`` is an injected
``Callable[[str], str]`` -- one command line in, that command's cleaned
response out.  The composition root that owns a socket lives outside this
package (``tools/xapp_live/transport.py``), exactly as ``tools/g3ota`` owns the
one behind :mod:`assurance.live`.

**Coverage.**  Four of the six ownable action families are knob-backed:

===================== ========== =====================================
action                family     telnet grammar
===================== ========== =====================================
``ue-dl-prb-cap``     ``cap``    ``ci prbcap <n_prb> <rnti_hex>``
``scheduler-priority````priority````ci sched_prio <weight> <rnti_hex>``
``dl-rf-attenuation`` ``rfatt``  ``ci rfatt <tx_att_dB>``
``dl-mcs-bounds``     ``mcs``    ``ci mcs <max> [<min>]``
===================== ========== =====================================

The other two are not actuated here and are not faked:
:data:`NOT_ACTUATED_BY_TELNET` names both.  ``cell-steering`` keeps the
production A1 -> E2SM-RC path (``tools.g3ota.run_ota``, PIN_TO_CELL) that is
the one with OTA evidence; ``slice-prb-quota`` has no telnet knob in
``libtelnetsrv_ci.so`` at all.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType
from typing import (
    Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple,
)

from assurance.actions import action_catalog
from assurance.actions.catalog import (
    ActionContract, ActionParameterError, validate_action_parameters,
)
from assurance.contracts.capability import ActuatorPath, DeploymentBinding
from assurance.gateway.token import KernelToken, TokenKind
from assurance.xapps.assignment import XAppExecutionAssignment
from assurance.xapps.executors import (
    CellPowerXApp, ExecutorError, PermitRequiredError, SpecialistXAppExecutor,
    TrafficSteeringXApp, UeSchedulerXApp,
)
from assurance.xapps.manifest import XAppCapabilityManifest, XAppKind
from assurance.xapps.snapshot import CommonKpiSnapshot

__all__ = [
    "NOT_ACTUATED_BY_TELNET",
    "TELNET_ACTUATED_ACTIONS",
    "TELNET_KNOB_BASIS",
    "LinkAdaptationXApp",
    "LiveActuationError",
    "NotActuatedByTelnetError",
    "TelnetActuationTransport",
    "TelnetExchange",
    "TelnetGrammarError",
    "TelnetKnobCodec",
    "TelnetRefusedError",
    "TelnetTransportError",
    "build_specialist_executor",
    "command_detail",
    "telnet_codecs",
]

#: Where every grammar and every wire range in this module comes from.  The
#: strings below are read off the checked-in handlers, not invented here.
TELNET_KNOB_BASIS = (
    "oai_patches/d2_actionspace_runtime_knobs.patch "
    "(common/utils/telnetsrv/telnetsrv_ci.c: rfatt_cmd, mcs_cmd, prbcap_cmd, "
    "sched_prio_cmd); telnet shell 'ci' on the gNB's --telnetsrv.listenport"
)

#: Ownable actions this adapter deliberately does not drive, with the reason.
#: Absent from :data:`TELNET_ACTUATED_ACTIONS`, so an assignment for one of
#: them is refused before any permit is spent rather than silently no-op'd.
NOT_ACTUATED_BY_TELNET: Mapping[str, str] = MappingProxyType({
    "cell-steering": (
        "NOT_ACTUATED_BY_TELNET: steering stays on the production "
        "A1 -> Non-RT RIC -> E2SM-RC Style 3 path driven by tools.g3ota.run_ota "
        "(PIN_TO_CELL). That path is the one with retained OTA evidence; "
        "routing a handover through a lab telnet knob would replace verified "
        "evidence with an unverified one."
    ),
    "slice-prb-quota": (
        "NOT_ACTUATED_BY_TELNET: libtelnetsrv_ci.so exposes no slice knob. "
        "The Style 2 / Action 6 encoder fork "
        "(oai_patches/e2sm_rc_style2_action6_slice_prb.patch) is the only "
        "primitive, and it is not a telnet command."
    ),
})


class LiveActuationError(ExecutorError):
    """The live actuation backend refuses to act."""


class NotActuatedByTelnetError(LiveActuationError):
    """The requested action has no telnet knob on this deployment."""


class TelnetGrammarError(LiveActuationError):
    """A response is not the checked-in knob's grammar; nothing is guessed."""


class TelnetRefusedError(LiveActuationError):
    """The gNB refused the command and therefore wrote nothing."""


class TelnetTransportError(LiveActuationError):
    """The injected transport failed; whether a write landed may be unknown."""


# --------------------------------------------------------------------------- #
# per-family codecs: the exact grammar of the checked-in knobs
# --------------------------------------------------------------------------- #

#: Substrings the ``ci`` handlers print on refusal (``ERROR_MSG_RET``).  Each
#: one means the handler returned *before* touching the MAC, so a refusal is a
#: safe outcome: no write happened and nothing needs undoing.
_REFUSAL_MARKERS: Tuple[str, ...] = (
    "out of range",
    "no MAC present",
    "no RU present",
    "no UE found",
    "could not identify UE",
    "could not find UE",
    "RNTI needs to be",
    "does not support runtime gain setting",
    "no parameter allowed",
)

#: ``fetch_rnti`` accepts ``[1, 0xfffe)`` and refuses everything else.
_RNTI_MINIMUM = 0x0001
_RNTI_MAXIMUM = 0xFFFD


def _refusal_in(response: str) -> Optional[str]:
    """The refusal line in *response*, or ``None``."""
    for line in response.splitlines():
        stripped = line.strip()
        for marker in _REFUSAL_MARKERS:
            if marker in stripped:
                return stripped
    return None


def _wire_exact(value: Any, decimals: int) -> bool:
    """True when ``f"{value:.{decimals}f}"`` loses nothing.

    The knobs print what they stored with a fixed precision, so a value the
    wire cannot express exactly would read back as a different number and turn
    a correct apply into a readback mismatch.  Refusing it before the send is
    the honest half of that trade: the operator learns the actuator's
    resolution instead of learning that "the gNB did not take it".
    """
    scaled = float(value) * (10 ** decimals)
    return abs(scaled - round(scaled)) < 1e-9


@dataclass(frozen=True)
class TelnetTarget:
    """What one configuration axis addresses on the bound gNB."""

    axis: str
    scope: str
    rnti: Optional[int] = None
    cell_id: Optional[str] = None


class TelnetKnobCodec:
    """One action family's telnet grammar, ranges and value checks.

    A codec owns four things and nothing else: which axis strings it answers
    for, the two command lines (read and write), how to read a value out of
    each response, and which values the wire can carry.  Ranges come from the
    frozen action catalog wherever the catalog states one -- they are imported,
    never restated -- and from the checked-in C handler where it states one the
    catalog does not (see :data:`TELNET_KNOB_BASIS`).
    """

    action_id: str = ""
    family: str = ""
    command: str = ""
    scope: str = ""
    axis_regex: re.Pattern = re.compile(r"(?!)")

    def __init__(self, contract: ActionContract) -> None:
        if contract.action_id != self.action_id:
            raise LiveActuationError(
                f"{type(self).__name__} takes the {self.action_id!r} contract, "
                f"got {contract.action_id!r}")
        self.contract = contract

    # -- axis resolution ---------------------------------------------------

    def target_for(self, axis: str) -> Optional[TelnetTarget]:
        """The target *axis* addresses, or ``None`` when it is not ours."""
        raise NotImplementedError

    def check_parameters(self, parameters: Mapping[str, Any], *,
                         cell_id: str, selector: Mapping[str, Any]) -> None:
        """Refuse an inadmissible assignment before a single byte is sent."""
        raise NotImplementedError

    def check_value(self, target: TelnetTarget, value: Any) -> None:
        """Refuse a value the wire cannot carry, before the write is sent."""
        raise NotImplementedError

    # -- the wire ----------------------------------------------------------

    def read_line(self, target: TelnetTarget) -> str:
        """The arg-less form, which prints the knob's current value(s)."""
        return self.command

    def parse_read(self, target: TelnetTarget, response: str) -> Any:
        raise NotImplementedError

    def write_line(self, target: TelnetTarget, value: Any) -> str:
        raise NotImplementedError

    def parse_write(self, target: TelnetTarget, value: Any,
                    response: str) -> Any:
        raise NotImplementedError

    # -- shared helpers ----------------------------------------------------

    def _catalog_bounds(self, name: str) -> Tuple[Optional[Any], Optional[Any]]:
        for parameter in self.contract.binding.parameters:
            if parameter.name == name:
                return parameter.minimum, parameter.maximum
        raise LiveActuationError(
            f"{self.action_id}: the catalog contract has no parameter {name!r}")

    def _check_rnti(self, rnti: Any) -> int:
        if isinstance(rnti, bool) or not isinstance(rnti, int):
            raise LiveActuationError(
                f"{self.action_id}: rnti must be an int, got {rnti!r}")
        if not _RNTI_MINIMUM <= rnti <= _RNTI_MAXIMUM:
            raise LiveActuationError(
                f"{self.action_id}: RNTI {rnti:#06x} is outside the "
                f"[{_RNTI_MINIMUM:#x},{_RNTI_MAXIMUM:#x}] the gNB's fetch_rnti "
                "accepts")
        return rnti

    @staticmethod
    def _lines(response: str) -> Tuple[str, ...]:
        return tuple(line.strip() for line in response.splitlines()
                     if line.strip())


class UeDlPrbCapCodec(TelnetKnobCodec):
    """``ci prbcap`` -- the per-UE downlink PRB grant ceiling.

    Grammar (``prbcap_cmd``)::

        ci prbcap                 -> "DL PRB cap 0 (uncapped)" | "DL PRB cap <n>"
                                     then "UE <rnti> DL PRB cap <n>" per capped UE
        ci prbcap <n> <rnti_hex>  -> "UE <rnti> DL PRB cap set to <n>"
                                     ("... set to 0 (uncapped)" for n == 0)

    The arg-less listing prints a per-UE line **only for a UE whose cap is
    non-zero**, so an absent line means "uncapped", not "absent UE".  The two
    are told apart before any of this runs: the transport verifies the RNTI is
    on the cell with a ``ci sched_prio`` probe when it arms, and that probe --
    which lists *every* connected UE -- is the liveness check this listing
    cannot be.
    """

    action_id = "ue-dl-prb-cap"
    family = "cap"
    command = "ci prbcap"
    scope = "UE"
    axis_regex = re.compile(r"^ue/0x([0-9a-fA-F]{1,4})/dlPrbCap$")

    _CELL_LINE = re.compile(r"^DL PRB cap (\d+)(?: \(uncapped\))?$")
    _UE_LINE = re.compile(r"^UE ([0-9a-fA-F]{1,4}) DL PRB cap (\d+)$")
    _UE_ACK = re.compile(
        r"^UE ([0-9a-fA-F]{1,4}) DL PRB cap set to (\d+)(?: \(uncapped\))?$")

    def target_for(self, axis: str) -> Optional[TelnetTarget]:
        match = self.axis_regex.match(axis)
        if match is None:
            return None
        return TelnetTarget(axis=axis, scope=self.scope,
                            rnti=int(match.group(1), 16))

    def check_parameters(self, parameters: Mapping[str, Any], *,
                         cell_id: str, selector: Mapping[str, Any]) -> None:
        self._check_rnti(parameters["rnti"])
        self.check_value(TelnetTarget(axis="", scope=self.scope),
                         parameters["maxDlPrbs"])

    def check_value(self, target: TelnetTarget, value: Any) -> None:
        minimum, maximum = self._catalog_bounds("maxDlPrbs")
        if isinstance(value, bool) or not isinstance(value, int):
            raise LiveActuationError(
                f"{self.action_id}: maxDlPrbs must be an int, got {value!r}")
        if value < minimum or value > maximum:
            raise LiveActuationError(
                f"{self.action_id}: maxDlPrbs {value} is outside the catalog "
                f"range [{minimum},{maximum}]")

    def parse_read(self, target: TelnetTarget, response: str) -> int:
        lines = self._lines(response)
        if not any(self._CELL_LINE.match(line) for line in lines):
            raise TelnetGrammarError(
                f"{self.command}: no 'DL PRB cap <n>' line in {response!r}")
        for line in lines:
            match = self._UE_LINE.match(line)
            if match and int(match.group(1), 16) == target.rnti:
                return int(match.group(2))
        # No line for this UE: prbcap_cmd prints one only when the cap is
        # non-zero, and the UE's presence was established when the scope armed.
        return 0

    def write_line(self, target: TelnetTarget, value: Any) -> str:
        return f"{self.command} {int(value)} {target.rnti:x}"

    def parse_write(self, target: TelnetTarget, value: Any,
                    response: str) -> int:
        for line in self._lines(response):
            match = self._UE_ACK.match(line)
            if match is None:
                continue
            if int(match.group(1), 16) != target.rnti:
                raise TelnetGrammarError(
                    f"{self.command}: acknowledgement names UE "
                    f"{match.group(1)}, the write addressed "
                    f"{target.rnti:04x}")
            acked = int(match.group(2))
            if acked != int(value):
                raise TelnetGrammarError(
                    f"{self.command}: acknowledged {acked} PRB, {int(value)} "
                    "was written")
            return acked
        raise TelnetGrammarError(
            f"{self.command}: no 'DL PRB cap set to' acknowledgement in "
            f"{response!r}")


class SchedulerPriorityCodec(TelnetKnobCodec):
    """``ci sched_prio`` -- the per-UE proportional-fair weight.

    Grammar (``sched_prio_cmd``)::

        ci sched_prio                -> "UE <rnti> PF weight <w>" per connected UE
        ci sched_prio <w> <rnti_hex> -> "UE <rnti> PF weight set to <w>"

    ``w`` is printed ``%.3f``, so a weight with more resolution than a
    thousandth is refused here rather than read back as a different number.
    The arg-less form lists every connected UE and is therefore also the
    transport's RNTI liveness probe.
    """

    action_id = "scheduler-priority"
    family = "priority"
    command = "ci sched_prio"
    scope = "UE"
    axis_regex = re.compile(r"^ue/0x([0-9a-fA-F]{1,4})/pfWeight$")
    decimals = 3

    LIST_LINE = re.compile(r"^UE ([0-9a-fA-F]{1,4}) PF weight (\d+\.\d{3})$")
    _ACK = re.compile(r"^UE ([0-9a-fA-F]{1,4}) PF weight set to (\d+\.\d{3})$")

    def target_for(self, axis: str) -> Optional[TelnetTarget]:
        match = self.axis_regex.match(axis)
        if match is None:
            return None
        return TelnetTarget(axis=axis, scope=self.scope,
                            rnti=int(match.group(1), 16))

    def check_parameters(self, parameters: Mapping[str, Any], *,
                         cell_id: str, selector: Mapping[str, Any]) -> None:
        self._check_rnti(parameters["rnti"])
        self.check_value(TelnetTarget(axis="", scope=self.scope),
                         parameters["pfWeight"])

    def check_value(self, target: TelnetTarget, value: Any) -> None:
        minimum, maximum = self._catalog_bounds("pfWeight")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise LiveActuationError(
                f"{self.action_id}: pfWeight must be a number, got {value!r}")
        if value < minimum or value > maximum:
            raise LiveActuationError(
                f"{self.action_id}: pfWeight {value} is outside the catalog "
                f"range [{minimum},{maximum}]")
        if value <= 0:
            raise LiveActuationError(
                f"{self.action_id}: sched_prio_cmd refuses a weight <= 0")
        if not _wire_exact(value, self.decimals):
            raise LiveActuationError(
                f"{self.action_id}: pfWeight {value} needs more than "
                f"{self.decimals} decimals; the knob stores and prints "
                f"%.{self.decimals}f, so this value cannot round trip")

    def parse_read(self, target: TelnetTarget, response: str) -> float:
        for line in self._lines(response):
            match = self.LIST_LINE.match(line)
            if match and int(match.group(1), 16) == target.rnti:
                return float(match.group(2))
        raise TelnetGrammarError(
            f"{self.command}: UE {target.rnti:04x} is not in the connected-UE "
            f"listing {response!r}; a PF weight cannot be assumed")

    def write_line(self, target: TelnetTarget, value: Any) -> str:
        return (f"{self.command} {float(value):.{self.decimals}f} "
                f"{target.rnti:x}")

    def parse_write(self, target: TelnetTarget, value: Any,
                    response: str) -> float:
        for line in self._lines(response):
            match = self._ACK.match(line)
            if match is None:
                continue
            if int(match.group(1), 16) != target.rnti:
                raise TelnetGrammarError(
                    f"{self.command}: acknowledgement names UE "
                    f"{match.group(1)}, the write addressed "
                    f"{target.rnti:04x}")
            acked = float(match.group(2))
            if abs(acked - float(value)) > 1e-9:
                raise TelnetGrammarError(
                    f"{self.command}: acknowledged {acked}, {float(value)} "
                    "was written")
            return acked
        raise TelnetGrammarError(
            f"{self.command}: no 'PF weight set to' acknowledgement in "
            f"{response!r}")


class CellTxAttenuationCodec(TelnetKnobCodec):
    """``ci rfatt`` -- the cell's downlink transmit attenuation.

    Grammar (``rfatt_cmd``)::

        ci rfatt        -> "current TX attenuation <att> dB"
        ci rfatt <att>  -> "TX attenuation set to <att> dB"

    Range: the catalog states none for ``txAttenuationDb``, so the bound
    enforced here is the handler's own ``[0,60] dB`` -- read off the C, not
    invented, and not a substitute for the harm contract that bounds how far a
    trial may move the cell.  Direction is the one the manifest records:
    attenuation below maximum gain, so a larger value means *less* downlink
    power.  Printed ``%.1f``.
    """

    action_id = "dl-rf-attenuation"
    family = "rfatt"
    command = "ci rfatt"
    scope = "NRCellDU"
    axis_regex = re.compile(r"^cell/([^/]+)/txAttenuationDb$")
    decimals = 1
    wire_minimum = 0.0
    wire_maximum = 60.0

    _READ = re.compile(r"^current TX attenuation (-?\d+\.\d) dB$")
    _ACK = re.compile(r"^TX attenuation set to (-?\d+\.\d) dB$")

    def target_for(self, axis: str) -> Optional[TelnetTarget]:
        match = self.axis_regex.match(axis)
        if match is None:
            return None
        return TelnetTarget(axis=axis, scope=self.scope,
                            cell_id=match.group(1))

    def check_parameters(self, parameters: Mapping[str, Any], *,
                         cell_id: str, selector: Mapping[str, Any]) -> None:
        _check_selector_cell(self.action_id, selector, cell_id)
        self.check_value(TelnetTarget(axis="", scope=self.scope,
                                      cell_id=cell_id),
                         parameters["txAttenuationDb"])

    def check_value(self, target: TelnetTarget, value: Any) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise LiveActuationError(
                f"{self.action_id}: txAttenuationDb must be a number, got "
                f"{value!r}")
        if not self.wire_minimum <= value <= self.wire_maximum:
            raise LiveActuationError(
                f"{self.action_id}: txAttenuationDb {value} is outside the "
                f"[{self.wire_minimum},{self.wire_maximum}] dB rfatt_cmd "
                "accepts")
        if not _wire_exact(value, self.decimals):
            raise LiveActuationError(
                f"{self.action_id}: txAttenuationDb {value} needs more than "
                f"{self.decimals} decimal; the knob prints %.{self.decimals}f, "
                "so this value cannot round trip")

    def parse_read(self, target: TelnetTarget, response: str) -> float:
        for line in self._lines(response):
            match = self._READ.match(line)
            if match:
                return float(match.group(1))
        raise TelnetGrammarError(
            f"{self.command}: no 'current TX attenuation' line in "
            f"{response!r}")

    def write_line(self, target: TelnetTarget, value: Any) -> str:
        return f"{self.command} {float(value):.{self.decimals}f}"

    def parse_write(self, target: TelnetTarget, value: Any,
                    response: str) -> float:
        for line in self._lines(response):
            match = self._ACK.match(line)
            if match is None:
                continue
            acked = float(match.group(1))
            if abs(acked - float(value)) > 1e-9:
                raise TelnetGrammarError(
                    f"{self.command}: acknowledged {acked} dB, "
                    f"{float(value)} dB was written")
            return acked
        raise TelnetGrammarError(
            f"{self.command}: no 'TX attenuation set to' acknowledgement in "
            f"{response!r}")


class DlMcsBoundsCodec(TelnetKnobCodec):
    """``ci mcs`` -- the cell's downlink MCS floor and ceiling.

    Grammar (``mcs_cmd``)::

        ci mcs              -> "DL MCS cap [<min>..<max>] UL MCS cap [<min>..<max>]"
        ci mcs <max> <min>  -> "DL MCS cap set to [<min>..<max>]"

    One command carries both bounds, so this family's axis carries both as one
    value -- ``{"maxDlMcs": ..., "minDlMcs": ...}``.  Splitting it into two
    axes would mean two writes, and the intermediate state between them is a
    configuration the operator never asked for.  Argument order on the wire is
    *max first*; the floor is applied only when ``0 <= min <= max``, which the
    catalog validator has already guaranteed by the time a write is built.
    """

    action_id = "dl-mcs-bounds"
    family = "mcs"
    command = "ci mcs"
    scope = "NRCellDU"
    axis_regex = re.compile(r"^cell/([^/]+)/dlMcsBounds$")

    _READ = re.compile(
        r"^DL MCS cap \[(\d+)\.\.(\d+)\] UL MCS cap \[(\d+)\.\.(\d+)\]$")
    _ACK = re.compile(r"^DL MCS cap set to \[(\d+)\.\.(\d+)\]$")

    def target_for(self, axis: str) -> Optional[TelnetTarget]:
        match = self.axis_regex.match(axis)
        if match is None:
            return None
        return TelnetTarget(axis=axis, scope=self.scope,
                            cell_id=match.group(1))

    def check_parameters(self, parameters: Mapping[str, Any], *,
                         cell_id: str, selector: Mapping[str, Any]) -> None:
        _check_selector_cell(self.action_id, selector, cell_id)
        self.check_value(
            TelnetTarget(axis="", scope=self.scope, cell_id=cell_id),
            {"maxDlMcs": parameters["maxDlMcs"],
             "minDlMcs": parameters["minDlMcs"]})

    def check_value(self, target: TelnetTarget, value: Any) -> None:
        if not isinstance(value, Mapping) \
                or set(value) != {"maxDlMcs", "minDlMcs"}:
            raise LiveActuationError(
                f"{self.action_id}: the axis value is "
                "{'maxDlMcs': int, 'minDlMcs': int}, got " + repr(value))
        for name in ("maxDlMcs", "minDlMcs"):
            bound = value[name]
            minimum, maximum = self._catalog_bounds(name)
            if isinstance(bound, bool) or not isinstance(bound, int):
                raise LiveActuationError(
                    f"{self.action_id}: {name} must be an int, got {bound!r}")
            if bound < minimum or bound > maximum:
                raise LiveActuationError(
                    f"{self.action_id}: {name} {bound} is outside the catalog "
                    f"range [{minimum},{maximum}]")
        if value["minDlMcs"] > value["maxDlMcs"]:
            raise LiveActuationError(
                f"{self.action_id}: minDlMcs {value['minDlMcs']} exceeds "
                f"maxDlMcs {value['maxDlMcs']}")

    def parse_read(self, target: TelnetTarget,
                   response: str) -> Dict[str, int]:
        for line in self._lines(response):
            match = self._READ.match(line)
            if match:
                return {"minDlMcs": int(match.group(1)),
                        "maxDlMcs": int(match.group(2))}
        raise TelnetGrammarError(
            f"{self.command}: no 'DL MCS cap [min..max]' line in {response!r}")

    def write_line(self, target: TelnetTarget, value: Any) -> str:
        return (f"{self.command} {int(value['maxDlMcs'])} "
                f"{int(value['minDlMcs'])}")

    def parse_write(self, target: TelnetTarget, value: Any,
                    response: str) -> Dict[str, int]:
        for line in self._lines(response):
            match = self._ACK.match(line)
            if match is None:
                continue
            acked = {"minDlMcs": int(match.group(1)),
                     "maxDlMcs": int(match.group(2))}
            expected = {"minDlMcs": int(value["minDlMcs"]),
                        "maxDlMcs": int(value["maxDlMcs"])}
            if acked != expected:
                raise TelnetGrammarError(
                    f"{self.command}: acknowledged {acked}, {expected} was "
                    "written")
            return acked
        raise TelnetGrammarError(
            f"{self.command}: no 'DL MCS cap set to' acknowledgement in "
            f"{response!r}")


def _check_selector_cell(action_id: str, selector: Mapping[str, Any],
                         cell_id: str) -> None:
    """A cell-wide knob may only be driven on the cell this endpoint serves."""
    named = selector.get("cellId")
    if named is None:
        raise LiveActuationError(
            f"{action_id}: no cellId in the assignment target selector; a "
            "cell-wide knob is not driven at an unnamed cell")
    if str(named) != str(cell_id):
        raise LiveActuationError(
            f"{action_id}: the assignment targets cell {named!r} but this "
            f"telnet endpoint serves cell {cell_id!r}; a cell-wide write would "
            "land on the wrong cell")


#: Codec class per telnet-actuated action id.
_CODEC_TYPES: Mapping[str, type] = MappingProxyType({
    UeDlPrbCapCodec.action_id: UeDlPrbCapCodec,
    SchedulerPriorityCodec.action_id: SchedulerPriorityCodec,
    CellTxAttenuationCodec.action_id: CellTxAttenuationCodec,
    DlMcsBoundsCodec.action_id: DlMcsBoundsCodec,
})

#: The action ids this adapter actuates, and the ``ci`` command each uses.
TELNET_ACTUATED_ACTIONS: Mapping[str, str] = MappingProxyType({
    action_id: codec.command for action_id, codec in _CODEC_TYPES.items()
})


def telnet_codecs(deployment: DeploymentBinding) -> Mapping[str, TelnetKnobCodec]:
    """Build one codec per telnet-actuated action from the frozen catalog."""
    catalog = {item.action_id: item for item in action_catalog(deployment)}
    codecs: Dict[str, TelnetKnobCodec] = {}
    for action_id, codec_type in _CODEC_TYPES.items():
        contract = catalog.get(action_id)
        if contract is None:
            raise LiveActuationError(
                f"{action_id} is no longer in the action catalog; the live "
                "adapter cannot bind a grammar to a contract that is gone")
        codecs[action_id] = codec_type(contract)
    return MappingProxyType(codecs)


# --------------------------------------------------------------------------- #
# the transport
# --------------------------------------------------------------------------- #

def command_detail(codec: "TelnetKnobCodec", target: "TelnetTarget",
                   value: Any) -> str:
    """A short, credential-free description of one attempted write."""
    return f"{codec.action_id} {target.axis}={value!r}"


@dataclass(frozen=True)
class TelnetExchange:
    """One command sent under one permit, and what came back.

    Kept for every send, including the refused and the unparsable ones: a run
    that ends in an unknown write is exactly the run whose command log has to
    survive.
    """

    axis: str
    operation: str
    command: str
    response: str
    outcome: str
    at: str


class TelnetActuationTransport:
    """Drive one gNB's ``ci`` knobs behind the executors' store interface.

    Constructed with an injected ``send`` -- ``Callable[[str], str]`` taking one
    command line and returning that command's cleaned response.  It opens
    nothing itself; ``tools/xapp_live/transport.py`` is the composition root
    that owns a socket.

    Two rules make it a *permitted* writer rather than a writer:

    #. it must be bound to the specialist executor that will drive it
       (:meth:`bind_executor`, which :func:`build_specialist_executor` calls),
       because the permit check it runs is that executor's own; and
    #. every ``read`` and every ``apply`` must happen inside a
       :meth:`permit_scope`.  Outside one there is no permit to check, so there
       is no send.
    """

    #: Direct gNB control.  Named so the gateway's adapter registry refuses it
    #: (``assurance/gateway/registry.py``): an effect produced here is a lab
    #: measurement and never an objective effect.
    actuator_path = ActuatorPath.LAB_SETUP_PREPARATION

    def __init__(
        self,
        *,
        send: Callable[[str], str],
        deployment: DeploymentBinding,
        cell_id: str,
        endpoint: str = "",
        clock: Optional[Callable[[], str]] = None,
    ) -> None:
        if not callable(send):
            raise LiveActuationError("send must be a callable(str) -> str")
        if not isinstance(cell_id, str) or not cell_id.strip():
            raise LiveActuationError(
                "the transport must know which cell this endpoint serves")
        self._send_line = send
        self._codecs = telnet_codecs(deployment)
        self._cell_id = cell_id
        self._endpoint = endpoint
        self._clock = clock

        self._executor: Optional[SpecialistXAppExecutor] = None
        self._permit: Optional[KernelToken] = None
        self._permit_kind: Optional[TokenKind] = None
        self._action_id: Optional[str] = None
        self._armed_at: str = ""

        self._exchanges: List[TelnetExchange] = []
        self._observed: Dict[str, Any] = {}
        self._applied: Dict[str, Any] = {}
        self._last: Dict[str, Any] = {}
        self._unknown: List[str] = []
        self._latched = False
        self._latch_detail = ""

    # -- binding -----------------------------------------------------------

    def bind_executor(self, executor: SpecialistXAppExecutor) -> None:
        """Bind the executor whose permit check gates this transport."""
        if not isinstance(executor, SpecialistXAppExecutor):
            raise LiveActuationError(
                "a live transport is gated by a SpecialistXAppExecutor's own "
                "permit check; nothing else may bind it")
        if self._executor is not None and self._executor is not executor:
            raise LiveActuationError(
                "this transport is already bound to "
                f"{self._executor.manifest.xapp_id}; rebinding would move the "
                "permit check to a different xApp's boundary")
        self._executor = executor

    # -- inspection --------------------------------------------------------

    @property
    def cell_id(self) -> str:
        return self._cell_id

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def exchanges(self) -> Tuple[TelnetExchange, ...]:
        """Every command this transport sent, in order."""
        return tuple(self._exchanges)

    @property
    def observed_baseline(self) -> Mapping[str, Any]:
        """The first value read per axis since the last commit scope armed.

        The same value the executor recorded for its rollback, kept here so a
        report can state the baseline it will be returned to without asking
        the executor for its private state.
        """
        return dict(self._observed)

    @property
    def applied_axes(self) -> Tuple[str, ...]:
        """Axes whose write was acknowledged by the gNB."""
        return tuple(sorted(self._applied))

    @property
    def unknown_writes(self) -> Tuple[str, ...]:
        """Axes whose write may or may not have landed."""
        return tuple(self._unknown)

    @property
    def latched(self) -> bool:
        """True once an outcome was unknown; further applies are refused."""
        return self._latched

    @property
    def latch_detail(self) -> str:
        return self._latch_detail

    def as_dict(self) -> Dict[str, Any]:
        """The last value observed per axis (the store interface's mirror)."""
        return dict(self._last)

    # -- the permit scope --------------------------------------------------

    def authorise(self, assignment: XAppExecutionAssignment, *,
                  permit: KernelToken, kind: TokenKind, now: str) -> None:
        """Open a scope in which this transport may send, or refuse.

        Order is the invariant: the executor's permit check first (so a bad
        permit costs zero bytes), then the action's admissibility, then the
        catalog's parameter validation and the wire's own limits, and only
        then -- for a UE-scoped action -- the one read that proves the RNTI is
        on this cell.
        """
        if self._executor is None:
            raise PermitRequiredError(
                "equipment write blocked: this transport is not bound to a "
                "specialist executor, so the permit check that authorises a "
                "send cannot run")
        if not isinstance(kind, TokenKind):
            raise LiveActuationError("kind must be a TokenKind member")
        # The owner's fail-closed check, called rather than restated: missing,
        # wrong-kind, expired, or bound to another assignment all raise here.
        self._executor._require_permit(  # noqa: SLF001 - reuse, not a bypass
            assignment, permit, now, kind)

        manifest = self._executor.manifest
        if assignment.xapp_id != manifest.xapp_id:
            raise LiveActuationError(
                f"the assignment is addressed to {assignment.xapp_id}, and "
                f"this transport is bound to {manifest.xapp_id}; the executor "
                "would refuse it, and arming would cost a read first")
        if not manifest.owns_action(assignment.action_id):
            raise LiveActuationError(
                f"{manifest.xapp_id} does not own {assignment.action_id!r}; "
                f"its closed ownership set is "
                f"{sorted(manifest.owned_action_ids)}")

        action_id = assignment.action_id
        reason = NOT_ACTUATED_BY_TELNET.get(action_id)
        if reason is not None:
            raise NotActuatedByTelnetError(f"{action_id}: {reason}")
        codec = self._codecs.get(action_id)
        if codec is None:
            raise NotActuatedByTelnetError(
                f"{action_id}: no telnet knob is bound to this action; the "
                f"adapter drives {sorted(TELNET_ACTUATED_ACTIONS)}")
        try:
            validate_action_parameters(codec.contract, assignment.parameters)
        except ActionParameterError as exc:
            raise LiveActuationError(
                f"{action_id}: {exc}; refused before any command was sent"
            ) from exc
        codec.check_parameters(assignment.parameters, cell_id=self._cell_id,
                               selector=assignment.target_selector)

        self._permit = permit
        self._permit_kind = kind
        self._action_id = action_id
        self._armed_at = now
        if kind is TokenKind.COMMIT:
            self._observed.clear()
            self._applied.clear()
        try:
            if codec.scope == "UE":
                self._require_connected(int(assignment.parameters["rnti"]))
        except Exception:
            self.release()
            raise

    def release(self) -> None:
        """Close the scope.  Nothing may be sent again without a new permit."""
        self._permit = None
        self._permit_kind = None
        self._action_id = None
        self._armed_at = ""

    @contextmanager
    def permit_scope(self, assignment: XAppExecutionAssignment, *,
                     permit: KernelToken, kind: TokenKind,
                     now: str) -> Iterator["TelnetActuationTransport"]:
        """:meth:`authorise` for the duration of a block, then release."""
        self.authorise(assignment, permit=permit, kind=kind, now=now)
        try:
            yield self
        finally:
            self.release()

    # -- the store interface the executors drive ---------------------------

    def read(self, axis: str) -> Any:
        """The knob's current value, parsed from its arg-less command."""
        codec, target = self._resolve(axis)
        self._require_scope(axis, writing=False)
        response = self._exchange(axis, "READ", codec.read_line(target))
        value = codec.parse_read(target, response)
        self._observed.setdefault(axis, value)
        self._last[axis] = value
        return value

    def apply(self, axis: str, value: Any) -> None:
        """Write *value* to the knob and refuse anything but a clean ACK."""
        codec, target = self._resolve(axis)
        self._require_scope(axis, writing=True)
        codec.check_value(target, value)
        response = self._exchange(axis, "APPLY", codec.write_line(target, value),
                                  writing=True)
        try:
            acked = codec.parse_write(target, value, response)
        except TelnetGrammarError as exc:
            # The command reached the gNB and the answer is not the one this
            # write asked for.  Nothing is counted as applied, and the
            # transport latches: the live value is no longer something this
            # adapter can state, and only the rollback that restores the
            # recorded baseline may write again.
            self._latch(axis, f"{command_detail(codec, target, value)}: {exc}")
            raise
        self._applied[axis] = acked
        self._last[axis] = acked

    # -- fail-closed internals ---------------------------------------------

    def _resolve(self, axis: str) -> Tuple[TelnetKnobCodec, TelnetTarget]:
        for codec in self._codecs.values():
            target = codec.target_for(axis)
            if target is None:
                continue
            if target.scope == "NRCellDU" \
                    and str(target.cell_id) != str(self._cell_id):
                raise LiveActuationError(
                    f"axis {axis!r} names cell {target.cell_id!r}; this telnet "
                    f"endpoint serves cell {self._cell_id!r}")
            return codec, target
        raise LiveActuationError(
            f"no telnet knob drives axis {axis!r}; the adapter refuses an axis "
            "it cannot map rather than silently dropping the write")

    def _require_scope(self, axis: str, *, writing: bool) -> None:
        if self._permit is None or self._permit_kind is None:
            raise PermitRequiredError(
                f"equipment access blocked: {axis!r} was touched outside a "
                "permit scope, so no Write Gateway permit authorises it")
        codec, _ = self._resolve(axis)
        if codec.action_id != self._action_id:
            raise PermitRequiredError(
                f"equipment access blocked: this scope was opened for "
                f"{self._action_id!r}, and {axis!r} belongs to "
                f"{codec.action_id!r}")
        if self._clock is not None and self._permit.is_expired(self._clock()):
            raise PermitRequiredError(
                "equipment access blocked: the permit lease expired at "
                f"{self._permit.lease_expiry} while the scope was open")
        if writing and self._latched \
                and self._permit_kind is not TokenKind.REVERSE_ROLLBACK:
            raise LiveActuationError(
                "the transport is latched after an unknown write "
                f"({self._latch_detail}); only a REVERSE_ROLLBACK permit may "
                "write again")

    def _require_connected(self, rnti: int) -> None:
        """Refuse a UE-scoped scope unless the RNTI is on this cell now."""
        listing = SchedulerPriorityCodec.command
        response = self._exchange(f"ue/{rnti:#06x}", "PROBE", listing)
        found = {int(match.group(1), 16)
                 for match in (SchedulerPriorityCodec.LIST_LINE.match(line)
                               for line in response.splitlines()
                               if line.strip())
                 if match is not None}
        if rnti not in found:
            raise LiveActuationError(
                f"UE {rnti:#06x} is not connected to cell {self._cell_id}: "
                f"{listing} lists {sorted(format(r, '#06x') for r in found)}. "
                "RNTI is cell-local, so a write addressed to an absent RNTI "
                "would either be refused or land on whoever holds it next")

    def _exchange(self, axis: str, operation: str, command: str, *,
                  writing: bool = False) -> str:
        at = self._clock() if self._clock is not None else self._armed_at
        try:
            response = self._send_line(command)
        except Exception as exc:
            self._record(axis, operation, command, repr(exc),
                         "TRANSPORT_ERROR", at)
            if writing:
                self._latch(axis, f"{command!r} raised {exc!r}")
                raise TelnetTransportError(
                    f"{command!r} failed with {exc!r}; whether the write "
                    "landed is UNKNOWN. Nothing further may be applied until "
                    "a rollback resolves it") from exc
            raise TelnetTransportError(
                f"{command!r} failed with {exc!r}; no value was read") from exc
        if not isinstance(response, str):
            self._record(axis, operation, command, repr(response),
                         "UNPARSED", at)
            if writing:
                self._latch(axis, f"{command!r} returned a non-string response")
                raise TelnetTransportError(
                    f"{command!r} returned {type(response).__name__}, not the "
                    "shell's text; the outcome is UNKNOWN")
            raise TelnetTransportError(
                f"{command!r} returned {type(response).__name__}, not text")
        refusal = _refusal_in(response)
        if refusal is not None:
            # ERROR_MSG_RET returns before the MAC is touched: a refusal is a
            # clean no-op, so it does not latch anything.
            self._record(axis, operation, command, response, "REFUSED", at)
            raise TelnetRefusedError(f"{command!r} was refused: {refusal}")
        self._record(axis, operation, command, response, "PARSED", at)
        return response

    def _record(self, axis: str, operation: str, command: str, response: str,
                outcome: str, at: str) -> None:
        self._exchanges.append(TelnetExchange(
            axis=axis, operation=operation, command=command,
            response=response, outcome=outcome, at=at))

    def _latch(self, axis: str, detail: str) -> None:
        if axis not in self._unknown:
            self._unknown.append(axis)
        self._latched = True
        self._latch_detail = detail


# --------------------------------------------------------------------------- #
# the fourth specialist, and the backend switch
# --------------------------------------------------------------------------- #

class LinkAdaptationXApp(SpecialistXAppExecutor):
    """Executes ``dl-mcs-bounds`` and nothing else.

    The fourth knob-backed specialist, written against the owner's
    :class:`~assurance.xapps.executors.SpecialistXAppExecutor` skeleton rather
    than beside it: validation, the permit boundary, staleness, readback and
    rollback are all inherited, and this class contributes only what is
    specific to a cell-wide MCS bound.

    Both bounds move in one write.  ``ci mcs`` takes them together, and a cell
    that spent a moment at ``[min_old..max_new]`` would be in a configuration
    nobody planned, so the axis carries the pair.

    The manifest for this xApp records why it is outside the initial
    coordinated live set (a cell-wide MCS bound can defeat any floor
    objective).  Nothing here relaxes that; a caller reaching for this
    executor has to say so.
    """

    OPERATIONS = {"dl-mcs-bounds": "SET_DL_MCS_BOUNDS"}

    def __init__(self, *, manifest: XAppCapabilityManifest,
                 store: Any) -> None:
        if manifest.kind is not XAppKind.LINK_ADAPTATION:
            raise ExecutorError(
                "LinkAdaptationXApp needs a LINK_ADAPTATION manifest")
        super().__init__(manifest=manifest, store=store)

    def _precheck(self, assignment: XAppExecutionAssignment,
                  snapshot: CommonKpiSnapshot, now: str) -> Optional[str]:
        if not str(assignment.target_selector.get("cellId", "")):
            return "no cellId in the assignment target selector"
        return None

    def _axes(self, assignment: XAppExecutionAssignment) \
            -> Tuple[Tuple[str, Any], ...]:
        cell = assignment.target_selector["cellId"]
        return ((f"cell/{cell}/dlMcsBounds",
                 {"maxDlMcs": assignment.parameters["maxDlMcs"],
                  "minDlMcs": assignment.parameters["minDlMcs"]}),)

    def _enrich_readback(self, assignment: XAppExecutionAssignment,
                         snapshot: CommonKpiSnapshot,
                         readback: Dict[str, Any]) -> Dict[str, Any]:
        cell = str(assignment.target_selector["cellId"])
        baselines = self._baselines[assignment.assignment_id]
        enriched = dict(readback)
        enriched["rollbackBaseline"] = {b.axis: b.value for b in baselines}
        enriched["affectedActiveUeIds"] = list(snapshot.active_ue_ids(cell))
        enriched["boundsDirectionNote"] = (
            "a lower maxDlMcs lowers the cell's DL spectral efficiency for "
            "every UE on it; the bound is cell-wide, not per-UE")
        return enriched


#: Specialist executor class per xApp kind.  ``SLICE_RESOURCE`` is absent
#: because no executor exists for it in this repository -- see
#: :data:`NOT_ACTUATED_BY_TELNET`.
_EXECUTOR_TYPES: Mapping[XAppKind, type] = MappingProxyType({
    XAppKind.TRAFFIC_STEERING: TrafficSteeringXApp,
    XAppKind.UE_SCHEDULER: UeSchedulerXApp,
    XAppKind.CELL_POWER: CellPowerXApp,
    XAppKind.LINK_ADAPTATION: LinkAdaptationXApp,
})


def build_specialist_executor(
    *, manifest: XAppCapabilityManifest, backend: Any,
) -> SpecialistXAppExecutor:
    """Build the specialist for *manifest* over *backend*.

    The one switch between the two actuation backends.  *backend* is either a
    :class:`~assurance.xapps.executors.HardwareFreeConfigStore` (hermetic
    tests) or a :class:`TelnetActuationTransport` (a live lab run); the
    executor class, its validation and its rollback are the same object either
    way, which is the point -- a live run must not be a second implementation
    of the path the tests cover.

    A live transport is bound to the executor it will serve, because the
    permit check the transport runs before every send is that executor's own.
    """
    if not isinstance(manifest, XAppCapabilityManifest):
        raise LiveActuationError("manifest must be an XAppCapabilityManifest")
    executor_type = _EXECUTOR_TYPES.get(manifest.kind)
    if executor_type is None:
        raise LiveActuationError(
            f"{manifest.kind.value}: no specialist executor exists in this "
            f"repository for {manifest.xapp_id}; its actions are "
            f"{sorted(manifest.owned_action_ids)}")
    if isinstance(backend, TelnetActuationTransport):
        unactuated = sorted(
            action_id for action_id in manifest.owned_action_ids
            if action_id in NOT_ACTUATED_BY_TELNET)
        if unactuated:
            raise NotActuatedByTelnetError(
                f"{manifest.xapp_id} owns {unactuated}: "
                + "; ".join(NOT_ACTUATED_BY_TELNET[a] for a in unactuated))
        executor = executor_type(manifest=manifest, store=backend)
        backend.bind_executor(executor)
        return executor
    return executor_type(manifest=manifest, store=backend)
