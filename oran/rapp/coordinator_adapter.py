"""O-RAN profile for the existing agentic intent coordinator.

**History-only.**  This is the adapter that put ``IntentCoordinator`` behind an
R1 deployment for the published campaign.  Nothing in the deployed runtime
reaches it: ``main.py`` builds the Operator Console over the Assurance Kernel,
and the console imports neither this module nor ``gui_entry`` above it.  It is
imported lazily by ``oran.rapp.headless.run_once`` so that module's settings
loader can be reused by the Kernel-path OTA runner without loading the
preserved coordinator at all.

The profile replaces only the collector/executor boundary.  S0/S1 conflict
screening, S2 model feasibility, calibrated theta routing, S5 negotiation,
history and episode safety remain owned by ``IntentCoordinator``.
"""

from __future__ import annotations

import copy
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, Mapping

from coordinator.episode_types import (
    MonitorVerdict, TerminalOutcome, TerminalReason,
)
from coordinator.history import HistoryMode
from coordinator.intent_coordinator import IntentCoordinator
from decision.intent_model import Intent, IntentStatus
from decision.llm_backend import LLMBackendManager

from .contract_support import jcs_sha256, load_schema
from .evidence import EvidenceLedger
from .policy_translator import (
    AdmissionRejected, POLICY_TYPE_ID, PolicyTranslationContext,
    translate_intent,
)
from .ports import AssuranceDecision, AssurancePort, PolicyDispatchPort


class DuplicateFailedPolicy(AdmissionRejected):
    """A byte-identical policy already failed in this coordinator session."""


@dataclass
class _RAppMetrics:
    """Minimal decision snapshot consumed by the preserved coordinator."""

    ue_id: str
    attached: bool = True
    throughput_mbps: float | None = 0.0
    latency_ms: float | None = None
    rsrp_dbm: float | None = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ue_id": self.ue_id,
            "attached": self.attached,
            "throughput_mbps": self.throughput_mbps,
            "latency_ms": self.latency_ms,
            "rsrp_dbm": self.rsrp_dbm,
            **copy.deepcopy(self.extra),
        }


class RAppDecisionCollector:
    """Read-only O-RAN decision/measurement port.

    DME evidence is deliberately not treated as pre-admission proof.  The S2
    prompt gets a conservative live snapshot, while the authoritative A1+DME
    AND gate remains in ``CombinedAssurance`` after policy execution.
    """

    simulation_mode = False

    def __init__(self):
        self._scheduler = None
        self._probe_config = None
        self._ue_ids: list[str] = []
        self._decision_throughput_mbps = 0.0

    def configure_ues(self, ue_ids: Iterable[str]) -> None:
        self._ue_ids = [str(ue) for ue in ue_ids]

    def set_scheduler(self, scheduler) -> None:
        self._scheduler = scheduler

    def set_probe_config(self, probe_config) -> None:
        self._probe_config = probe_config

    def collect_all(self) -> Dict[str, _RAppMetrics]:
        return {ue: _RAppMetrics(ue, throughput_mbps=
                                 self._decision_throughput_mbps)
                for ue in self._ue_ids}

    def get_throughput_all(self, duration_s=None) -> Dict[str, float]:
        return {ue: self._decision_throughput_mbps for ue in self._ue_ids}

    def get_goodput_passive_all(self, duration_s=None) -> Dict[str, float]:
        return self.get_throughput_all(duration_s)

    def remove_ue(self, ue_id: str) -> None:
        self._ue_ids = [ue for ue in self._ue_ids if ue != str(ue_id)]

    def shutdown(self) -> None:
        return None


@dataclass
class _RAppAxisState:
    power_offset_db: float = 0.0
    prb_cap: int = 0
    sched_priority: float = 1.0
    mcs_offset: int = 0
    ue_sched_priority: Dict[int, float] = field(default_factory=dict)
    ue_prb_cap: Dict[int, int] = field(default_factory=dict)


class R1PolicyExecutor:
    """R1/A1 policy lifecycle port consumed by the O-RAN coordinator profile."""

    def __init__(self, *, dispatch: PolicyDispatchPort, assurance: AssurancePort,
                 capability_manifest: Mapping[str, Any]):
        self.dispatch = dispatch
        self.assurance = assurance
        self.capability = copy.deepcopy(dict(capability_manifest))
        self.states: Dict[str, _RAppAxisState] = {}
        self.rnti_resolver = None
        self.on_rnti_remap = None
        self._failed_policy_digests: set[str] = set()
        self.context: PolicyTranslationContext | None = None
        self.evidence_records: list[Dict[str, Any]] = []
        self.now: datetime | None = None
        self.last_trial: Dict[str, Any] | None = None

    def configure_gnbs(self, gnb_ids: Iterable[str]) -> None:
        self.states = {str(gnb): _RAppAxisState() for gnb in gnb_ids}

    def get_all_offsets(self) -> Dict[str, float]:
        """Cached power offsets per gNB, mirroring the OAI executor contract.

        The base coordinator's POWER_CONSTRAINT monitor queries this during
        network-state evaluation. The O-RAN executor never actuates power over
        telnet, so every axis state carries its default 0.0 offset; the guard
        then reads within-bound instead of raising AttributeError and sinking
        the whole episode into a TechnicalFailsafe with an empty trace.
        """
        return {gid: s.power_offset_db for gid, s in self.states.items()}

    def begin_episode(self, *, context: PolicyTranslationContext,
                      evidence_records: Iterable[Dict[str, Any]],
                      now: datetime) -> None:
        self.context = context
        self.evidence_records = copy.deepcopy(list(evidence_records))
        self.now = now
        self.last_trial = None

    def execute(self, intent: Intent) -> Dict[str, Any]:
        if self.context is None or self.now is None:
            raise RuntimeError("R1 executor episode context was not initialized")
        self.dispatch.bootstrap_info()
        self.dispatch.discover_services(api_name="a1-policy-management")
        policy_type_object = self.dispatch.get_policy_type(POLICY_TYPE_ID)
        if (isinstance(policy_type_object, Mapping)
                and not isinstance(policy_type_object.get("policySchema"), Mapping)
                and isinstance(policy_type_object.get("schemaSha256"), str)):
            policy_schema = load_schema(
                "AIC_UECellSteering_1.0.0.policy")
            status_schema = load_schema(
                "AIC_UECellSteering_1.0.0.status")
            advertised = self.capability.get("schemaDigests", {})
            if (policy_type_object["schemaSha256"] != jcs_sha256(policy_schema)
                    or advertised.get("policy") != jcs_sha256(policy_schema)
                    or advertised.get("status") != jcs_sha256(status_schema)):
                raise AdmissionRejected(
                    "R1 policy-type digest differs from pinned schemas")
            policy_type_object = {
                **dict(policy_type_object),
                "policySchema": policy_schema,
                "statusSchema": status_schema,
            }
        discovery = {
            "policyTypeIds": self.dispatch.discover_policy_types(),
            "policyTypeObject": policy_type_object,
        }
        policy = translate_intent(
            intent, policy_type_discovery=discovery,
            capability_manifest=self.capability, context=self.context)
        digest = jcs_sha256(policy)
        if digest in self._failed_policy_digests:
            raise DuplicateFailedPolicy("same failed policy is not repeated")
        try:
            created = self.dispatch.create_policy(
                self.capability["nearRtRicId"], POLICY_TYPE_ID, policy)
            policy_id = created["policyId"]
            data_job = self.dispatch.create_continuous_job(
                policy_id=policy_id,
                policy_revision=policy["trace"]["policyRevision"],
                near_rt_ric_id=self.capability["nearRtRicId"])
            status = self.dispatch.get_policy_status(policy_id)
            judgement = self.assurance.assess(
                policy, policy_id, status, self.evidence_records, now=self.now)
        except Exception:
            self._failed_policy_digests.add(digest)
            raise
        trial = {
            "policy": policy,
            "policyId": policy_id,
            "location": created.get("location"),
            "dataJob": data_job,
            "policy_status": status,
            "evidence_records": copy.deepcopy(self.evidence_records),
            "judgement": judgement,
            "policy_digest": digest,
        }
        if judgement is not AssuranceDecision.SATISFIED:
            self._failed_policy_digests.add(digest)
        self.last_trial = trial
        return trial


class OranIntentCoordinator(IntentCoordinator):
    """The preserved coordinator with only its S3/S4 boundary replaced."""

    def __init__(self, *, collector: RAppDecisionCollector,
                 executor: R1PolicyExecutor,
                 llm_manager: LLMBackendManager | None = None,
                 config=None):
        super().__init__(config=config, collector=collector, executor=executor,
                         llm_manager=llm_manager)
        collector.configure_ues(self.config.network.ues.keys())
        executor.configure_gnbs(self.config.network.gnbs.keys())
        self.history_mode = HistoryMode.ONLINE_RESERVOIR
        self._profile_committed_intent: Intent | None = None

    def _coordination_cycle(self, cycle: Dict[str, Any],
                            current_intent: Intent,
                            active_intents: list[Intent]):
        """Run real S2 admission, then the injected R1/DME S3/S4 surface."""
        self._cur_schema_valid = None
        self._cur_schema_reason = None
        self._cur_proposal_generated = False
        self._cur_prompt_hash = None
        self._cur_inference_ms = None
        self._cur_schema_ms = None
        self._cur_admission_ms = None
        if self._deadline_expired():
            from coordinator.fsm import OperationTimeout
            raise OperationTimeout("episode deadline exceeded before feasibility")

        self._transition("S2")
        network_state = self._get_network_state()
        try:
            feasibility = self._analyze_feasibility(
                current_intent, active_intents, network_state)
        finally:
            self._stamp_proposal_stage(cycle)
        cycle["confidence"] = feasibility.confidence
        cycle["feasible"] = feasibility.feasible
        cycle["target_value"] = float(current_intent.target.target_value)
        self._cur_adm_t0 = time.monotonic()

        theta_star = self.calibrator.get_theta_star()
        raw_confidence = feasibility.confidence
        if not getattr(self, "_calibration_context_ok", True):
            calibrated, reason = 0.0, "fail_closed_context_selection"
        else:
            calibrated, reason = self._calibrated_probability(raw_confidence)
        self._bind_cycle_calibration(
            cycle, raw_confidence, calibrated, theta_star, reason)
        if not (feasibility.feasible and calibrated >= theta_star):
            self._finalize_schema_admission(cycle)
            cycle["routed_to"] = "negotiation"
            return feasibility, False

        ledger = getattr(self, "_reserve_ledger", None)
        if ledger is not None and not ledger.reserve_trial(
                "revised_trial" if cycle.get("cycle") else "initial_trial",
                cycle_index=cycle.get("cycle"), cycle_id=cycle.get("cycle_id")):
            self._finalize_schema_admission(cycle)
            cycle["routed_to"] = "budget_blocked"
            cycle["terminal_override"] = (
                TerminalOutcome.PENDING_NOT_ADMITTED,
                TerminalReason.INSUFFICIENT_TRIAL_BUDGET)
            return feasibility, False
        if ledger is not None:
            cycle["trial_reservation_id"] = ledger.last_reservation_id

        self._finalize_schema_admission(cycle)
        cycle["routed_to"] = "trial"
        self._transition("S3")
        try:
            trial = self._bounded_call(
                lambda: self.executor.execute(current_intent),
                float(getattr(self, "model_call_timeout_s", 30.0)),
                label="R1 policy lifecycle and DME assurance")
        except AdmissionRejected as exc:
            cycle["trial_success"] = False
            cycle["profile_error"] = str(exc)
            cycle["trial_stats"] = self._profile_trial_stats(False)
            return feasibility, False
        except Exception as exc:
            cycle["trial_success"] = False
            cycle["profile_error"] = str(exc)
            cycle["trial_stats"] = self._profile_trial_stats(False)
            self._transition("S5")
            cycle["terminal_override"] = (
                TerminalOutcome.TECHNICAL_FAILSAFE,
                TerminalReason.INTERNAL_ERROR)
            return feasibility, False

        self._transition("S4")
        judgement = trial["judgement"]
        cycle["profile_trial"] = {
            key: copy.deepcopy(value) for key, value in trial.items()
            if key != "judgement"
        }
        cycle["assurance_decision"] = judgement.value
        cycle["monitor_verdicts"] = {
            current_intent.id: (
                MonitorVerdict.SATISFIED.value
                if judgement is AssuranceDecision.SATISFIED
                else MonitorVerdict.VIOLATED.value
                if judgement is AssuranceDecision.VIOLATED
                else MonitorVerdict.UNKNOWN.value)
        }
        cycle["trial_success"] = judgement is AssuranceDecision.SATISFIED
        cycle["trial_stats"] = self._profile_trial_stats(
            judgement is AssuranceDecision.SATISFIED)
        if judgement is AssuranceDecision.SATISFIED:
            self._profile_committed_intent = current_intent
            return feasibility, True
        if judgement is AssuranceDecision.UNKNOWN:
            cycle["terminal_override"] = (
                TerminalOutcome.TECHNICAL_FAILSAFE,
                TerminalReason.INTERNAL_ERROR)
        return feasibility, False

    def _profile_trial_stats(self, success: bool) -> Dict[str, Any]:
        values = list(self.ue_collector.get_throughput_all().values())
        mean = (sum(values) / len(values)) if values else 0.0
        return {"tput_before": mean, "tput_min": mean,
                "tput_after": mean, "tau": 0.0,
                "profile_assurance": True, "success": bool(success)}

    def _settle_after_cleanup(self, result: Dict[str, Any]) -> Dict[str, Any]:
        result = super()._settle_after_cleanup(result)
        if result.get("terminal_outcome") not in {
                TerminalOutcome.COMMIT_ORIGINAL.value,
                TerminalOutcome.COMMIT_REVISED.value}:
            return result
        committed = self._profile_committed_intent
        if committed is None:
            return self._finalize_episode(
                result, TerminalOutcome.TECHNICAL_FAILSAFE,
                TerminalReason.INTERNAL_ERROR,
                pending_intent=result.get("pending_intent"),
                committed_revision=None)
        committed.status = IntentStatus.ACTIVE
        if not self.intent_manager.add(committed):
            return self._finalize_episode(
                result, TerminalOutcome.TECHNICAL_FAILSAFE,
                TerminalReason.INTERNAL_ERROR, pending_intent=committed,
                committed_revision=None)
        return result


class RAppCoordinatorAdapter:
    """Own the O-RAN ports while delegating all decisions to IntentCoordinator."""

    def __init__(self, *, dispatch: PolicyDispatchPort,
                 assurance: AssurancePort, ledger: EvidenceLedger,
                 capability_manifest: Mapping[str, Any],
                 transition_capacity: int = 512,
                 negotiator: Callable[[Any, str], Any | None] | None = None,
                 llm_manager: LLMBackendManager | None = None,
                 config=None):
        self.dispatch = dispatch
        self.assurance = assurance
        self.ledger = ledger
        self.capability = copy.deepcopy(dict(capability_manifest))
        self.transition_capacity = int(transition_capacity)
        self.negotiator = negotiator
        self._lock = threading.RLock()
        self._transitions: list[Dict[str, Any]] = []
        collector = RAppDecisionCollector()
        executor = R1PolicyExecutor(
            dispatch=dispatch, assurance=assurance,
            capability_manifest=capability_manifest)
        self.coordinator = OranIntentCoordinator(
            collector=collector, executor=executor,
            llm_manager=llm_manager, config=config)
        self.coordinator.on_state_change = self._on_state_change
        if negotiator is not None:
            def _legacy_negotiation_policy(alternatives):
                response = negotiator(alternatives[0], "agentic_s5")
                if response in ("accept", "reject"):
                    return response
                return "accept" if response is not None else "reject"
            self.coordinator.on_negotiation_needed = _legacy_negotiation_policy
        self._identifiers: Dict[str, Any] = {}
        self._process_intent_calls = 0
        self._last_real_fsm_history: list[Dict[str, Any]] = []
        self._last_terminal_outcome: str | None = None
        self._last_terminal_evidence_ref: str | None = None
        self._last_ledger_references: list[str] = []

    def _on_state_change(self, source: str, target: str) -> None:
        self._transition(source, target, "FSM_EVENT", None, origin="REAL")

    def _transition(self, source: str, target: str, outcome: str,
                    evidence_ref: str | None, *, origin: str) -> None:
        event = {"from": source, "to": target, "outcome": outcome,
                 "evidenceRef": evidence_ref, "origin": origin}
        with self._lock:
            self._transitions.append(event)
            if len(self._transitions) > self.transition_capacity:
                del self._transitions[:-self.transition_capacity]

    def transition_snapshot(self) -> list[Dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._transitions)

    def harness_reset(self) -> None:
        """Clear development-only scenario observations, never policy state."""
        with self._lock:
            self._transitions.clear()
            self._process_intent_calls = 0
            self._last_real_fsm_history.clear()
            self._last_terminal_outcome = None
            self._last_terminal_evidence_ref = None
            self._last_ledger_references.clear()

    def harness_observations(self) -> Dict[str, Any]:
        """Expose proof that the production Coordinator path actually ran."""
        with self._lock:
            return {
                "processIntentCalls": self._process_intent_calls,
                "coordinatorExecutionMode": (
                    "REAL_PROCESS_INTENT" if self._process_intent_calls else
                    "SYNTHETIC_CONTRACT_TRANSITION"),
                "coordinatorFsmHistory": copy.deepcopy(
                    self._last_real_fsm_history),
                "coordinatorTerminalOutcome": self._last_terminal_outcome,
                "coordinatorTerminalEvidenceRef": (
                    self._last_terminal_evidence_ref),
                "coordinatorLedgerReferences": copy.deepcopy(
                    self._last_ledger_references),
            }

    def harness_transition(self, source: str, target: str,
                           outcome: str | None = None) -> Dict[str, Any]:
        """Record a contract-runner counterpart transition with continuity."""
        allowed = {"S0", "S1", "S2", "S3", "S4", "S5", "S6"}
        if source not in allowed or target not in allowed:
            raise ValueError("unknown coordinator state")
        with self._lock:
            previous = self._transitions[-1]["to"] if self._transitions else source
            if previous != source:
                raise ValueError("coordinator transition is discontinuous")
        self._transition(source, target, outcome or "CONFORMANCE_EVENT", None,
                         origin="SYNTHETIC")
        return {"state": target, "outcome": outcome or "CONFORMANCE_EVENT"}

    def process_intent(self, intent: Intent | str, *,
                       context: PolicyTranslationContext,
                       evidence_records: Iterable[Dict[str, Any]],
                       now: datetime,
                       identifiers: Dict[str, Any]) -> Dict[str, Any]:
        self._identifiers = copy.deepcopy(dict(identifiers))
        with self._lock:
            self._process_intent_calls += 1
        self.coordinator.executor.begin_episode(
            context=context, evidence_records=evidence_records, now=now)
        if isinstance(intent, str):
            result = self.coordinator.process_intent_text(intent)
        else:
            result = self.coordinator.resolve_pending_intent(intent)
        self._annotate_terminal_transition(result)
        ledger_records = self._append_ledger(result)
        with self._lock:
            self._last_real_fsm_history = copy.deepcopy(self._transitions)
            self._last_terminal_outcome = result.get("terminal_outcome")
            self._last_terminal_evidence_ref = result.get("evidence_record_id")
            self._last_ledger_references = [
                record["evidenceRef"] for record in ledger_records
                if isinstance(record.get("evidenceRef"), str)
                and record["evidenceRef"]
            ]
        return result

    def process_admitted_intent(self, intent: Any, **kwargs) -> Dict[str, Any]:
        """Compatibility name; admission is now computed, never assumed."""
        return self.process_intent(intent, **kwargs)

    def _annotate_terminal_transition(self, result: Dict[str, Any]) -> None:
        with self._lock:
            for event in reversed(self._transitions):
                if event["to"] == "S6":
                    event["outcome"] = result.get("terminal_outcome")
                    event["evidenceRef"] = result.get("evidence_record_id")
                    break

    def _append_ledger(self, result: Dict[str, Any]) -> list[Dict[str, Any]]:
        chain = copy.deepcopy(self._identifiers)
        chain.update({
            "run_id": result.get("experiment_run_id", chain.get("run_id")),
            "episode_id": result.get("episode_id", chain.get("episode_id")),
        })
        trial = self.coordinator.executor.last_trial
        if not trial:
            return []
        policy = trial["policy"]
        status = trial["policy_status"]
        evidence_records = trial["evidence_records"]
        policy_id = trial["policyId"]
        chain.update({
            "intent_id": policy["trace"]["intentId"],
            "intent_revision": policy["trace"]["intentRevision"],
            "policyTypeId": POLICY_TYPE_ID,
            "policyId": policy_id,
            "policyRevision": policy["trace"]["policyRevision"],
            "r1_request_id": chain.get("r1_request_id") or str(uuid.uuid4()),
            "kpi_window": evidence_records[0]["window"] if evidence_records else None,
            "scope": policy["scope"],
        })
        aic = status.get("aicStatus", {})
        ledger_records = [self.ledger.append(
            stage="COORDINATOR_JUDGEMENT", identifiers=chain,
            outcome=result.get("terminal_outcome", "UNKNOWN"),
            evidence_ref=result.get("evidence_record_id"))]
        ledger_records.append(self.ledger.append(
            stage="A1_POLICY_LIFECYCLE", identifiers=chain,
            outcome=status.get("enforceStatus", "UNKNOWN"),
            evidence_ref=f"r1-policy-status:{policy_id}"))
        ledger_records.append(self.ledger.append(
            stage="NEAR_RT_ACTION_EVIDENCE", identifiers=chain,
            outcome=aic.get("episodeState", "UNKNOWN"),
            evidence_ref=(f"episode:{aic['episodeId']}"
                          if aic.get("episodeId") else None)))
        ledger_records.append(self.ledger.append(
            stage="INTENT_SATISFACTION", identifiers=chain,
            outcome=trial["judgement"].value,
            evidence_ref=(f"dme-observation:{evidence_records[0]['observationId']}"
                          if evidence_records else None)))
        return ledger_records
