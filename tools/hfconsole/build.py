"""Build a hardware-free Kernel submission session and attach it to the Cockpit.

The public surface is small:

* :func:`build_hardware_free_runtime` — a :class:`LiveObjectiveRuntime` wired
  over :class:`MockActuationAdapter` for one submittable objective family.
* :func:`build_hardware_free_session` — that runtime wrapped in the Cockpit's
  :class:`KernelSubmissionSession` (``mode=MODE_MOCK``), plus the objective
  grammar and a default operator sentence.
* :func:`attach_hardware_free_session` — build one and hand it to an
  ``OperatorConsole`` through ``attach_kernel_session``.

Everything is deterministic and touches no socket, process, model or radio.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional, Sequence

from assurance.contracts.live_binding import load_assurance_live_binding
from assurance.gateway.mock_adapter import MockActuationAdapter
from assurance.live.objective_runtime import build_live_objective_runtime
from assurance.objectives.action102_support import (
    CAP_ACTION_ID,
    CAP_ADAPTER_KEY,
    CAP_AXIS,
    CAP_POLICY_TYPE_ID,
    SupplementaryCapError,
    SupplementaryCapRequest,
    cap_axis_baseline,
    live_cap_admissible,
    supplementary_axis_declarations,
    with_supplementary_cap,
)
from assurance.live.pin_to_cell_driver import (
    KpmUeAttributionReader,
    LiveCellTopology,
    LiveUeObservation,
)
from assurance.advisors.grammar import scope_selector
from assurance.objectives import FAMILY_MODULES, RegistryError, record_for
from tools.g5ota.objective_live import family_grammar, family_utterance, live_scope

from gui.operator.sources.kernel_live import MODE_MOCK, KernelSubmissionSession

#: The committed live binding.  It names the two real cells (12345678 and
#: 87654321) and the KPM expected epochs; the hardware-free run uses the same
#: binding so the contract instances are the live ones, not a second geometry.
DEFAULT_BINDING = "deployment/assurance-live-binding.1.0.0.json"

#: The two cells the binding names.  Home is where the UE starts; target is the
#: cell an intent may ask for.  A caller may override both.
DEFAULT_HOME_NCI = 12345678
DEFAULT_TARGET_NCI = 87654321
DEFAULT_AMF_UE_NGAP_ID = 131

#: gNB nb_id -> NCI for the two-cell topology the binding describes.
_NB_ID_TO_NCI = {0xE00: DEFAULT_HOME_NCI, 0xB00: DEFAULT_TARGET_NCI}
_PLMN = {"mcc": "208", "mnc": "95"}

#: Counter loaders default to empty: with no injected samples a run reaches a
#: terminal on measurement sufficiency rather than a KPI pass, which is the
#: honest default for a plumbing check.  A caller that wants a decided predicate
#: passes its own loaders (hardware-free synthetic samples, recorded as such).
_EMPTY_COUNTER_LOADERS: Mapping[str, Callable[[], Sequence[Any]]] = {
    "counter/rru-prb-dl": tuple,
    "counter/kpm-f3-drb-ue-thp-dl": tuple,
}

#: The heavy, non-target UE a supplementary cap controls.  A *different*
#: identity from the objective UE by construction: capping the UE the objective
#: protects is the one composition the contract refuses outright.
DEFAULT_CONTROLLED_AMF_UE_NGAP_ID = 132
#: The epoch-frozen finite cap catalog.  Values inside the ``[5,24]`` APPLY
#: range on a 24-PRB radio; the epoch multiplies them by the cell candidates and
#: freezes one catalog hash over the product.
DEFAULT_CAP_CANDIDATES = (12,)
#: Deployment calibration.  Typed and frozen into the epoch by
#: ``with_supplementary_cap``; hardware-free values, recorded as such.
DEFAULT_OBJECTIVE_FLOOR_KBPS = 500.0
DEFAULT_CONTROLLED_RESERVE_KBPS = 1500.0
_CAP_CALIBRATION_REF = "calibration/hardware-free/ue-dl-prb-cap"


class HardwareFreeConsoleError(RuntimeError):
    """A hardware-free session could not be composed."""


def resolve_controlled_amf_ue_ngap_id(
    *, explicit: Optional[int] = None, utterance: Optional[str] = None,
    objective_ue: int, default: Optional[int] = None,
) -> Optional[int]:
    """Which UE a supplementary control acts on, from the two places to say it.

    The flag and the sentence's ``controlledUeId=`` scope, read with the
    deterministic grammar's own selector so the console and the Intent Agent
    cannot disagree about what a sentence said.  Both may speak and must then
    agree.  ``None`` composes no supplementary control, which is what every
    sentence written before this work package means.
    """
    stated: Dict[str, int] = {}
    if explicit is not None:
        stated["--controlled-amf-ue-ngap-id"] = int(explicit)
    if utterance:
        typed = scope_selector(utterance).get("controlledUeId")
        if typed is not None:
            try:
                stated["the sentence's controlledUeId="] = int(str(typed))
            except (TypeError, ValueError):
                raise HardwareFreeConsoleError(
                    f"the sentence names controlledUeId={typed!r}, which is not "
                    "an amfUeNgapId") from None
    if not stated:
        return default
    chosen = set(stated.values())
    if len(chosen) > 1:
        raise HardwareFreeConsoleError(
            "the run names more than one controlled UE: "
            + "; ".join(f"{source} {value}"
                        for source, value in sorted(stated.items()))
            + ". One supplementary control acts on one UE.")
    controlled = chosen.pop()
    if controlled == int(objective_ue):
        raise HardwareFreeConsoleError(
            f"the controlled UE and the objective UE are both amfUeNgapId "
            f"{controlled}. A supplementary control acts on a different, "
            "heavy, non-target UE.")
    return controlled


class _VirtualClock:
    """A deterministic clock; nothing in a hardware-free run waits on wall time."""

    def __init__(self, start: str = "2026-08-25T00:00:00.000000Z") -> None:
        self._base = _parse(start)
        self.ms = 0

    def now(self) -> str:
        moment = self._base + timedelta(milliseconds=self.ms)
        return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")

    def monotonic_ms(self) -> int:
        return self.ms

    def sleep_ms(self, ms: int) -> None:
        self.ms += max(0, int(ms))


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(timezone.utc)


def _identity(*, amf_ue_ngap_id: int, home_nci: int) -> LiveUeObservation:
    """The UE identity shape the deployment publishes, with a home serving cell."""
    return LiveUeObservation(
        amf_ue_ngap_id=amf_ue_ngap_id,
        gu_ami={
            "plmnId": dict(_PLMN),
            "amfRegionId": "01", "amfSetId": "040", "amfPointer": "04",
        },
        serving_nci=home_nci,
        e2_node="ngran=02;plmn=208-095-2;nb=0000003584/00;cudu=none:00000000000000000000",
        connection_epoch=173,
        observed_at="2026-08-25T00:00:00.000000Z",
        trace_hash="0" * 64,
    )


def submittable_families() -> Sequence[str]:
    """The objective families the deployment will accept a submission for.

    The registry is the single source; this list is derived from it so a family
    that becomes (un)submittable there changes here with no second edit.
    """
    families = []
    for family in FAMILY_MODULES:
        try:
            if record_for(family).deployment_capability.submittable:
                families.append(family)
        except RegistryError:
            continue
    return tuple(families)


def build_hardware_free_runtime(
    family: str,
    *,
    home_nci: int = DEFAULT_HOME_NCI,
    target_nci: int = DEFAULT_TARGET_NCI,
    amf_ue_ngap_id: int = DEFAULT_AMF_UE_NGAP_ID,
    binding_path: str = DEFAULT_BINDING,
    clock: Optional[_VirtualClock] = None,
    counter_sample_loaders: Optional[Mapping[str, Callable[[], Sequence[Any]]]] = None,
    read_new_lines: Callable[[], Sequence[str]] = tuple,
    case_id: Optional[str] = None,
    cap: Optional["HardwareFreeCap"] = None,
) -> Any:
    """Wire one objective's live contract instances over the mock adapter.

    Returns the ``LiveObjectiveRuntime`` from
    :func:`assurance.live.objective_runtime.build_live_objective_runtime`; the
    caller reads ``.path`` (the :class:`VerticalPath`), ``.cell_id``,
    ``.bundle`` and ``.geometry``.

    ``cap`` composes a second participant beside the steering adapter: the
    steering mock then owns only the ``servingCell`` axis and the cap mock owns
    ``dlPrbCap``, so the gateway merges two partial observations into the
    surface digest.  That is the multi-participant behaviour the live
    composition has, exercised with no transport at all -- a mock, and it says
    so: this path is ``HARDWARE_FREE_ROUND_TRIP`` and never OTA evidence.
    """
    if family not in FAMILY_MODULES:
        raise HardwareFreeConsoleError(
            f"unknown objective family {family!r}; known: "
            + ", ".join(sorted(FAMILY_MODULES))
        )
    if cap is not None and not live_cap_admissible(family):
        raise HardwareFreeConsoleError(
            f"{family} may not carry a {CAP_ACTION_ID}; the composition policy "
            "admits one only in QoSTarget and UELevelTarget")
    clk = clock or _VirtualClock()
    binding = load_assurance_live_binding(binding_path)
    adapter = MockActuationAdapter(config={"servingCell": str(home_nci)})
    # A hardware-free run does not host the live watchdogs; the mock adapter
    # says so rather than pretending to arm hardware timers.
    adapter.hosts_watchdogs = False
    reader = KpmUeAttributionReader(
        read_new_lines=read_new_lines,
        topology=LiveCellTopology(
            plmn=dict(_PLMN),
            nb_id_to_nci={0xE00: home_nci, 0xB00: target_nci},
            expected_epochs=dict(binding.kpm_expected_epochs),
        ),
    )
    return build_live_objective_runtime(
        family_module=FAMILY_MODULES[family](),
        scope=live_scope(
            amf_ue_ngap_id=amf_ue_ngap_id, home_nci=home_nci, target_nci=target_nci),
        binding=binding,
        # Not used on the hardware-free branch (adapter_override is set), but the
        # signature requires them; the no-op builder keeps the contract explicit.
        policy_port=None,
        policy_builder_factory=lambda kernel, bundle: (lambda command: {}),
        reader=reader,
        identity=_identity(amf_ue_ngap_id=amf_ue_ngap_id, home_nci=home_nci),
        now=clk.now,
        monotonic_ms=clk.monotonic_ms,
        sleep_ms=clk.sleep_ms,
        case_id=case_id or f"case/hardware-free:{family}",
        adapter_name="mock",
        adapter_override=adapter,
        counter_sample_loaders=dict(
            counter_sample_loaders
            if counter_sample_loaders is not None
            else (_EMPTY_COUNTER_LOADERS if cap is None
                  else {**_EMPTY_COUNTER_LOADERS, **_cap_counter_loaders(family)})
        ),
        bundle_transform=(
            None if cap is None
            else (lambda bundle: with_supplementary_cap(bundle, cap.request))
        ),
        supplementary_adapters=({} if cap is None else {CAP_ADAPTER_KEY: cap.adapter}),
        axis_adapters=({} if cap is None else {CAP_AXIS: CAP_ADAPTER_KEY}),
        supplementary_axes=(
            () if cap is None else supplementary_axis_declarations((CAP_ACTION_ID,))),
    )


def _cap_counter_loaders(family: str) -> Mapping[str, Callable[[], Sequence[Any]]]:
    """Empty loaders for the three counters a cap composition adds.

    Empty on purpose, exactly like the family's own defaults: with no injected
    samples the run reaches a terminal on measurement sufficiency rather than a
    KPI pass, which is the honest hardware-free answer.  What this path proves
    is the *plumbing* -- one policy per semantic scope, ordered apply,
    corroborated readback, reverse-order rollback -- and it proves it without
    pretending to have measured a radio.
    """
    return {
        f"counter/{family}/{CAP_ACTION_ID}": tuple,
        f"counter/{family}/ue-throughput-objective": tuple,
        f"counter/{family}/ue-throughput-controlled": tuple,
    }


@dataclass(frozen=True)
class HardwareFreeCap:
    """The SUPPLEMENTARY cap participant of a hardware-free composition.

    Two mock adapters, not one: the whole point of the hardware-free proof is
    that the *gateway* behaves the way it will behave live -- prepare-all
    before any write, PRIMARY before SUPPLEMENTARY, a merged surface digest
    from two partial observations, and reverse-order rollback -- and that
    behaviour only exists when there really are two participants.  The A1
    transport, the producer and the released xApp are exercised against the
    real ``R1Adapter`` in ``tests/assurance/test_action102_hardware_free.py``.
    """

    request: SupplementaryCapRequest
    adapter: MockActuationAdapter
    controlled_ue: Mapping[str, Any]
    max_dl_prbs: int

    def describe(self) -> Mapping[str, Any]:
        """A read-only snapshot for the Cockpit's frozen view model.

        Plain strings and integers only.  The console is handed *this callable*
        rather than the adapter, so nothing the GUI renders can reach an
        actuator -- and nothing it renders is a Kernel decision.
        """
        applied = str(self.adapter.snapshot().get(CAP_AXIS, ""))
        writes = [command for command in self.adapter.writes
                  if command.get("axis") == CAP_AXIS]
        return {
            "bindingState": "MOCK_NO_POLICY",
            "policyId": "",
            "readbackState": f"OBSERVED={applied}",
            "rollbackState": (
                "RESTORED" if applied == cap_axis_baseline() else "APPLIED"),
            "e2WriteCount": len(writes),
            "detail": "hardware-free mock adapter; no A1 policy and no E2 write",
        }


@dataclass(frozen=True)
class HardwareFreeSession:
    """A composed hardware-free session and the pieces the caller may need.

    ``session`` is the object the Cockpit drives (``draft``/``confirm``/
    ``start``).  ``runtime`` is kept so a headless caller can read the event
    stream and terminal after ``start``.  ``utterance`` is a sentence an
    Operator could have typed for this objective; a caller may substitute their
    own before drafting.
    """

    session: KernelSubmissionSession
    runtime: Any
    grammar: Mapping[str, Any]
    utterance: str
    family: str
    #: The SUPPLEMENTARY cap participant, when one was composed.
    cap: Optional[HardwareFreeCap] = None


def build_hardware_free_cap(
    *,
    home_nci: int = DEFAULT_HOME_NCI,
    objective_amf_ue_ngap_id: int = DEFAULT_AMF_UE_NGAP_ID,
    controlled_amf_ue_ngap_id: int = DEFAULT_CONTROLLED_AMF_UE_NGAP_ID,
    candidate_caps: Sequence[int] = DEFAULT_CAP_CANDIDATES,
    objective_floor_kbps: float = DEFAULT_OBJECTIVE_FLOOR_KBPS,
    controlled_reserve_kbps: float = DEFAULT_CONTROLLED_RESERVE_KBPS,
) -> HardwareFreeCap:
    """Compose the SUPPLEMENTARY cap participant, hardware-free."""
    if int(controlled_amf_ue_ngap_id) == int(objective_amf_ue_ngap_id):
        raise SupplementaryCapError(
            "the controlled UE is the objective UE; a cap must control a "
            "different, heavy, non-target UE")
    controlled = {
        "cellId": str(home_nci),
        "ueId": str(controlled_amf_ue_ngap_id),
    }
    adapter = MockActuationAdapter(
        config={CAP_AXIS: cap_axis_baseline()}, name=CAP_ADAPTER_KEY)
    # A hardware-free adapter does not host the live watchdogs; it says so
    # rather than pretending to arm hardware timers.
    adapter.hosts_watchdogs = False
    request = SupplementaryCapRequest(
        controlled_ue=controlled,
        candidate_caps=tuple(candidate_caps),
        objective_throughput_floor_kbps=float(objective_floor_kbps),
        controlled_harm_reserve_kbps=float(controlled_reserve_kbps),
        calibration_ref=_CAP_CALIBRATION_REF,
        controlled_ue_scope_id=str(controlled_amf_ue_ngap_id),
    )
    return HardwareFreeCap(
        request=request, adapter=adapter, controlled_ue=controlled,
        max_dl_prbs=int(sorted(candidate_caps)[0]),
    )


def build_hardware_free_session(
    family: str,
    *,
    utterance: Optional[str] = None,
    publish: Optional[Callable[[str, Any], None]] = None,
    home_nci: int = DEFAULT_HOME_NCI,
    target_nci: int = DEFAULT_TARGET_NCI,
    amf_ue_ngap_id: int = DEFAULT_AMF_UE_NGAP_ID,
    binding_path: str = DEFAULT_BINDING,
    counter_sample_loaders: Optional[Mapping[str, Callable[[], Sequence[Any]]]] = None,
    read_new_lines: Callable[[], Sequence[str]] = tuple,
    with_cap: bool = False,
    controlled_amf_ue_ngap_id: Optional[int] = None,
    candidate_caps: Sequence[int] = DEFAULT_CAP_CANDIDATES,
) -> HardwareFreeSession:
    """Compose a hardware-free :class:`KernelSubmissionSession` for one family.

    A SUPPLEMENTARY controlled-UE cap is composed when an operator *names the
    UE it acts on* -- ``controlled_amf_ue_ngap_id``, or the sentence's own
    ``controlledUeId=`` scope, which the frozen grammar already carries on a
    key of its own.  A second ``ueId=`` is deliberately not accepted for it:
    the grammar reads scope tokens into one mapping, so it would silently
    rewrite which UE the objective addresses.

    ``with_cap`` composes the cap on the default controlled UE for callers that
    want one without naming it.  Naming none and passing nothing is
    byte-for-byte the single-participant session this console always built.
    """
    controlled = resolve_controlled_amf_ue_ngap_id(
        explicit=controlled_amf_ue_ngap_id, utterance=utterance,
        objective_ue=amf_ue_ngap_id,
        default=DEFAULT_CONTROLLED_AMF_UE_NGAP_ID if with_cap else None)
    cap = build_hardware_free_cap(
        home_nci=home_nci, objective_amf_ue_ngap_id=amf_ue_ngap_id,
        controlled_amf_ue_ngap_id=controlled,
        candidate_caps=candidate_caps,
    ) if controlled is not None else None
    runtime = build_hardware_free_runtime(
        family,
        home_nci=home_nci, target_nci=target_nci, amf_ue_ngap_id=amf_ue_ngap_id,
        binding_path=binding_path, counter_sample_loaders=counter_sample_loaders,
        read_new_lines=read_new_lines, cap=cap,
    )
    grammar = family_grammar(family, runtime.bundle)
    default_utterance = family_utterance(family, target_nci, str(amf_ue_ngap_id))
    if cap is not None:
        # The sentence says what the session would do.  The clause comes last
        # because the frozen grammar reads the *first* number as the
        # objective's bound, and the controlled UE is on its own scope key so
        # it cannot displace the objective's.
        default_utterance += (
            f"; cap controlledUeId={cap.request.controlled_ue_scope_id} "
            f"at {cap.max_dl_prbs} PRB")
    session = KernelSubmissionSession(
        path=runtime.path,
        cell_id=runtime.cell_id,
        objective_registry=grammar,
        mode=MODE_MOCK,
        publish=publish,
        settle_ms=runtime.geometry.cadence_ms,
        supplementary=(None if cap is None else {
            "actionId": CAP_ACTION_ID,
            "adapter": CAP_ADAPTER_KEY,
            "axis": CAP_AXIS,
            "policyTypeId": CAP_POLICY_TYPE_ID,
            "controlledUeId": cap.request.controlled_ue_scope_id,
            "maxDlPrbs": cap.max_dl_prbs,
            "observe": cap.describe,
        }),
    )
    return HardwareFreeSession(
        session=session,
        runtime=runtime,
        grammar=grammar,
        utterance=utterance or default_utterance,
        family=family,
        cap=cap,
    )


def attach_hardware_free_session(
    console: Any,
    family: str,
    *,
    utterance: Optional[str] = None,
    home_nci: int = DEFAULT_HOME_NCI,
    target_nci: int = DEFAULT_TARGET_NCI,
    amf_ue_ngap_id: int = DEFAULT_AMF_UE_NGAP_ID,
    binding_path: str = DEFAULT_BINDING,
    counter_sample_loaders: Optional[Mapping[str, Callable[[], Sequence[Any]]]] = None,
    with_cap: bool = False,
    controlled_amf_ue_ngap_id: Optional[int] = None,
) -> HardwareFreeSession:
    """Build a hardware-free session and attach it to an ``OperatorConsole``.

    This is the one call ``main.py --hardware-free`` makes after building the
    console.  The console keeps opening Disconnected by default; a hardware-free
    session is attached only when explicitly requested, exactly as a live
    session would be attached by the live composition root.
    """
    composed = build_hardware_free_session(
        family,
        utterance=utterance,
        home_nci=home_nci, target_nci=target_nci, amf_ue_ngap_id=amf_ue_ngap_id,
        binding_path=binding_path, counter_sample_loaders=counter_sample_loaders,
        with_cap=with_cap, controlled_amf_ue_ngap_id=controlled_amf_ue_ngap_id,
    )
    console.attach_kernel_session(composed.session)
    return composed
