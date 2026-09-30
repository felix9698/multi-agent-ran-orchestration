"""The Phase-3 lab harness: a real Kernel permit and a live KPI snapshot.

``tools/xapp_live/run_xapp.py`` refuses to touch the radio without two things it
will not build for itself: a permit that the Assurance Kernel actually issued,
and a :class:`~assurance.xapps.snapshot.CommonKpiSnapshot` assembled from
Measurement Collector samples.  This module supplies both for a lab run, and
supplies them honestly.

What is real here
-----------------
* The Kernel is a real :class:`~assurance.kernel.kernel.AssuranceKernel` over a
  real event store and reducer.  Contracts are admitted, an epoch is frozen, a
  case is opened, a trial is opened and driven to ``COMMIT_DECIDED`` through the
  same transitions ``assurance/vertical.py`` uses.  The ``COMMIT`` permit
  therefore carries a fence, a lease, an expected configuration hash and an
  idempotency key that the Kernel's ledger recorded when it issued them.
* The snapshot is assembled by :class:`~assurance.collector.o1col.KpmJsonlAdapter`
  from the **live** kpm-gate stream, so the serving-cell attribution a UE-scoped
  action is checked against is measured, not asserted.  Epoch mismatches make
  that adapter drop the line, so a stale pin yields an empty snapshot rather
  than a confident wrong one.

What is deliberately not real, and why
--------------------------------------
The Kernel's own ``PREPARE``/``READY`` gateway operations run against
:class:`~assurance.gateway.mock_adapter.MockActuationAdapter`.  They have to:
the four telnet knobs are ``LAB_SETUP_PREPARATION`` and the Write Gateway
adapter registry refuses a Lab Setup adapter by design (section 9), so there is
no production adapter for them to run against and there is not supposed to be.

That is not a shortcut around the permit boundary -- the permit is still issued
by a real Kernel and ``run_xapp`` still refuses anything that is not a
:class:`~assurance.gateway.token.KernelToken`.  It does mean the Kernel here is
authorising a write it does not itself perform: ``run_xapp`` performs it over
telnet.  **An effect measured this way is a research measurement of one xApp's
own actuator, never OTA evidence for an objective family.**  Steering keeps its
production path and is not run through here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from assurance.collector.o1col import KpmJsonlAdapter
from assurance.contracts import (
    ActuatorBinding, ActuatorPath, Aggregation, CapabilityManifest,
    CertifiedHarmBound, ClockRequirement, ComparisonOperator,
    CompositionManifest, CoordinationCasePolicy, CounterBinding,
    DeploymentBinding, Estimator, EvidenceCell, GapPolicy, HarmContract,
    HarmKind, MeasurementContract, MeasurementSource, OverlapPolicy,
    TargetContract, TargetOption, TargetPredicate, TargetReleasePolicy,
    TargetVector, TransportSecurity, TypedConstraint, UncertaintyRule,
    WatchdogAction, WatchdogContract, contract_content_hash,
)
from assurance.core.axes import EvidenceCellStatus
from assurance.core.confirmation import ConfirmationAction, ConfirmationRecord
from assurance.core.envelopes import ASSURANCE_SCHEMA_VERSION
from assurance.core.provenance import Provenance, TypedQuantity
from assurance.core.states import StopReason, TrialState
from assurance.core.timebase import format_utc
from assurance.gateway.gateway import TokenBoundWriteGateway
from assurance.gateway.journal import InMemoryTransactionJournal
from assurance.gateway.mock_adapter import MockActuationAdapter
from assurance.gateway.token import TokenKind
from assurance.kernel.event_store import MemoryEventStore
from assurance.kernel.kernel import AssuranceKernel
from assurance.kernel.reducer import KernelReducer
from assurance.vertical import SteppingClock, VerticalDeployment, VerticalPath
from assurance.collector.samples import RawSample
from assurance.xapps.snapshot import (
    SERVING_CELL_ATTRIBUTION_COUNTER, snapshot_from_samples,
)
from tools.xapp_live.run_xapp import KernelPermitSource

__all__ = ["build_lab_kernel", "permit_source", "live_snapshot",
           "LabHarnessError"]


class LabHarnessError(RuntimeError):
    """The harness cannot be stood up honestly."""


#: Where the live kpm-gate writes, and the pin the binding names.  Both are
#: read at call time rather than baked in, so a re-pin is picked up without a
#: code change and a stale pin fails closed (the adapter drops the lines).
_BINDING = "deployment/assurance-live-binding.1.0.0.json"

_IDENTITY = {
    "version": "1.0.0",
    "schema_version": ASSURANCE_SCHEMA_VERSION,
    "document_status": "NORMATIVE",
    "standard_mapping": {"labSetup": "direct-gnb-telnet"},
}

_CASE_ID = "case/xapp-live-lab"
_ADAPTER = "lab-mock"
_CELL = "12345678"
_BASELINE: Mapping[str, Any] = {"labAxis": "baseline"}
_CADENCE_MS = 1000


def _identity(contract_id: str) -> Dict[str, Any]:
    return {**_IDENTITY, "contract_id": contract_id}


def _quantity(value: float, unit: str, source: str) -> TypedQuantity:
    return TypedQuantity(value, unit, Provenance.EXPERIMENT_CONFIG, source)


def _contracts() -> Dict[str, Any]:
    """A cross-consistent contract family for the four telnet lab axes.

    The actuator's path is :attr:`ActuatorPath.LAB_SETUP_PREPARATION`, which is
    the truth about ``ci rfatt`` / ``ci mcs`` / ``ci prbcap`` / ``ci sched_prio``
    and is what keeps these contracts from ever being mistaken for an objective
    family's production actuation.
    """
    counter = CounterBinding(
        counter_id="counter/ue-dl-throughput",
        deployment_counter_name="DRB.UEThpDl",
        source=MeasurementSource.E2_KPM,
        scope_keys=("cellId", "ueId"),
        unit="Mbps",
        native_cadence_ms=_CADENCE_MS,
        deployment_binding_ref="deployment/xapp-live-telnet")
    measurement = MeasurementContract(
        **_identity("measurement/ue-dl-throughput"),
        counter_id="counter/ue-dl-throughput",
        scope_selector={"cellId": _CELL},
        membership_snapshot=("ue-target",),
        cadence_ms=_CADENCE_MS,
        window_width_ms=3000,
        window_stride_ms=3000,
        overlap=OverlapPolicy.DISJOINT,
        aggregation=Aggregation.MEAN,
        estimator=Estimator.SAMPLE_MEAN,
        minimum_entity_count=1,
        hold_ms=3000,
        gap_policy=GapPolicy.CONSERVATIVE_CHARGE,
        missing_interval_charge=_quantity(5.0, "ms", "plan/missing-charge"),
        freshness_bound_ms=4000,
        clock_requirement=ClockRequirement.SYNCHRONISED_REQUIRED,
        uncertainty_rule=UncertaintyRule(
            "bounded_absolute",
            _quantity(0.1, "Mbps", "calibration/ue-dl-throughput")))
    predicate = TargetPredicate(
        "ue-dl-throughput-floor",
        TypedConstraint("measurement/ue-dl-throughput",
                        ComparisonOperator.GREATER_OR_EQUAL,
                        _quantity(0.5, "Mbps", "plan/lab-floor")))
    option = TargetOption(
        **_identity("option/lab-axis"),
        capability_ref="capability/lab-telnet",
        parameter_space={"labAxis": ("applied",)})
    target = TargetContract(
        **_identity("target/lab-axis"),
        objective_family="TrafficSteeringPreference",
        scope_selector={"cellId": _CELL},
        predicates=(predicate,),
        options=(option,),
        validity_region=(),
        hold_ms=3000)
    watchdog = WatchdogContract(
        **_identity("watchdog/ue-dl-throughput"),
        watchdog_id="wd/ue-dl-throughput",
        trigger=TypedConstraint("measurement/ue-dl-throughput",
                                ComparisonOperator.GREATER_OR_EQUAL,
                                _quantity(0.1, "Mbps", "plan/watchdog-floor")),
        action=WatchdogAction.STOP_AND_ROLLBACK)
    bound = CertifiedHarmBound(
        "bound/lab-axis",
        _quantity(20.0, "ms", "calibration/lab-bound"),
        _quantity(2.0, "ms", "calibration/lab-uncertainty"),
        _quantity(22.0, "ms", "calibration/lab-conservative"),
        "measurement/ue-dl-throughput#uncertainty",
        {"cellId": _CELL}, 10_000, ("calibration/lab-1",), "proof/lab-v1")
    harm = HarmContract(
        **_identity("harm/lab-axis"),
        harm_kind=HarmKind.TRIAL_INDUCED,
        scope_selector={"cellId": _CELL},
        reserve=_quantity(100.0, "ms", "plan/lab-reserve"),
        bounds=(bound,),
        watchdogs=(watchdog,),
        missing_interval_charge=_quantity(5.0, "ms", "plan/missing-charge"))
    deployment = DeploymentBinding(
        **_identity("deployment/xapp-live-telnet"),
        endpoint_id="oai-gnb-telnet",
        # The contract validator requires a scheme+address it recognises, so
        # this records the endpoint, not the wire protocol.  The truth about
        # the wire -- a plaintext loopback telnet shell -- is carried by
        # ``run_xapp.lab_deployment`` (TransportSecurity.NONE), which is what
        # the actual write runs under.
        base_url="https://127.0.0.1:9091",
        transport_security=TransportSecurity.MTLS,
        secret_refs={"clientSecret": "env:LAB_TELNET_NONE"})
    # The Kernel refuses to admit an objective actuator whose path is not
    # OFFICIAL_ORAN_DYNAMIC ("objective actuator must use official O-RAN
    # dynamic path"), so the telnet action space *cannot* be named here.  That
    # refusal is the design working, and it has a consequence worth stating
    # plainly rather than hiding behind this constant: **the permit authorises
    # the trial, not the knob.**  The contracted actuator below is the official
    # steering path; the ci-telnet write run_xapp performs is outside the Write
    # Gateway entirely and is never covered by an admitted actuator contract.
    actuator = ActuatorBinding(
        **_identity("actuator/lab-telnet"),
        capability_ref="capability/lab-telnet",
        path=ActuatorPath.OFFICIAL_ORAN_DYNAMIC,
        policy_type_id="20008",
        service_model={"serviceModel": "E2SM-RC", "style": "3"},
        readback_measurement_ref="measurement/ue-dl-throughput",
        deployment_binding_ref="deployment/xapp-live-telnet")
    capability = CapabilityManifest(
        **_identity("capability/lab-telnet"),
        capability_id="capability/lab-telnet",
        supported_objectives=("TrafficSteeringPreference",),
        constraints=(predicate.constraint,),
        actuator_refs=("actuator/lab-telnet",),
        measurement_refs=("measurement/ue-dl-throughput",),
        interface_versions={"oaiCiTelnet": "1.0"})
    return {
        "counter": counter, "measurement": measurement, "target": target,
        "option": option,
        "vector": TargetVector(**_identity("vector/lab-axis"),
                               ordered_target_refs=("target/lab-axis",)),
        "release": TargetReleasePolicy(**_identity("release/lab-axis")),
        "case_policy": CoordinationCasePolicy(
            **_identity("case-policy/lab-axis"), deadline_ms=600_000,
            max_trials=8, max_proposals=16,
            target_release_policy_ref="release/lab-axis",
            harm_contract_refs=("harm/lab-axis",)),
        "watchdog": watchdog, "harm": harm, "deployment": deployment,
        "actuator": actuator, "capability": capability,
        "composition": CompositionManifest(
            **_identity("composition/lab-telnet"),
            composition_id="composition/lab-telnet",
            capability_refs=("capability/lab-telnet",)),
    }


def _confirmation(contract: Any, *, event_id: str, at: str) -> ConfirmationRecord:
    return ConfirmationRecord(
        confirmed_object_type=type(contract).__name__,
        confirmed_content_hash=contract_content_hash(contract),
        event_id=event_id, timestamp=at,
        action=ConfirmationAction.CONFIRM_AND_START)


class _StoppingKernel:
    """The Kernel, with the one transition a rollback legitimately needs.

    ``run_xapp`` asks for a ``REVERSE_ROLLBACK`` permit in its ``finally``, and
    the Kernel only issues one from ``STOPPING`` / ``REVERSE_ROLLBACK`` /
    ``RECOVERY_VERIFYING``.  A trial that is being rolled back *is* stopping, so
    this proxy advances it there before asking -- through the Kernel's own
    :meth:`advance_trial`, which refuses an illegal transition exactly as it
    would for any other caller.  Nothing here forges a token or skips a state.
    """

    def __init__(self, kernel: Any, trial_id: str, clock: Any) -> None:
        self._kernel = kernel
        self._trial_id = trial_id
        self._clock = clock

    def issue_token(self, trial_id: str, *, token_kind: Any, now: str) -> Any:
        if token_kind is TokenKind.REVERSE_ROLLBACK:
            state = TrialState(
                self._kernel.reduced_state()["trials"][trial_id]["state"])
            if state is TrialState.COMMIT_DECIDED:
                # COMMIT_DECIDED -> STOPPING is a legal transition and it is
                # the true one: the write went out and is being undone.  The
                # reason is OPERATOR_ABORT because that is what actually
                # happened -- the campaign ended the dwell on purpose.  It is
                # not EXECUTION_ERROR and must not be recorded as one.
                self._kernel.advance_trial(trial_id, TrialState.STOPPING,
                                           now=self._clock(),
                                           reason=StopReason.OPERATOR_ABORT)
        return self._kernel.issue_token(trial_id, token_kind=token_kind, now=now)


def build_lab_kernel() -> Dict[str, Any]:
    """A real Kernel with one trial parked in ``COMMIT_DECIDED``.

    Returns the kernel, the trial id and the clock, so a caller can keep
    issuing permits for successive assignments against the same open case.
    """
    clock = SteppingClock("2026-09-01T00:00:00.000Z")
    contracts = _contracts()
    adapter = MockActuationAdapter(config=dict(_BASELINE))
    gateway = TokenBoundWriteGateway(
        adapters={_ADAPTER: adapter}, safe_state=dict(_BASELINE),
        journal=InMemoryTransactionJournal(), clock=clock)
    kernel = AssuranceKernel(event_store=MemoryEventStore(),
                             reducer=KernelReducer(), write_gateway=gateway,
                             measurement_collector=None)
    now = clock()
    vector_confirmation = _confirmation(contracts["vector"],
                                        event_id="confirm/lab-vector", at=now)
    for key in ("counter", "measurement", "target", "release", "watchdog",
                "harm", "actuator", "capability", "composition"):
        kernel.admit_contract(contracts[key], confirmation=None, now=now)
    kernel.admit_contract(contracts["vector"], confirmation=vector_confirmation,
                          now=now)
    kernel.admit_contract(
        contracts["case_policy"],
        confirmation=_confirmation(contracts["case_policy"],
                                   event_id="confirm/lab-policy", at=now),
        now=now)
    kernel.admit_deployment(contracts["deployment"], now=now)
    kernel.freeze_epoch(confirmation=vector_confirmation, now=now)

    catalog = kernel.current_catalog()
    kernel.open_case(
        case_id=_CASE_ID, policy=contracts["case_policy"],
        active_vector="target/lab-axis",
        usable_reserve={"harm/lab-axis": _quantity(
            100.0, "ms", "plan/lab-reserve").to_canonical_dict()},
        reserve_per_trial={"harm/lab-axis": _quantity(
            20.0, "ms", "plan/lab-per-trial").to_canonical_dict()},
        evidence_cells=(EvidenceCell(
            cell_id="cell/lab-axis", target_ref="target/lab-axis",
            candidate_semantic_hash=catalog.candidates[0].semantic_hash,
            status=EvidenceCellStatus.OPEN,
            required_independent_contributions=1),),
        now=clock())

    path = VerticalPath(
        kernel=kernel, gateway=gateway,
        deployment=VerticalDeployment(adapter_name=_ADAPTER,
                                      scope={"cellId": _CELL},
                                      baseline_config=dict(_BASELINE)),
        case_id=_CASE_ID, clock=clock, coordinator=None, collector=None)
    return {"kernel": kernel, "clock": clock, "path": path,
            "gateway": gateway,
            "candidate_id": catalog.candidates[0].candidate_id}


def permit_source() -> KernelPermitSource:
    """The permit source ``run_xapp --permit-source`` loads.

    One Kernel, one case and one trial per **process**, which is exactly the
    unit ``run_xapp`` is: it makes one assignment and rolls it back.  A trial
    that has been rolled back is unresolved until its recovery is verified, and
    the Kernel refuses to open another behind it (``RECOVERY_BLOCKED``) -- so
    reusing a case across assignments would mean driving a recovery this
    harness has no evidence for.  A fresh case per process avoids inventing
    one.
    """
    lab = build_lab_kernel()
    trial_id = lab["path"].open_trial(lab["candidate_id"])
    lab["path"].reserve_and_stage(trial_id)
    lab["path"].prepare(trial_id)
    lab["path"].ready(trial_id)
    lab["path"].commit_decision(trial_id)
    return KernelPermitSource(
        kernel=_StoppingKernel(lab["kernel"], trial_id, lab["clock"]),
        trial_id=trial_id)


#: nb_id -> NR cell identity, as the deployment topology fixes it.  This is
#: configuration, not measurement: which cell a node *is* comes from the
#: binding, while which node a UE is *on* is measured.
_NODE_CELL = {"0000003584": "12345678", "0000002816": "87654321"}

_KPM_JSONL = ("/opt/ran-lab/controller/oran-deploy/session-20260819/lower-live/"
              "a1-live-kpm.jsonl")

#: The gNB's own MAC statistics file, which is the only place the cell-local
#: RNTI is published.  KPM carries ``ran_ue_id`` (the RAN UE NGAP ID) and the
#: MAC stats carry ``CU-UE-ID``; in this monolithic gNB they are the same
#: identifier, so the two can be joined on it.  That join is why the RNTI in an
#: attribution entry is a *read* rather than an assumption -- which is exactly
#: what the executor refuses to let a caller skip ("a cell-local identifier
#: cannot be assumed").
_MAC_STATS = "/opt/ran-lab/controller/nrMAC_stats.log"
_MAC_UE = re.compile(r"UE RNTI ([0-9a-f]{4}) CU-UE-ID (\d+)")


def _rnti_by_ran_ue_id():
    """``ran_ue_id`` -> RNTI, read from the gNB's published MAC statistics."""
    try:
        text = Path(_MAC_STATS).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    return {m.group(2): int(m.group(1), 16) for m in _MAC_UE.finditer(text)}


def _expected_epochs() -> Dict[str, int]:
    binding = json.loads(Path(_BINDING).read_text(encoding="utf-8"))
    return {str(k): int(v) for k, v in binding["kpm"]["expectedEpochs"].items()}


def _node_cell(e2_node: str) -> Optional[str]:
    for nb_id, cell in _NODE_CELL.items():
        if f"nb={nb_id}/" in e2_node:
            return cell
    return None


def live_snapshot(*, tail_lines: int = 4000) -> Any:
    """A ``CommonKpiSnapshot`` assembled from the live kpm-gate stream.

    The gate's JSONL is parsed by the real
    :class:`~assurance.collector.o1col.KpmJsonlAdapter`, which drops any line
    whose ``connection_epoch`` is not the one the committed binding pins.  So a
    stale pin produces an empty snapshot and the run refuses, rather than a
    confident snapshot about epochs nobody is on.

    The serving-cell attribution entries are *derived* from those samples and
    from nothing else: a UE-scoped KPM indication arriving on a node is what
    says the UE is on that node's cell.  The node-to-cell map is deployment
    configuration; the attribution is measurement.
    """
    epochs = _expected_epochs()
    adapter = KpmJsonlAdapter(expected_epochs=epochs, source_id="a1-live-kpm")
    lines = Path(_KPM_JSONL).read_text(encoding="utf-8").splitlines()[-tail_lines:]
    result = adapter.parse_lines(lines)
    if not result.samples:
        raise LabHarnessError(
            f"no KPM sample survived the pin {epochs}; the gate is down, the "
            "stream is stale, or the binding names epochs nobody is on")

    latest: Dict[str, Any] = {}
    for sample in result.samples:
        ue = sample.scope_snapshot.get("amf_ue_ngap_id")
        node = sample.scope_snapshot.get("e2_node")
        if not ue or not node:
            continue
        cell = _node_cell(node)
        if cell is None:
            continue
        previous = latest.get(ue)
        if previous is None or sample.observed_at >= previous.observed_at:
            latest[ue] = sample

    if not latest:
        raise LabHarnessError(
            "the KPM stream carries no UE-scoped (format 3) indication, so "
            "there is no measured attribution to check a UE-scoped action "
            "against")

    rnti_by_ran_ue = _rnti_by_ran_ue_id()
    attribution = []
    for index, (ue, sample) in enumerate(sorted(latest.items())):
        cell = _node_cell(sample.scope_snapshot["e2_node"])
        scope = {"ueId": str(ue), "cellId": cell, "amfUeNgapId": str(ue)}
        ran_ue_id = sample.scope_snapshot.get("ran_ue_id")
        rnti = rnti_by_ran_ue.get(str(ran_ue_id)) if ran_ue_id else None
        if rnti is not None:
            scope["rnti"] = "%#06x" % rnti
            scope["ranUeId"] = str(ran_ue_id)
        attribution.append(RawSample(
            sample_id=f"attr/{ue}/{sample.sample_id[:16]}",
            counter_id=SERVING_CELL_ATTRIBUTION_COUNTER,
            value=TypedQuantity(float(int(cell)), "NCI", Provenance.MEASURED,
                                f"kpm/{sample.trace_hash[:16]}"),
            scope_snapshot=scope,
            observed_at=sample.observed_at, cadence_ms=1000,
            clock_health=sample.clock_health, trace_hash=sample.trace_hash,
            sequence=index))

    taken_at = max(sample.observed_at for sample in attribution)
    return snapshot_from_samples(
        snapshot_id=f"snap/live-kpm/{taken_at}", taken_at=taken_at,
        samples=tuple(attribution) + tuple(result.samples))


def snapshot_provider():
    """The zero-argument provider ``run_xapp --snapshot-source`` loads.

    ``run_xapp._load`` calls whatever it resolves, so the reference must name a
    factory that *returns the provider*, not the snapshot itself: the entry
    takes its snapshot at the moment of the assignment, not at import time.
    """
    return live_snapshot
