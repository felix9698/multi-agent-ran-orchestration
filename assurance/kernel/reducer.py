"""Event reduction and the terminal state hash.

Owner lane: **KERN**.  Signatures frozen by this design step.

Design section 4.3 states the requirement in one sentence -- the same accepted
event stream and reducer version reproduce the same ledger and terminal state
hash without any LLM -- and design section 6.3 makes the reducer version part
of what an evidence epoch freezes.  Those two together are why
:attr:`Reducer.reducer_version` is a declared part of the protocol rather than
a module constant: a stream is only replayable against the reducer version it
was reduced with, so the version has to travel with the reducer and be
recordable in the epoch.

A reducer must be a **pure fold**.  Given a state and an envelope it returns
the next state, and it does nothing else: no clock, no random, no I/O, no
network, no model.  Everything time-dependent is already in the envelope's
timestamp, and everything decision-dependent is already in its payload.  A
reducer that read the wall clock would produce a different ledger on replay
and quietly break design section 17.5 -- "Raw evidence deterministically
reproduces all objective verdicts, ledger updates, figures, and terminal
states".
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Iterable, Mapping, Protocol, runtime_checkable

from assurance.core.addressing import content_hash
from assurance.core.axes import (
    CandidateAvailability,
    ExecutionValidity,
    MeasurementSufficiency,
    PredicateVerdict,
    TrialOutcome,
)
from assurance.core.envelopes import EventEnvelope
from assurance.core.states import IllegalTransitionError, TrialState, assert_transition

__all__ = [
    "KernelReducer",
    "Reducer",
    "ReducerError",
    "replay",
    "terminal_state_hash",
]


#: 그 설정에 대해 아무것도 알려 주지 않은 시행 결과 -- 후보를 소진하지 않는다 (§5.1).
_NOTHING_LEARNED_OUTCOMES = frozenset({
    TrialOutcome.INDETERMINATE, TrialOutcome.INVALID, TrialOutcome.EXEC_ERROR})


class ReducerError(RuntimeError):
    """An event could not be folded into the state.

    Raised for an event the reducer does not recognise, or one whose
    transition is illegal (see
    :func:`assurance.core.states.assert_transition`).  Skipping an unknown
    event would make replay silently diverge from the original run while still
    producing a hash, which is worse than failing.
    """


@runtime_checkable
class Reducer(Protocol):
    """Folds the accepted event stream into the ledger and case state."""

    #: Version of this reduction logic.  Frozen into the evidence epoch
    #: (design section 6.3) and compared on replay; a mismatch means the
    #: stream must be replayed with the recorded version, not the current one.
    reducer_version: str

    def initial_state(self) -> Mapping[str, Any]:
        """The state before any event.

        Signature frozen; body owned by lane **KERN**.

        Must be a value, not a reference to shared mutable state: two replays
        running concurrently must not be able to observe each other.
        """
        ...

    def apply(
        self, state: Mapping[str, Any], envelope: EventEnvelope
    ) -> Mapping[str, Any]:
        """Return the state after folding *envelope* into *state*.

        Signature frozen; body owned by lane **KERN**.

        Pure: no mutation of *state*, no I/O, no clock, no randomness.
        Returning a new mapping rather than mutating is what makes a partial
        replay resumable and a divergence bisectable.

        Must raise :class:`ReducerError` for an unrecognised event kind and
        for an illegal trial transition.  Design acceptance criterion 17.3
        requires the Kernel to "reject every illegal one"; on the replay side,
        rejection means refusing to produce a state at all.
        """
        ...


class KernelReducer:
    """Pure reducer for the KERN-owned event vocabulary."""

    #: 1.1.0 -> 1.2.0 (2026-09-22).  INDETERMINATE·INVALID·EXEC_ERROR 뒤 candidate 가
    #: CONSUMED 에서 AVAILABLE 로 바뀌었다 -- 관측이 불충분했거나 적용 전에 거절돼
    #: 아무것도 바뀌지 않은 시행이 후보를 소진시키지 않는다.  **의미가 달라졌으므로
    #: 판본도 달라야 한다**: 이 문자열은 `terminal_state_hash` 에 섞여 들어가므로,
    #: 같은 판본표를 단 두 규칙이 존재하면 증거 사슬이 거짓을 말한다.
    #:
    #: 올리면 `_apply_owned` 의 EpochFrozen 검사와 `kernel.py` 의 epoch 승인이
    #: 1.1.0 으로 얼린 **기존 event stream 을 거부한다.**  오너 판단(2026-09-22):
    #: 그 기록은 어차피 데이터로 쓰지 않으므로 재생 호환을 유지할 이유가 없다.
    #: 보관 stream 은 로그로서만 값이 있다.
    reducer_version = "assurance-kernel-reducer/1.2.0"

    #: **규칙을 판본에 묶는다.**  이 집합이 1.1.0 과 1.2.0 을 가르는 유일한 차이이므로,
    #: 기록된 stream 을 기록된 판본으로 접으려면 이름표만이 아니라 이 규칙도 되돌려야
    #: 한다.  그러지 않으면 `_reducer_as_recorded` 는 이름만 바꾸고 1.2.0 규칙으로 접어
    #: `terminal_state_hash` 가 어긋난다 -- 2026-09-23 실측: `EXEC_ERROR` 로 정착한
    #: stream 둘(`LIVECONSOLE-QoSTarget-20260906T092318Z`, `...092723Z`)이 정확히 그렇게
    #: 재생에 실패했고, 그 결과가 없는 stream 은 통과했다.
    nothing_learned_outcomes = _NOTHING_LEARNED_OUTCOMES

    def initial_state(self) -> Mapping[str, Any]:
        return {
            "activeEpoch": None,
            "candidateAvailability": {},
            "candidateAvailabilityByEpoch": {},
            "cases": {},
            "catalog": None,
            "catalogs": {},
            "compatibility": {},
            "contracts": {},
            "deployments": {},
            "epochs": {},
            "evidenceCells": {},
            "exhaustionCertificates": {},
            "fences": {},
            "harmLedger": [],
            "pendingHarmCharges": {},
            "resourceLocks": {},
            "samples": {},
            "transactions": {},
            "trialLedger": [],
            "trials": {},
        }

    @staticmethod
    def _trial(state: Dict[str, Any], trial_id: str) -> Dict[str, Any]:
        try:
            return state["trials"][trial_id]
        except KeyError as exc:
            raise ReducerError(f"unknown trial: {trial_id}") from exc

    def apply(
        self, state: Mapping[str, Any], envelope: EventEnvelope
    ) -> Mapping[str, Any]:
        return self._apply_owned(deepcopy(dict(state)), envelope)

    def _apply_owned(
        self, next_state: Dict[str, Any], envelope: EventEnvelope
    ) -> Mapping[str, Any]:
        """Reduce into an exclusively owned replay accumulator.

        Only replay and the Kernel's private cursor may use this transient
        update.  Public ``apply`` still isolates its input, including on a
        rejected event.  Copy incoming payloads so the accumulator never
        modifies or lends mutable references to the accepted event stream.
        A caller must discard its accumulator if reduction raises.
        """
        if not isinstance(envelope, EventEnvelope) or not envelope.verify_content_hash():
            raise ReducerError("reducer received an invalid event envelope")
        payload = deepcopy(dict(envelope.payload))
        kind = envelope.event_kind

        # Advisory traffic is retained in the stream but has no reduction
        # effect.  Removing agents from replay therefore cannot change state.
        if kind in {"AdvisoryAccepted", "AdvisoryRejected", "GatewayResultRejected"}:
            return next_state

        if kind == "ContractAdmitted":
            contract_hash = str(payload["contractHash"])
            next_state["contracts"][contract_hash] = payload
            return next_state

        if kind == "DeploymentAdmitted":
            binding_hash = str(payload["bindingHash"])
            next_state["deployments"][binding_hash] = payload
            return next_state

        if kind == "CatalogFrozen":
            if payload.get("domain"):
                # A frozen domain (2026-09-19): the cardinality is the sum of
                # each block's product of allowed-value counts.
                sizes = []
                for block in payload["domain"]:
                    product = 1
                    for values in block.get("values", []):
                        product *= len(values)
                    if product != int(block.get("size", -1)):
                        raise ReducerError("catalog domain block size is not its product")
                    sizes.append(product)
                if int(payload.get("cardinality", -1)) != sum(sizes) or payload.get("candidates"):
                    raise ReducerError("catalog cardinality does not match membership")
            elif int(payload.get("cardinality", -1)) != len(payload.get("candidates", [])):
                raise ReducerError("catalog cardinality does not match membership")
            epoch_ref = str(payload["epochRef"])
            next_state["catalogs"][epoch_ref] = payload
            return next_state

        if kind == "EpochFrozen":
            epoch_id = str(payload["epochId"])
            if payload.get("reducerVersion") != self.reducer_version:
                raise ReducerError("epoch reducer version does not match reducer")
            try:
                catalog = next_state["catalogs"][epoch_id]
            except KeyError as exc:
                raise ReducerError("epoch has no staged catalog") from exc
            if catalog.get("catalogHash") != payload.get("catalogHash"):
                raise ReducerError("epoch catalog hash does not match staged catalog")
            next_state["epochs"][epoch_id] = payload
            next_state["activeEpoch"] = epoch_id
            next_state["catalog"] = catalog
            epoch_availability = next_state["candidateAvailabilityByEpoch"].setdefault(
                epoch_id, {}
            )
            for candidate in catalog.get("candidates", []):
                candidate_id = str(candidate["candidateId"])
                epoch_availability.setdefault(
                    candidate_id, CandidateAvailability.AVAILABLE.value
                )
            next_state["candidateAvailability"] = epoch_availability
            return next_state

        if kind == "CaseOpened":
            case_id = str(payload["caseId"])
            if case_id in next_state["cases"]:
                raise ReducerError(f"case already exists: {case_id}")
            next_state["cases"][case_id] = {
                **payload,
                "activeVector": payload.get("activeVector"),
                "deployedSuccess": False,
                "paused": False,
                "terminal": None,
            }
            return next_state

        if kind == "CasePauseChanged":
            case_id = str(payload["caseId"])
            try:
                case = next_state["cases"][case_id]
            except KeyError as exc:
                raise ReducerError(f"unknown case: {case_id}") from exc
            case["paused"] = bool(payload["paused"])
            return next_state

        if kind == "TrialOpened":
            trial_id = str(payload["trialId"])
            if trial_id in next_state["trials"]:
                raise ReducerError(f"trial already exists: {trial_id}")
            next_state["trials"][trial_id] = {
                **payload,
                "applyCounted": False,
                "baselineHash": None,
                "commitAcknowledged": False,
                "configurationReread": False,
                "observedConfigHash": None,
                "planAdapter": None,
                "planAppliedHash": None,
                "planAxes": [],
                "planBaselineHash": None,
                "planHash": None,
                "planPrefixHashes": [],
                "planWatchdogIds": [],
                "evaluation": {
                    "executionValidity": ExecutionValidity.NOT_EVALUATED.value,
                    "mandatoryPredicateIds": [],
                    "measurementSufficiency": MeasurementSufficiency.NOT_EVALUATED.value,
                    "predicateVerdicts": {},
                },
                "finalizeAcknowledged": False,
                "gatewayReadyAcknowledged": False,
                "guardsArmed": False,
                "harmClockStartedAt": None,
                "outcome": TrialOutcome.NOT_SETTLED.value,
                "observationStartedAt": None,
                "readyBaselineHash": None,
                "readyEvidenceRefs": [],
                "recoveryConfigurationReread": False,
                "recoveryVerified": False,
                "settlementEventId": None,
                "state": TrialState.PROPOSED.value,
                "stopReason": None,
                "successDecisionDurable": False,
            }
            candidate_id = payload.get("candidateId")
            trial_epoch = str(payload["epochId"])
            epoch_availability = next_state["candidateAvailabilityByEpoch"].setdefault(
                trial_epoch, {}
            )
            # A domain records only the points it has used, so an untouched
            # member is tracked from its first trial on.
            if candidate_id in epoch_availability or (
                    candidate_id is not None
                    and (next_state["catalogs"].get(trial_epoch) or {}).get("domain")):
                epoch_availability[candidate_id] = (
                    CandidateAvailability.IN_FLIGHT.value
                )
                if next_state["activeEpoch"] == trial_epoch:
                    next_state["candidateAvailability"] = epoch_availability
            return next_state

        if kind == "TrialPlanStaged":
            trial = self._trial(next_state, str(payload["trialId"]))
            trial["planAdapter"] = payload["adapter"]
            trial["planAppliedHash"] = payload["appliedConfigHash"]
            trial["planAxes"] = list(payload.get("axes", []))
            trial["planBaselineHash"] = payload["baselineConfigHash"]
            trial["planHash"] = payload["planHash"]
            trial["planPrefixHashes"] = list(payload.get("prefixConfigHashes", []))
            trial["planWatchdogIds"] = list(payload.get("watchdogIds", []))
            return next_state

        if kind == "TrialCommitReadinessRecorded":
            trial = self._trial(next_state, str(payload["trialId"]))
            trial["guardsArmed"] = bool(payload["watchdogsArmed"])
            trial["armedHarmContractRefs"] = list(
                payload.get("armedHarmContractRefs", [])
            )
            trial["armedWatchdogEvidenceRefs"] = list(
                payload.get("armedWatchdogEvidenceRefs", [])
            )
            trial["baselineHash"] = payload.get("baselineHash")
            return next_state

        if kind == "TrialStateChanged":
            trial_id = str(payload["trialId"])
            trial = self._trial(next_state, trial_id)
            try:
                source = TrialState(str(payload["from"]))
                target = TrialState(str(payload["to"]))
            except ValueError as exc:
                raise ReducerError("unknown trial state in transition") from exc
            if trial["state"] != source.value:
                raise ReducerError(
                    f"transition source {source.value} does not match "
                    f"current state {trial['state']}"
                )
            try:
                assert_transition(source, target)
            except IllegalTransitionError as exc:
                raise ReducerError(str(exc)) from exc
            trial["state"] = target.value
            if payload.get("reason") is not None:
                trial["stopReason"] = payload["reason"]
            if target is TrialState.APPLYING and not trial["applyCounted"]:
                trial["applyCounted"] = True
                trial["harmClockStartedAt"] = envelope.timestamp
                next_state["trialLedger"].append(
                    {
                        "caseId": trial["caseId"],
                        "epochId": trial["epochId"],
                        "eventId": envelope.event_id,
                        "startedAt": envelope.timestamp,
                        "trialId": trial_id,
                    }
                )
            if target is TrialState.OBSERVING:
                trial["observationStartedAt"] = envelope.timestamp
            if target is TrialState.COMMIT_DECIDED:
                transaction_id = str(payload["transactionId"])
                next_state["transactions"].setdefault(
                    transaction_id,
                    {
                        "resolution": None,
                        "resolved": False,
                        "trialId": trial_id,
                    },
                )
            if target is TrialState.FINALIZING_LIVE:
                trial["successDecisionDurable"] = True
            return next_state

        if kind == "TokenIssued":
            trial = self._trial(next_state, str(payload["trialId"]))
            resource_id = str(payload["resourceId"])
            fence = int(payload["fencingToken"])
            if fence < int(next_state["fences"].get(resource_id, -1)):
                raise ReducerError("old fencing token in TokenIssued event")
            next_state["fences"][resource_id] = fence
            transaction_id = str(payload["transactionId"])
            transaction = next_state["transactions"].setdefault(
                transaction_id,
                {"resolved": False, "resolution": None, "trialId": trial["trialId"]},
            )
            transaction.update(payload)
            return next_state

        if kind == "GatewayResultRecorded":
            trial = self._trial(next_state, str(payload["trialId"]))
            resource_id = str(payload["resourceId"])
            if int(payload["fencingToken"]) < int(
                next_state["fences"].get(resource_id, -1)
            ):
                raise ReducerError("old fencing token in gateway result")
            transaction_id = str(payload["transactionId"])
            transaction = next_state["transactions"].setdefault(
                transaction_id,
                {"resolved": False, "resolution": None, "trialId": trial["trialId"]},
            )
            transaction["lastGatewayResult"] = payload
            # The Kernel's belief about the live configuration of this trial's
            # resource is exactly what the gateway last read back.  Every
            # permit names it (KernelToken.expected_config_hash: "what the
            # gateway must observe before acting"), so it has to be part of
            # the reduced state rather than of process memory.
            if payload.get("observedConfigHash") is not None:
                trial["observedConfigHash"] = payload["observedConfigHash"]
            if payload.get("tokenKind") == "READY" and payload.get("outcome") in {
                "ACKED",
                "ALREADY_APPLIED",
            }:
                baseline_hash = payload.get("observedConfigHash")
                if baseline_hash is not None:
                    trial["gatewayReadyAcknowledged"] = True
                    trial["readyBaselineHash"] = baseline_hash
                    trial["readyEvidenceRefs"] = list(
                        payload.get("evidenceRefs", [])
                    )
            # A commit is acknowledged when the readback shows the *staged
            # applied* configuration live -- not when it shows the permit's
            # expected one, which names the configuration that had to be there
            # *before* the write (design section 9: an acknowledgement is not
            # success; the contracted readback is).
            if payload.get("tokenKind") == "COMMIT" and payload.get("outcome") in {
                "ACKED",
                "ALREADY_APPLIED",
            } and payload.get("observedConfigHash") is not None and payload.get(
                "observedConfigHash"
            ) == trial.get("planAppliedHash"):
                trial["commitAcknowledged"] = True
            if payload.get("tokenKind") == "CONFIGURATION_REREAD" and payload.get(
                "outcome"
            ) in {"ACKED", "ALREADY_APPLIED"} and payload.get(
                "observedConfigHash"
            ) == payload.get("expectedConfigHash"):
                # The live-finalize reread is the one taken in
                # FINALIZING_LIVE; a reread taken anywhere on the recovery
                # path is recovery evidence and must never be readable as the
                # contracted reread a success settlement requires.
                if trial["state"] == TrialState.FINALIZING_LIVE.value:
                    trial["configurationReread"] = True
                else:
                    trial["recoveryConfigurationReread"] = True
            if payload.get("tokenKind") == "FINALIZE_LIVE" and payload.get(
                "outcome"
            ) in {"ACKED", "ALREADY_APPLIED"}:
                trial["finalizeAcknowledged"] = True
            if payload.get("tokenKind") == "RECOVERY_CONFIRM" and payload.get(
                "outcome"
            ) in {"ACKED", "ALREADY_APPLIED"} and trial.get(
                "recoveryConfigurationReread"
            ):
                trial["recoveryVerified"] = True
            return next_state

        if kind == "HarmCharged":
            trial_id = str(payload["trialId"])
            next_state["pendingHarmCharges"].setdefault(trial_id, []).append(
                {
                    **payload,
                    "observedAt": envelope.timestamp,
                    "observedEventId": envelope.event_id,
                }
            )
            return next_state

        if kind == "HarmReserved":
            movements = payload.get("reservations", [])
            if not movements:
                raise ReducerError("harm event has no movements")
            for movement in movements:
                entry = {
                    **movement,
                    "eventId": envelope.event_id,
                    "timestamp": envelope.timestamp,
                    "trialId": payload["trialId"],
                }
                next_state["harmLedger"].append(entry)
            trial_id = str(payload["trialId"])
            for resource_id in payload.get("resourceIds", []):
                owner = next_state["resourceLocks"].get(resource_id)
                if owner is not None and owner != trial_id:
                    raise ReducerError(f"resource already locked: {resource_id}")
                next_state["resourceLocks"][resource_id] = trial_id
            return next_state

        if kind == "RawSampleIngested":
            sample_id = str(payload["sampleId"])
            if sample_id in next_state["samples"]:
                raise ReducerError(f"duplicate raw sample: {sample_id}")
            next_state["samples"][sample_id] = payload
            return next_state

        if kind == "TrialEvaluated":
            trial = self._trial(next_state, str(payload["trialId"]))
            trial["evaluation"] = {
                "executionValidity": payload["executionValidity"],
                "holdComplete": bool(payload.get("holdComplete", False)),
                "mandatoryPredicateIds": list(
                    payload.get("mandatoryPredicateIds", [])
                ),
                "measurementSufficiency": payload["measurementSufficiency"],
                "predicateVerdicts": dict(payload.get("predicateVerdicts", {})),
                "sameCandidate": bool(payload.get("sameCandidate", False)),
                "traceRefs": list(payload.get("traceRefs", [])),
                "validityRegionStable": bool(
                    payload.get("validityRegionStable", False)
                ),
            }
            if payload.get("stopReason") is not None:
                trial["stopReason"] = payload["stopReason"]
            return next_state

        if kind == "EvidenceCellRegistered":
            cell_id = str(payload["cellId"])
            if cell_id in next_state["evidenceCells"]:
                raise ReducerError(f"evidence cell already exists: {cell_id}")
            next_state["evidenceCells"][cell_id] = {
                **payload,
                "pendingEvidenceUpdates": [],
                "pendingStatus": payload["status"],
            }
            return next_state

        if kind == "CompatibilityRecorded":
            next_state["compatibility"][str(payload["recordId"])] = payload
            return next_state

        if kind == "EvidenceContributionRecorded":
            cell_id = str(payload["cellId"])
            try:
                cell = next_state["evidenceCells"][cell_id]
            except KeyError as exc:
                raise ReducerError(f"unknown evidence cell: {cell_id}") from exc
            if payload["contribution"].get("reusedFromEpoch") is not None:
                cell["contributions"] = [
                    *cell.get("contributions", []),
                    payload["contribution"],
                ]
                cell["status"] = payload["resultingStatus"]
                cell["pendingStatus"] = payload["resultingStatus"]
            else:
                cell["pendingEvidenceUpdates"] = [
                    *cell.get("pendingEvidenceUpdates", []),
                    {
                        "contribution": payload["contribution"],
                        "resultingStatus": payload["resultingStatus"],
                    },
                ]
                cell["pendingStatus"] = payload["resultingStatus"]
            return next_state

        if kind == "ExhaustionCertified":
            key = f"{payload.get('caseId', '')}:{payload['vectorRef']}"
            next_state["exhaustionCertificates"][key] = payload
            return next_state

        if kind == "TargetVectorReleased":
            case_id = str(payload["caseId"])
            try:
                next_state["cases"][case_id]["activeVector"] = payload["vectorRef"]
            except KeyError as exc:
                raise ReducerError(f"unknown case: {case_id}") from exc
            for cell_id, status in payload.get("unsealedCells", {}).items():
                next_state["evidenceCells"][cell_id]["status"] = status
                next_state["evidenceCells"][cell_id]["sealedUntilVectorRef"] = None
            return next_state

        if kind == "CaseTerminated":
            case_id = str(payload["caseId"])
            try:
                case = next_state["cases"][case_id]
            except KeyError as exc:
                raise ReducerError(f"unknown case: {case_id}") from exc
            if case.get("terminal") not in {None, payload["termination"]}:
                raise ReducerError("case already terminated differently")
            case["terminal"] = payload["termination"]
            return next_state

        if kind == "TrialSettled":
            trial_id = str(payload["trialId"])
            trial = self._trial(next_state, trial_id)
            if trial["settlementEventId"] is not None:
                raise ReducerError(f"trial already settled: {trial_id}")
            if trial["state"] != TrialState.SETTLEMENT.value:
                raise ReducerError("trial settlement requires SETTLEMENT state")
            outcome = TrialOutcome(str(payload["outcome"]))
            trial["outcome"] = outcome.value
            trial["settlementEventId"] = envelope.event_id
            trial["state"] = (
                TrialState.SETTLED_SUCCESS.value
                if outcome is TrialOutcome.SUCCESS
                else TrialState.SETTLED_NON_SUCCESS.value
            )
            for charge in payload.get("harmCharges", []):
                next_state["harmLedger"].append(
                    {
                        **charge,
                        "eventId": envelope.event_id,
                        "timestamp": envelope.timestamp,
                        "trialId": trial_id,
                    }
                )
            next_state["pendingHarmCharges"].pop(trial_id, None)
            for movement in payload.get("harmMovements", []):
                next_state["harmLedger"].append(
                    {
                        **movement,
                        "eventId": envelope.event_id,
                        "timestamp": envelope.timestamp,
                        "trialId": trial_id,
                    }
                )
            for update in payload.get("evidenceUpdates", []):
                cell_id = str(update["cellId"])
                try:
                    cell = next_state["evidenceCells"][cell_id]
                except KeyError as exc:
                    raise ReducerError(
                        f"settlement references unknown evidence cell: {cell_id}"
                    ) from exc
                existing_ids = {
                    item.get("contributionId")
                    for item in cell.get("contributions", [])
                }
                cell["contributions"] = [
                    *cell.get("contributions", []),
                    *[
                        item
                        for item in update.get("contributions", [])
                        if item.get("contributionId") not in existing_ids
                    ],
                ]
                cell["status"] = update["resultingStatus"]
                settled_ids = {
                    item.get("contributionId")
                    for item in update.get("contributions", [])
                }
                cell["pendingEvidenceUpdates"] = [
                    item
                    for item in cell.get("pendingEvidenceUpdates", [])
                    if item.get("contribution", {}).get("contributionId")
                    not in settled_ids
                ]
                remaining = cell["pendingEvidenceUpdates"]
                cell["pendingStatus"] = (
                    remaining[-1]["resultingStatus"]
                    if remaining
                    else cell["status"]
                )
            for resource_id, owner in tuple(next_state["resourceLocks"].items()):
                if owner == trial_id:
                    del next_state["resourceLocks"][resource_id]
            candidate_id = trial.get("candidateId")
            trial_epoch = str(trial["epochId"])
            epoch_availability = next_state["candidateAvailabilityByEpoch"].setdefault(
                trial_epoch, {}
            )
            if candidate_id in epoch_availability or (
                    candidate_id is not None
                    and (next_state["catalogs"].get(trial_epoch) or {}).get("domain")):
                # 핸드오프 2026-09-18 §5.1: 불완전한 관측을 '이미 시도한 설정' 으로 쳐서
                # 필요한 재측정을 막지 않는다.  관측이 불충분했거나(INDETERMINATE·INVALID)
                # 적용 전에 거절돼 아무것도 바뀌지 않은(EXEC_ERROR) 시행은 그 설정에 대해
                # 아무것도 알려 주지 않았으므로 후보를 되돌린다.  나머지(성공·실패·운영자
                # 중단·안전 정지·복구 실패)는 종전대로 소진한다 -- 안전 정지는 실제 해를
                # 끼쳤을 수 있다.  재시도는 판당 dispatch 상한과 B 가 묶는다.
                epoch_availability[candidate_id] = (
                    CandidateAvailability.AVAILABLE.value
                    if outcome in self.nothing_learned_outcomes
                    else CandidateAvailability.CONSUMED.value
                )
                if next_state["activeEpoch"] == trial_epoch:
                    next_state["candidateAvailability"] = epoch_availability
            case = next_state["cases"].get(trial.get("caseId"))
            if case is not None and outcome is TrialOutcome.SUCCESS:
                case["deployedSuccess"] = True
            transaction_id = str(payload.get("transactionId", trial["transactionId"]))
            transaction = next_state["transactions"].setdefault(
                transaction_id, {"trialId": trial_id}
            )
            transaction["resolved"] = True
            transaction["resolution"] = "SETTLED"
            return next_state

        if kind in {"RecoveryStarted", "RecoveryObserved"}:
            transaction_id = str(payload["transactionId"])
            transaction = next_state["transactions"].setdefault(
                transaction_id, {"resolved": False, "resolution": None}
            )
            transaction["recovery"] = payload
            return next_state

        if kind == "TransactionResolved":
            transaction_id = str(payload["transactionId"])
            transaction = next_state["transactions"].setdefault(transaction_id, {})
            transaction["resolved"] = True
            transaction["resolution"] = payload["resolution"]
            trial_id = payload.get("trialId") or transaction.get("trialId")
            if trial_id in next_state["trials"]:
                next_state["trials"][trial_id]["recoveryVerified"] = payload[
                    "resolution"
                ] in {"ABORTED", "FINALIZED", "ROLLED_BACK"}
            return next_state

        raise ReducerError(f"unrecognised Kernel event kind: {kind}")

def replay(reducer: Reducer, envelopes: Iterable[EventEnvelope]) -> Mapping[str, Any]:
    """Fold *envelopes* through *reducer* from its initial state.

    Signature frozen; body owned by lane **KERN**.

    The order of *envelopes* is the store's accepted order and is authoritative:
    replay does not re-sort, because the accepted order is what the original
    run actually observed and re-sorting would produce a state that never
    existed.
    """
    if reducer is None or envelopes is None:
        raise NotImplementedError(
            "owned by lane KERN; see docs/architecture/SEAMS-GATE2.md"
        )
    state = reducer.initial_state()
    # No intermediate state escapes this fold, so the built-in reducer can
    # own one accumulator rather than copying the frozen catalog per event.
    # Keep the protocol path for injected reducers (including subclasses).
    apply = reducer._apply_owned if type(reducer) is KernelReducer else reducer.apply
    for envelope in envelopes:
        state = apply(state, envelope)
    return state


def terminal_state_hash(state: Mapping[str, Any], *, reducer_version: str) -> str:
    """The comparable digest of a reduced terminal state.

    Signature frozen; body owned by lane **KERN**.

    This is the value design section 15 compares -- "append-only event replay
    to identical final state hash" -- and the value the Gate 1 reachability
    test asserts one of.

    Two requirements pull in opposite directions and both must be met:

    * it must cover everything that constitutes the outcome: terminal case
      state, per-trial outcomes, evidence cell statuses, ledger balances,
      candidate availability, exhaustion certificates;
    * it must exclude everything that legitimately differs between two replays
      of the same stream: wall-clock replay times, process ids, host names,
      absolute file paths, iteration-order artefacts.

    *reducer_version* is mixed into the digest rather than merely checked, so
    a state reduced by a different version can never collide with this one.
    Comparing hashes across reducer versions is meaningless, and a mixed-in
    version makes that structurally visible instead of a caller's
    responsibility.

    Uses :func:`assurance.core.addressing.content_hash`, so the canonical form
    is the same RFC 8785 one used everywhere else in the system.
    """
    if state is None or reducer_version is None:
        raise NotImplementedError(
            "owned by lane KERN; see docs/architecture/SEAMS-GATE2.md"
        )
    if not isinstance(reducer_version, str) or not reducer_version.strip():
        raise ReducerError("reducer_version must be a non-empty string")
    return content_hash({"reducerVersion": reducer_version, "state": dict(state)})
