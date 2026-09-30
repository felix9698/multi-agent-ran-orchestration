"""Phase B Operator Console - LIVE console evidence as a Replay source.

Owner: track **T3**.  Authority: ``docs/phase-b-gui/replay-sources.1.0.0.json``.

A sitting at the Cockpit's LIVE console writes two files per case into
``docs/integration/evidence/``: ``LIVECONSOLE-<objective>-<stamp>-events.jsonl``,
the Kernel's append-only event stream, and ``-run.json``, what the operator was
shown when it ended.  Until now the console could not read them back, so the
only OTA evidence the project has was citable from a shell and not openable in
the tool that produced it (acceptance matrix ``GAP-4``).

**The reduced state is the source of truth, not ``run.json``.**  This adapter
folds the event stream through the *same* :func:`~assurance.kernel.reducer.replay`
the Kernel ran on the radio, and everything it shows - the seven axes, the trial
state, the settlement, the harm ledger, the counter samples - is read off the
resulting state.  ``run.json`` is used for exactly two things: the utterance and
objective the operator typed, which are not in the stream, and the recorded
``terminalStateHash``, which is *checked* against the re-derived one.  A run
whose stream does not reproduce its recorded hash is **refused**, because a
console that renders a run it cannot reproduce is a console that will one day
render a run that never happened.

**Mode is REPLAY and cannot be anything else.**  ``run.json`` records
``sessionMode: "LIVE"`` - that is the mode of the *recording*, and it travels
into the summary as ``sourceMode``.  The session reading it back is a Replay:
nothing is being observed, and the badge, the export banner and the figure
watermark all follow the session mode.  There is no argument here that changes
it (``session-store.1.0.0`` refuses a LIVE run not evidenced by a live
transport, so the refusal is structural rather than a convention).

**What is not claimed.**  The stream carries the assurance counters the
objective's contracts bound and the Kernel's own decisions.  It carries none of
the nineteen KPI registry metrics - no throughput series, no RSRP, no MCS - so
they resolve ``UNSUPPORTED`` for this source rather than ``UNKNOWN``, and the
run records why.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Final, List, Mapping, Optional, Sequence, Tuple

from runstore.records import telemetry_sample, timeline_event
from runstore.records import cycle_trace, episode_trace

# ``assurance`` is imported lazily, inside the two methods that reduce a stream.
# At module scope it would pull ``oran.contract`` in, and this package must stay
# importable with ``tests/`` ahead of the repository root on ``sys.path`` -- the
# shadowing probe ``tests/gui/test_analysis_and_widgets.py`` runs.  The other
# adapters keep their heavy imports inside their loaders for the same reason.

#: Adapter id, matching ``replay-sources.1.0.0.json``.
SOURCE_ID: Final[str] = "liveconsole-run"

RUN_SUFFIX: Final[str] = "-run.json"
EVENTS_SUFFIX: Final[str] = "-events.jsonl"
SCOPE_ARCHIVE_SUFFIX: Final[str] = "-scope-archive.json"
RUN_PREFIX: Final[str] = "LIVECONSOLE-"

#: ``run.json`` schema versions this adapter reads.  A document that names
#: another one is refused rather than read optimistically.
#:
#: ``1.1.0`` adds the ``supplementary`` half -- one record per SUPPLEMENTARY
#: participant, carrying the policies that adapter bound, the UE/cell/epoch its
#: configuration readback was entitled to match, every answer that readback
#: gave and the durable binding journal's last word.  ``1.0.0`` stays readable
#: and stays 1.0.0: a document with no ``supplementary`` key is a run that had
#: none *recorded*, which is not the same fact as a run that had none, and this
#: adapter says so rather than showing an empty list as an answer.
SUPPORTED_SCHEMA_VERSIONS: Final[Tuple[str, ...]] = (
    "liveconsole-run/1.0.0",
    "liveconsole-run/1.1.0",
)

#: The first version that records the supplementary half, for the message that
#: explains an absence.
SUPPLEMENTARY_SINCE: Final[str] = "liveconsole-run/1.1.0"

#: Versions that record it.  Enumerated rather than compared: version strings
#: do not order lexically past a two-digit component ("1.2.0" sorts above
#: "1.10.0"), and a reader that silently mis-ordered them would decide the
#: supplementary half was "not recorded" for a run that carried it.  A new
#: version is added here and to :data:`SUPPORTED_SCHEMA_VERSIONS`, deliberately
#: and in one place each.
VERSIONS_WITH_SUPPLEMENTARY: Final[Tuple[str, ...]] = (
    "liveconsole-run/1.1.0",
)

#: Which timeline lane each Kernel event kind belongs to.  Written out rather
#: than pattern-matched: a lane is what an operator scans, and guessing it from
#: a name would put a gateway refusal on the same track as a contract admission.
EVENT_LANES: Final[Dict[str, str]] = {
    # Session set-up: what was admitted and frozen before anything could act.
    "DeploymentAdmitted": "SESSION",
    "ContractAdmitted": "SESSION",
    "CatalogFrozen": "SESSION",
    "EpochFrozen": "SESSION",
    # The operator's case, and the advisory that proposed within it.
    "CaseOpened": "INTENT",
    "AdvisoryAccepted": "DECISION",
    # The assurance loop itself.
    "TrialOpened": "COORDINATOR",
    "TrialStateChanged": "COORDINATOR",
    "TrialPlanStaged": "COORDINATOR",
    "TrialCommitReadinessRecorded": "COORDINATOR",
    "TrialEvaluated": "COORDINATOR",
    "TrialSettled": "COORDINATOR",
    "HarmReserved": "COORDINATOR",
    "HarmCharged": "COORDINATOR",
    "HarmReturned": "COORDINATOR",
    "EvidenceCellRegistered": "COORDINATOR",
    "EvidenceContributionRecorded": "COORDINATOR",
    "CaseTerminated": "COORDINATOR",
    "RecoveryStarted": "WARNING",
    "RecoveryObserved": "WARNING",
    # Everything that crossed the write boundary.
    "TokenIssued": "R1",
    "GatewayResultRecorded": "R1",
    "TransactionResolved": "R1",
    # What came back up from the lower layer.
    "RawSampleIngested": "LOWER_FEEDBACK",
}

#: The lane an unknown kind lands in.  A future Kernel event must still appear -
#: dropping it would make the timeline quietly incomplete - and the assurance
#: loop is the honest default for an event the Kernel emitted about itself.
DEFAULT_LANE: Final[str] = "COORDINATOR"

#: Gateway outcomes that leave the write unresolved.  They are the reason a
#: timeline entry is a warning rather than an informational line.
UNRESOLVED_OUTCOMES: Final[Tuple[str, ...]] = (
    "UNKNOWN", "PARTIAL_APPLY", "REJECTED_FENCE", "REJECTED_LEASE_EXPIRED",
    "REJECTED_IDEMPOTENCY_COLLISION", "REJECTED", "ERROR",
)

#: Terminal trial state to run disposition.  Only a settled success is
#: ``COMPLETED``: a run directory must never summarize an incident lockdown as
#: a clean run, and the recorded state is the only thing that decides.
DISPOSITION_MAP: Final[Dict[str, str]] = {
    "SETTLED_SUCCESS": "COMPLETED",
    "SETTLED_NON_SUCCESS": "ABORTED",
    "INCIDENT_LOCKDOWN": "FAILED",
    "SAFETY_STOPPED": "ABORTED",
}


def _reducer_as_recorded(envelopes):
    """그 stream 이 선언한 판본의 reducer.  선언이 없거나 같으면 현재 것을 쓴다."""
    from assurance.kernel.reducer import KernelReducer

    version = None
    for item in envelopes:
        payload = getattr(item, 'payload', None)
        if isinstance(payload, dict) and payload.get('reducerVersion'):
            version = str(payload['reducerVersion'])
            break
    if not version or version == KernelReducer.reducer_version:
        return KernelReducer()

    # **이름표만 바꾸면 기록된 대로 접는 것이 아니다.**  1.1.0 과 1.2.0 을 가르는
    # 유일한 규칙은 "아무것도 알려 주지 않은 결과가 후보를 되돌리는가" 이므로, 그
    # 이전 판본으로 접을 때는 그 규칙도 함께 되돌린다.  2026-09-23 실측: 이것 없이는
    # `EXEC_ERROR` 로 정착한 stream 이 재생에 실패했다.
    class _AsRecorded(KernelReducer):
        reducer_version = version
        if version == "assurance-kernel-reducer/1.1.0":
            nothing_learned_outcomes = frozenset()

    return _AsRecorded()


class LiveConsoleRunError(Exception):
    """A recorded LIVE console run that cannot be loaded, with its issue kind."""

    def __init__(self, detail: str, *, kind: str = "SCHEMA_MISMATCH") -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


# --------------------------------------------------------------------------- #
# Source
# --------------------------------------------------------------------------- #


class LiveConsoleRunSource:
    """One ``LIVECONSOLE-*`` pair, opened read-only and replayed on demand.

    Construction reads and validates; nothing is written anywhere near the
    evidence directory, which is a repository artifact and an input.
    """

    def __init__(self, root, run_id: Optional[str] = None) -> None:
        self.root = Path(root)
        if not self.root.is_dir():
            raise LiveConsoleRunError(f"{self.root} is not a directory",
                                      kind="CONNECTION_LOST")
        available = list_sessions(self.root)
        if not available:
            raise LiveConsoleRunError(
                f"{self.root} holds no {RUN_PREFIX}*{RUN_SUFFIX} run record",
                kind="CONNECTION_LOST")
        chosen = run_id or available[0]
        if chosen not in available:
            raise LiveConsoleRunError(
                f"{chosen} is not one of the runs under {self.root}: "
                f"{', '.join(available)}", kind="CONNECTION_LOST")
        self.run_id = chosen
        self.run_path = self.root / f"{chosen}{RUN_SUFFIX}"
        self.events_path = self.root / f"{chosen}{EVENTS_SUFFIX}"
        if not self.events_path.is_file():
            raise LiveConsoleRunError(
                f"{chosen} has a run record but no event stream; the stream is "
                "what the axes are re-derived from, and a run record on its own "
                "is a claim rather than evidence", kind="PARTIAL_DATA")
        scope = self.root / f"{chosen}{SCOPE_ARCHIVE_SUFFIX}"
        self.scope_archive_path = scope if scope.is_file() else None

        self.document = _read_json(self.run_path)
        self.schema_version = str(self.document.get("schemaVersion", ""))
        if self.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise LiveConsoleRunError(
                f"{chosen} declares schemaVersion {self.schema_version!r}, and "
                f"this adapter reads {', '.join(SUPPORTED_SCHEMA_VERSIONS)}")
        self.records = _read_jsonl(self.events_path)
        if not self.records:
            raise LiveConsoleRunError(
                f"{chosen} has an empty event stream", kind="DATA_TRUNCATED")

        self._state: Optional[Mapping[str, Any]] = None
        self._reducer_version: str = ""

    # -- identity ---------------------------------------------------------- #

    @property
    def case_id(self) -> str:
        return str(self.document.get("caseId") or self.run_id)

    @property
    def objective(self) -> str:
        return str(self.document.get("objective") or "")

    @property
    def utterance(self) -> str:
        """The sentence the operator typed.  Not in the stream, so read here."""
        return str(self.document.get("utterance") or "")

    @property
    def source_mode(self) -> str:
        """The mode of the RECORDING.  Never this session's mode."""
        return str(self.document.get("sessionMode") or "LIVE")

    @property
    def records_supplementary(self) -> bool:
        """Whether this document's version records the supplementary half."""
        return self.schema_version in VERSIONS_WITH_SUPPLEMENTARY

    @property
    def supplementary(self) -> List[Dict[str, Any]]:
        """One record per SUPPLEMENTARY participant, as the file kept it.

        Empty on a ``1.0.0`` document *and* on a ``1.1.0`` run that composed no
        supplementary action.  The two are different facts and
        :attr:`records_supplementary` is what tells them apart; the loader
        records the first as a data issue rather than letting an empty list
        stand as an answer.
        """
        recorded = self.document.get("supplementary")
        if not isinstance(recorded, list):
            return []
        return [dict(entry) for entry in recorded if isinstance(entry, Mapping)]

    @property
    def recorded_terminal_state_hash(self) -> str:
        return str((self.document.get("contract") or {}).get(
            "terminalStateHash") or "")

    @staticmethod
    def digest(path: Path) -> str:
        """The digest of the bytes on disk, so the raw copy is checkable."""
        return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()

    # -- replay ------------------------------------------------------------ #

    def envelopes(self) -> List[EventEnvelope]:
        """The recorded lines back into the envelopes the Kernel appended.

        Field by field with no defaulting: a line missing something the Kernel
        wrote is a corrupt log, and substituting a value would replay a stream
        that was never accepted.
        """
        from assurance.core.components import ComponentId
        from assurance.core.envelopes import EventEnvelope

        try:
            return [
                EventEnvelope(
                    schema_version=record["schemaVersion"],
                    object_id=record["objectId"],
                    event_id=record["eventId"],
                    timestamp=record["timestamp"],
                    content_hash=record["contentHash"],
                    sequence=record["sequence"],
                    expiry=record.get("expiry"),
                    idempotency_key=record["idempotencyKey"],
                    source_component=ComponentId(record["sourceComponent"]),
                    payload=record["payload"],
                    event_kind=record["eventKind"],
                )
                for record in self.records
            ]
        except (KeyError, ValueError) as exc:
            raise LiveConsoleRunError(
                f"{self.run_id}: the event stream is not a Kernel event log "
                f"({type(exc).__name__}: {exc})") from exc

    def state(self) -> Mapping[str, Any]:
        """The reduced state, replayed once and cached.

        Three refusals, in the order a corruption would be met: an envelope
        whose content hash does not cover its own payload, a stream the reducer
        will not accept, and a stream that reduces to a state other than the one
        the run recorded.
        """
        from assurance.kernel.reducer import (
            KernelReducer, ReducerError, replay, terminal_state_hash)

        if self._state is not None:
            return self._state
        envelopes = self.envelopes()
        unsealed = [item.event_id for item in envelopes
                    if not item.verify_content_hash()]
        if unsealed:
            raise LiveConsoleRunError(
                f"{self.run_id}: {len(unsealed)} envelope(s) do not carry the "
                f"digest of their own payload, first {unsealed[0]}")
        # 2026-09-22: 여기서 **현재** reducer 를 쓰면, 판본을 올리는 순간 보관된 모든
        # stream 이 `epoch reducer version does not match reducer` 로 거절된다 --
        # `_apply_owned` 와 `kernel.py` 가 판본을 동등 비교하기 때문이다(관문이 제 일을
        # 한 것이지 결함이 아니다).  재생이 확인하는 것은 "**기록된 대로 접히는가**"
        # 이므로, 그 stream 이 자기 EpochFrozen 에 적어 둔 판본으로 접어야 한다.
        # 실측(6개 실기 stream): 이 방식으로 terminalStateHash 가 바이트 단위로
        # 재현된다.  라이브 경로는 그대로 현재 판본으로 **새** case 를 연다.
        reducer = _reducer_as_recorded(envelopes)
        try:
            state = replay(reducer, envelopes)
        except ReducerError as exc:
            raise LiveConsoleRunError(
                f"{self.run_id}: the reducer refused the recorded stream "
                f"({exc})") from exc
        recomputed = terminal_state_hash(
            state, reducer_version=reducer.reducer_version)
        recorded = self.recorded_terminal_state_hash
        if recorded and recomputed != recorded:
            raise LiveConsoleRunError(
                f"{self.run_id}: the stream reduces to {recomputed} and the run "
                f"recorded {recorded}; a run that cannot be reproduced is not "
                "replayed")
        self._state = state
        self._reducer_version = reducer.reducer_version
        return state

    @property
    def reducer_version(self) -> str:
        self.state()
        return self._reducer_version

    def terminal_state_hash(self) -> str:
        from assurance.kernel.reducer import terminal_state_hash

        return terminal_state_hash(self.state(),
                                   reducer_version=self.reducer_version)


# --------------------------------------------------------------------------- #
# Projections off the reduced state
# --------------------------------------------------------------------------- #


def latest_trial(state: Mapping[str, Any]) -> Tuple[str, Mapping[str, Any]]:
    """The trial the case ended on, or ``("", {})`` when none was opened."""
    trials = state.get("trials") or {}
    if not trials:
        return "", {}
    trial_id = sorted(trials)[-1]
    return trial_id, trials[trial_id]


def derived_axes(state: Mapping[str, Any]) -> Dict[str, Any]:
    """The decision axes, from the reduced state and nothing else.

    The same projection ``gui/operator/sources/kernel_live.py`` makes for a live
    case, so a replayed run and a live one are read the same way.  Kept as
    separate axes: "nobody could measure it" is not "the objective failed", and
    a console that collapsed them would print the second when the first is true.
    """
    _trial_id, trial = latest_trial(state)
    if not trial:
        return {}
    evaluation = trial.get("evaluation") or {}
    return {
        "executionValidity": evaluation.get("executionValidity", "NOT_EVALUATED"),
        "measurementSufficiency": evaluation.get(
            "measurementSufficiency", "NOT_EVALUATED"),
        "predicateVerdicts": dict(evaluation.get("predicateVerdicts") or {}),
        "trialOutcome": trial.get("outcome", "NOT_SETTLED"),
        "holdComplete": evaluation.get("holdComplete"),
    }


def gateway_operations(records: Sequence[Mapping[str, Any]]
                       ) -> List[List[str]]:
    """``[[tokenKind, outcome], ...]`` in the order the gateway answered.

    Taken from the stream rather than the reduced state on purpose: the state
    keeps a transaction's *latest* token and result, and the sequence a recovery
    took is the thing an operator is trying to read.  Each result names its own
    ``tokenKind``, so the pairing is read off the record rather than inferred by
    matching results to issued tokens in order -- an inference that a recovery
    issuing a token out of band would silently break.

    This is a *superset* of what the live console's own run record carries: the
    console records the operations its runtime report saw, and a recovery driven
    after the incident (the ``CONFIGURATION_REREAD`` on the three lockdowns) is
    in the Kernel's stream but not in that report.
    """
    operations: List[List[str]] = []
    for record in records:
        if record.get("eventKind") != "GatewayResultRecorded":
            continue
        payload = record.get("payload") or {}
        operations.append([str(payload.get("tokenKind") or ""),
                           str(payload.get("outcome") or "")])
    return operations


def build_settlement(state: Mapping[str, Any],
                     records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """What the operator was shown when the case ended, re-derived."""
    _trial_id, trial = latest_trial(state)
    cases = state.get("cases") or {}
    case = cases[sorted(cases)[-1]] if cases else {}
    return {
        "trialState": trial.get("state"),
        "outcome": trial.get("outcome"),
        "stopReason": trial.get("stopReason"),
        "caseTermination": case.get("terminal"),
        "gatewayOperations": gateway_operations(records),
        "harmCharges": [
            f"{entry.get('movementKind')} {entry.get('harmContractRef')}: "
            f"{(entry.get('amount') or {}).get('value')} "
            f"{(entry.get('amount') or {}).get('unit')}"
            for entry in state.get("harmLedger") or ()
        ],
        "evidenceStatus": _evidence_status(state),
    }


def _evidence_status(state: Mapping[str, Any]) -> Optional[str]:
    cells = state.get("evidenceCells") or {}
    if not cells:
        return None
    statuses = sorted({str((cell or {}).get("status") or "")
                       for cell in cells.values()} - {""})
    return statuses[0] if len(statuses) == 1 else (
        "/".join(statuses) if statuses else None)


def build_telemetry(state: Mapping[str, Any], *, artifact_ref: str
                    ) -> List[Dict[str, Any]]:
    """The counter samples the Kernel accepted, as telemetry.

    Every field comes off the recorded sample - the observation time it
    declared, the cadence the contract bound, the scope it was attributed to -
    and none is re-stamped.  ``quality`` is derived from what the sample itself
    says about its clock and its gaps, which is the only thing that can say it.
    """
    samples = state.get("samples") or {}
    ordered = sorted(
        samples.values(),
        key=lambda item: (str(item.get("observedAt") or ""),
                          int(item.get("sequence") or 0),
                          str(item.get("sampleId") or "")))
    built: List[Dict[str, Any]] = []
    for seq, sample in enumerate(ordered):
        quantity = sample.get("value") or {}
        raw = quantity.get("value")
        value = float(raw) if isinstance(raw, (int, float)) else None
        scope = sample.get("scopeSnapshot") or {}
        scope_id = str(scope.get("ueId") or scope.get("cellId") or "")
        built.append(telemetry_sample(
            seq=seq,
            metric=str(sample.get("counterId") or ""),
            value=value,
            unit=str(quantity.get("unit") or ""),
            scope_level="UE" if scope.get("ueId") else "CELL",
            scope_id=scope_id,
            # The counters reached the Kernel over the R1 data path (E2SM-KPM
            # indications exposed by the gate), which is the boundary the
            # normalized record names.
            boundary="R1_DME",
            interface="R1",
            management_service="Data Management and Exposure",
            t_utc=sample.get("observedAt"),
            artifact_ref=artifact_ref,
            observation_id=str(sample.get("sampleId") or ""),
            sampling_interval_ms=(int(sample["cadenceMs"])
                                  if isinstance(sample.get("cadenceMs"), int)
                                  else None),
            quality=_sample_quality(sample, value)))
    return built


def _sample_quality(sample: Mapping[str, Any], value: Optional[float]) -> str:
    if value is None:
        return "MISSING"
    if sample.get("missingIntervals"):
        return "SUSPECT"
    health = str(sample.get("clockHealth") or "")
    if health == "SYNCHRONISED":
        return "OK"
    if health:
        return "SUSPECT"
    # The sample declared no clock health of its own; saying OK on its behalf
    # would be this adapter vouching for something it cannot see.
    return "AMBIGUOUS"


def build_supplementary(source: LiveConsoleRunSource) -> List[Dict[str, Any]]:
    """The supplementary half, as the run recorded it.

    Copied field for field rather than summarized: a cap's *own* adapter is the
    only thing that knows which policy it bound, which UE it was entitled to
    read the configuration back from, and whether the durable binding reached
    ``RESTORED`` -- the three facts that say a supplementary control was applied
    to the right UE and taken off again.  Nothing here is derived, because the
    event stream does not carry them: they are the adapter's, and the run
    document is where they were written down.
    """
    return [
        {
            "actionId": entry.get("actionId"),
            "adapterKey": entry.get("adapterKey"),
            "policyTypeId": entry.get("policyTypeId"),
            "axis": entry.get("axis"),
            "controlledUe": dict(entry.get("controlledUe") or {}),
            "candidateCaps": list(entry.get("candidateCaps") or ()),
            "liveBindings": list(entry.get("liveBindings") or ()),
            "bindingPolicyId": entry.get("bindingPolicyId"),
            "bindingState": entry.get("bindingState"),
            "readbackState": entry.get("readbackState"),
            "rollbackDetail": entry.get("rollbackDetail"),
            "expectedAttribution": dict(entry.get("expectedAttribution") or {}),
            "readbackLog": [dict(item)
                            for item in entry.get("readbackLog") or ()],
        }
        for entry in source.supplementary
    ]


def build_timeline(source: LiveConsoleRunSource) -> List[Dict[str, Any]]:
    """Every recorded envelope as one timeline entry, in recorded order."""
    case_id = source.case_id
    events: List[Dict[str, Any]] = []
    for seq, record in enumerate(source.records):
        kind = str(record.get("eventKind") or "")
        payload = record.get("payload") or {}
        severity, note = _severity(kind, payload)
        events.append(timeline_event(
            seq=seq,
            lane=EVENT_LANES.get(kind, DEFAULT_LANE),
            kind=kind,
            severity=severity,
            origin="OBSERVED",
            t_utc=record.get("timestamp"),
            title=note or kind,
            component=str(record.get("sourceComponent") or ""),
            ids={"episodeId": case_id,
                 "correlationId": str(record.get("objectId") or ""),
                 "evidenceId": str(record.get("eventId") or "")},
            detail={"objectId": record.get("objectId"),
                    "sequence": record.get("sequence"),
                    "idempotencyKey": record.get("idempotencyKey")},
            source_ref=f"raw/{SOURCE_ID}/{source.run_id}{EVENTS_SUFFIX}"))
    return events


def _severity(kind: str, payload: Mapping[str, Any]) -> Tuple[str, str]:
    """Severity and title, read off the payload rather than off the name."""
    if kind == "GatewayResultRecorded":
        outcome = str(payload.get("outcome") or "")
        if outcome in UNRESOLVED_OUTCOMES:
            return "WARNING", f"gateway answered {outcome}"
        return "INFO", f"gateway answered {outcome}"
    if kind == "TrialStateChanged":
        target = str(payload.get("to") or "")
        if target == "INCIDENT_LOCKDOWN":
            return "ERROR", "trial entered INCIDENT_LOCKDOWN"
        return "INFO", f"{payload.get('from')} -> {target}"
    if kind in ("RecoveryStarted", "RecoveryObserved"):
        return "WARNING", kind
    return "INFO", ""


def build_decision(source: LiveConsoleRunSource
                   ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """One episode for the case, one cycle for each trial it opened.

    ``terminalOutcome`` is ``None`` and therefore ``eq12State`` is ``None``.
    That is not a gap to be filled: the Kernel's six case terminations are not
    the old coordinator's three Eq.12 states, and mapping one onto the other
    would invent a verdict the run never reached.  The Kernel's own vocabulary
    rides alongside, in fields named for it.
    """
    state = source.state()
    axes = derived_axes(state)
    settlement = build_settlement(state, source.records)
    cases = state.get("cases") or {}
    case = cases[sorted(cases)[-1]] if cases else {}

    episode = episode_trace(
        episode_id=source.case_id,
        intent_text=source.utterance,
        terminal_outcome=None,
        objective=source.objective,
        epochId=state.get("activeEpoch"),
        trialState=settlement["trialState"],
        trialOutcome=settlement["outcome"],
        stopReason=settlement["stopReason"],
        caseTermination=settlement["caseTermination"],
        evidenceStatus=settlement["evidenceStatus"],
        executionValidity=axes.get("executionValidity"),
        measurementSufficiency=axes.get("measurementSufficiency"),
        predicateVerdicts=axes.get("predicateVerdicts") or {},
        holdComplete=axes.get("holdComplete"),
        gatewayOperations=settlement["gatewayOperations"],
        harmCharges=settlement["harmCharges"],
        maxTrials=case.get("maxTrials"),
        terminalStateHash=source.terminal_state_hash(),
        reducerVersion=source.reducer_version,
        eq12Note=(
            "the Kernel's terminations are not the coordinator's Eq.12 states; "
            "terminalOutcome is null because this source has none, and the "
            "trial state and case termination are recorded instead"),
    )

    cycles: List[Dict[str, Any]] = []
    for index, trial_id in enumerate(sorted((state.get("trials") or {}))):
        trial = (state.get("trials") or {})[trial_id]
        evaluation = trial.get("evaluation") or {}
        cycles.append(cycle_trace(
            episode_id=source.case_id,
            cycle_index=index,
            trialId=trial_id,
            trialState=trial.get("state"),
            trialOutcome=trial.get("outcome"),
            stopReason=trial.get("stopReason"),
            candidateId=trial.get("candidateId"),
            targetRef=trial.get("targetRef"),
            planAxes=list(trial.get("planAxes") or ()),
            planAdapter=trial.get("planAdapter"),
            executionValidity=evaluation.get("executionValidity"),
            measurementSufficiency=evaluation.get("measurementSufficiency"),
            predicateVerdicts=dict(evaluation.get("predicateVerdicts") or {}),
            holdComplete=evaluation.get("holdComplete"),
            observationStartedAt=trial.get("observationStartedAt"),
        ))
    return episode, cycles


# --------------------------------------------------------------------------- #
# Discovery and loading
# --------------------------------------------------------------------------- #


def list_sessions(path) -> List[str]:
    """Run ids under *path*, newest last, each with both halves present."""
    root = Path(path)
    if not root.is_dir():
        return []
    found = []
    for candidate in sorted(root.glob(f"{RUN_PREFIX}*{RUN_SUFFIX}")):
        run_id = candidate.name[:-len(RUN_SUFFIX)]
        if (root / f"{run_id}{EVENTS_SUFFIX}").is_file():
            found.append(run_id)
    return found


def load_liveconsole_run(source: "LiveConsoleRunSource | str | Path", runs_root,
                         *, run_id: Optional[str] = None,
                         capability_manifest: Optional[Mapping[str, Any]] = None
                         ) -> Any:
    """Normalize one recorded LIVE console case into a finalized run.

    Returns the store, finalized.  ``manifest.mode`` is ``REPLAY`` and its
    evidence is ``REPLAY_OF_RECORDED_SOURCE`` with the stream's digest - there
    is no argument, and no UI control, that makes this LIVE.
    """
    from gui.operator.store.metric_index import MetricIndexBuilder
    from gui.operator.store.session_store import SessionStore

    if not isinstance(source, LiveConsoleRunSource):
        source = LiveConsoleRunSource(source, run_id)
    # Replayed before the store exists: a stream that cannot be reproduced must
    # leave no run directory behind at all, not a failed one to be interpreted.
    state = source.state()
    events_digest = source.digest(source.events_path)
    supplementary = build_supplementary(source)

    store = SessionStore.create(
        runs_root, mode="REPLAY",
        mode_evidence={"basis": "REPLAY_OF_RECORDED_SOURCE",
                       "sourceRunIds": [source.run_id],
                       "sourceDigests": [events_digest]},
        profile_id=source.objective or None,
        config_snapshot={
            "source": {"adapter": SOURCE_ID, "runId": source.run_id,
                       "schemaVersion": source.schema_version,
                       "sourceMode": source.source_mode,
                       "reducerVersion": source.reducer_version},
            "caseId": source.case_id,
            "objective": source.objective,
            "utterance": source.utterance,
            "contract": source.document.get("contract"),
            "preflight": source.document.get("preflight"),
            # The supplementary half is a *result*, not configuration, and it
            # lives in the summary.  The config snapshot is credential-scrubbed
            # -- ``adapterKey`` is key-shaped and would come back
            # ``REDACTED_NEVER_WRITTEN`` -- so recording it here as well would
            # keep a mangled second copy of a record that is already whole.
            "capabilityManifest": capability_manifest,
        })
    try:
        store.add_source(
            source_id=SOURCE_ID, kind="R1_LIVE", boundary="R1_POLICY",
            path=str(source.root), digest=events_digest,
            schema_version=source.schema_version,
            adapter="gui/operator/sources/adapters/liveconsole_run.py",
            notes=("a recording of a LIVE console case, replayed; the writes it "
                   "records went out over R1 when it ran, and nothing is being "
                   "observed now"))
        store.set_contract(
            authority_version=(source.document.get("contract") or {}).get(
                "contractProfile"),
            bundle_digest=source.terminal_state_hash())

        # -- raw artifacts, byte-preserved --------------------------------- #
        store.copy_raw(SOURCE_ID, source.events_path,
                       f"{source.run_id}{EVENTS_SUFFIX}")
        store.copy_raw(SOURCE_ID, source.run_path,
                       f"{source.run_id}{RUN_SUFFIX}")
        if source.scope_archive_path is not None:
            store.copy_raw(SOURCE_ID, source.scope_archive_path,
                           f"{source.run_id}{SCOPE_ARCHIVE_SUFFIX}")

        artifact_ref = f"raw/{SOURCE_ID}/{source.run_id}{EVENTS_SUFFIX}"
        samples = build_telemetry(state, artifact_ref=artifact_ref)
        for sample in samples:
            store.append_telemetry(sample)
        for event in build_timeline(source):
            store.append_event(event)

        episode, cycles = build_decision(source)
        store.append_episode(episode)
        for cycle in cycles:
            store.append_cycle(cycle)

        # -- honest absences ------------------------------------------------ #
        store.record_issue(
            "UNSUPPORTED_FIELD",
            "a Kernel event stream carries the counters the objective's "
            "contracts bound and the decisions taken on them; it carries none "
            "of the KPI registry metrics, so throughput, RSRP, SINR, MCS and "
            "PRB utilization are UNSUPPORTED for this source rather than "
            "missing from a source that could have had them")
        store.record_issue(
            "UNSUPPORTED_FIELD",
            "the Kernel's six case terminations are not the coordinator's three "
            "Eq.12 states, so decision/episodes.jsonl carries eq12State null "
            "and records trialState and caseTermination instead")
        if not source.records_supplementary:
            # An absence, stated.  A run written before the supplementary half
            # was recorded may still have composed one; showing an empty list
            # as the answer would turn "not written down" into "did not
            # happen".
            store.record_issue(
                "UNSUPPORTED_FIELD",
                f"{source.schema_version} predates {SUPPLEMENTARY_SINCE} and "
                "records no supplementary half, so whether this case composed "
                "a SUPPLEMENTARY control -- and to which UE -- cannot be read "
                "back from it; the empty list below is an absence of record, "
                "not an absence of cap",
                gap_id="GAP-4")
        axes = derived_axes(state)
        settlement = build_settlement(state, source.records)
        for field, recorded, derived in _disagreements(source.document, axes,
                                                       settlement):
            # The run record and the stream disagree.  The stream wins, because
            # it is what the terminal hash covers, and the disagreement is shown
            # rather than resolved silently: a console that quietly preferred
            # one of two accounts is a console an operator cannot audit.
            store.record_issue(
                "SCHEMA_MISMATCH",
                f"run.json records {field} as {json.dumps(recorded, sort_keys=True)} "
                f"and the event stream re-derives {json.dumps(derived, sort_keys=True)}; "
                "the stream is authoritative because it is what the terminal "
                "state hash covers")

        builder = MetricIndexBuilder(source_id=SOURCE_ID,
                                     capability_manifest=capability_manifest)
        builder.observe_all(samples)
        store.write_metric_index(builder.build())

        trial_state = str(settlement.get("trialState") or "")
        summary = {
            "runId": store.run_id,
            "sourceRunId": source.run_id,
            "mode": store.mode,
            "sourceMode": source.source_mode,
            "sourceModeNote":
                "the mode of the RECORDING. This session is "
                f"{store.mode}: nothing here is being observed, and the badge, "
                "the export banner and the figure watermark all follow the "
                "session mode",
            "sourceClass": "ASSURANCE_KERNEL_EVENT_LOG",
            "sourceClassNote":
                "every value below is re-derived by folding the recorded event "
                "stream through the same Kernel reducer that produced it; "
                "run.json supplied only the operator's sentence and the "
                "terminal state hash this replay was checked against",
            "caseId": source.case_id,
            "objective": source.objective,
            "utterance": source.utterance,
            "axes": axes,
            "settlement": settlement,
            "supplementary": supplementary,
            "supplementaryRecorded": source.records_supplementary,
            "terminalStateHash": source.terminal_state_hash(),
            "reducerVersion": source.reducer_version,
            "recordedTerminalStateHash": source.recorded_terminal_state_hash,
            "outcomes": {trial_state: 1} if trial_state else {},
            "terminalOutcomes": (
                {str(settlement["outcome"]): 1} if settlement.get("outcome")
                else {}),
            "eventCount": len(source.records),
            "dataIssues": store.data_issues(),
        }
        store.write_summary(summary)

        disposition = DISPOSITION_MAP.get(trial_state, "FAILED")
        if disposition != "COMPLETED":
            ended = trial_state or "no terminal trial state"
            store.record_issue(
                "PARTIAL_DATA",
                f"the recorded case ended in {ended}; the run is recorded as "
                f"{disposition} and is never summarized as a success")
        store.finalize(disposition)
        return store
    except Exception:
        store.finalize("FAILED")
        raise


def _disagreements(document: Mapping[str, Any], axes: Mapping[str, Any],
                   settlement: Mapping[str, Any]
                   ) -> List[Tuple[str, Any, Any]]:
    """Where the run record and the re-derived projection differ.

    Only fields the run record actually carries are compared; a field it never
    wrote is an absence, not a disagreement.  The two known differences on the
    2026-09-04 lockdowns are legitimate and both are the stream carrying *more*:
    the evidence cell's status, which the runtime report left null, and the
    recovery ``CONFIGURATION_REREAD`` the report stopped short of.
    """
    found: List[Tuple[str, Any, Any]] = []
    recorded_axes = document.get("axes")
    if isinstance(recorded_axes, Mapping) and dict(recorded_axes) != dict(axes):
        found.append(("axes", dict(recorded_axes), dict(axes)))
    recorded = document.get("settlement")
    if isinstance(recorded, Mapping):
        for key, value in settlement.items():
            if key not in recorded:
                continue
            theirs = recorded[key]
            if key == "gatewayOperations":
                theirs = [list(item) for item in (theirs or ())]
            if theirs != value:
                found.append((f"settlement.{key}", theirs, value))
    return found


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LiveConsoleRunError(f"{path.name}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise LiveConsoleRunError(f"{path.name} does not hold a JSON object")
    return loaded


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise LiveConsoleRunError(f"{path.name}: {exc}",
                                  kind="CONNECTION_LOST") from exc
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise LiveConsoleRunError(
                f"{path.name}:{number} is not JSON ({exc}); a truncated event "
                "stream is refused rather than partly replayed",
                kind="DATA_TRUNCATED") from exc
        if not isinstance(record, dict):
            raise LiveConsoleRunError(
                f"{path.name}:{number} is not a JSON object")
        records.append(record)
    return records


__all__ = [
    "DEFAULT_LANE", "DISPOSITION_MAP", "EVENTS_SUFFIX", "EVENT_LANES",
    "LiveConsoleRunError", "LiveConsoleRunSource", "RUN_PREFIX", "RUN_SUFFIX",
    "SCOPE_ARCHIVE_SUFFIX", "SOURCE_ID", "SUPPORTED_SCHEMA_VERSIONS",
    "SUPPLEMENTARY_SINCE", "VERSIONS_WITH_SUPPLEMENTARY",
    "build_decision", "build_settlement", "build_supplementary",
    "build_telemetry", "build_timeline",
    "derived_axes", "gateway_operations", "latest_trial", "list_sessions",
    "load_liveconsole_run",
]
