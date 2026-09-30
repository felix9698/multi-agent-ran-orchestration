"""Cockpit projections: the Kernel's own state, read and never written.

Task section 9 asks the console for three surfaces the Gate 3 Contract Studio
did not have -- the always-visible header, ``Trial & Safety`` and the
``Evidence Ledger`` -- and every one of them shows facts that belong to the
Assurance Kernel.  This module is the single place those facts are turned into
frozen view models, and it is deliberately the *only* place: three renderers
each reading the reduced state their own way would be three chances to
disagree about what a trial outcome was.

Everything here is a pure function of ``kernel.reduced_state()`` plus the
append-only event stream.  Three consequences, and all three are the point:

**Nothing can be written back.**  The functions take mappings and return frozen
dataclasses.  There is no setter, no callback and no path from a widget to a
verdict, an evidence closure, a harm charge or a rollback result -- task
section 9.9 as a missing capability rather than as a disabled button.

**Nothing is inferred.**  A value the reduced state does not carry becomes
``Unknown`` or ``Unsupported`` with the reason (task sections 9.11, 9.13), and
a value that was computed rather than observed is marked ``DERIVED`` through
:mod:`gui.operator.data_class` (task section 9.7).  The harm balance is the
clearest case: it is arithmetic over the ledger, so it is DERIVED, and it says
so beside the number.

**Interactive and Batch read the same projection.**  A batch run drives the
same Kernel through the same vertical boundary; :func:`project` is handed its
reduced state exactly as an interactive run's is, which is the readable half of
task section 9.3.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from assurance.core.axes import (
    CandidateAvailability,
    EvidenceCellStatus,
    OPEN_EVIDENCE_CELL_STATUSES,
    TrialOutcome,
)
from assurance.core.states import TERMINAL_TRIAL_STATES

from .. import data_class as dc
from .. import status as st

# --------------------------------------------------------------------------- #
# Reading the append-only stream
# --------------------------------------------------------------------------- #

#: Reason reported when the Kernel handed over no readable event stream.  The
#: Evidence Ledger then shows nothing *and says why*; an empty list rendered as
#: a successful read would be exactly the "empty session that looks like a
#: successful replay" task section 9.12 forbids.
NO_EVENT_STREAM: str = "NO_READABLE_EVENT_STREAM"


def kernel_events(kernel: Any) -> Tuple[Any, ...]:
    """The Kernel's append-only stream, oldest first, or an empty tuple.

    The store is read through whatever accessor the Kernel exposes and is
    never mutated.  A Kernel that offers no readable stream yields ``()`` and
    the caller states :data:`NO_EVENT_STREAM`; guessing at a stream would be
    inventing a ledger.
    """
    store = getattr(kernel, "_event_store", None)
    iterate = getattr(store, "iterate", None)
    if iterate is None:
        return ()
    try:
        return tuple(iterate())
    except Exception:                                      # pragma: no cover
        return ()


# --------------------------------------------------------------------------- #
# Header (task section 9's seven always-visible items)
# --------------------------------------------------------------------------- #

#: The seven header items, in the order task section 9 lists them.  Declared as
#: data so a test can assert the header carries all seven rather than six.
COCKPIT_HEADER_KEYS: Tuple[str, ...] = (
    "case_trial",
    "kernel_state",
    "target_vector",
    "harm",
    "measurement",
    "mode",
    "emergency_stop",
)

COCKPIT_HEADER_LABELS: Mapping[str, str] = {
    "case_trial": "Case / Trial",
    "kernel_state": "Kernel state",
    "target_vector": "Target Vector",
    "harm": "Harm reserve / charge",
    "measurement": "Measurement",
    "mode": "Mode",
    "emergency_stop": "Emergency Stop",
}


@dataclass(frozen=True)
class HeaderItem:
    """One header cell: a value, how it got there, and how healthy it is."""

    key: str
    label: str
    value: str
    badge: dc.DataClassBadge
    status: str = st.UNKNOWN
    detail: Optional[str] = None

    def as_text(self) -> str:
        """Glyph-led, data-class-marked single line."""
        text = (f"{st.resolve(self.status).glyph} {self.badge.glyph} "
                f"{self.label}: {self.value}")
        return f"{text} ({self.detail})" if self.detail else text


@dataclass(frozen=True)
class HarmBalanceView:
    """One harm contract's reserve arithmetic, marked as arithmetic.

    ``limit_respected`` is a comparison of the ledger against the epoch-frozen
    usable reserve, not a judgement: the Kernel refuses a charge that would
    exceed the reserve, so a False here means the console is looking at a
    ledger the Kernel would not have written and the operator should know.
    """

    harm_contract_ref: str
    unit: str = ""
    usable: Optional[float] = None
    reserved: float = 0.0
    charged: float = 0.0
    returned: float = 0.0
    charged_for_missing_interval: float = 0.0
    status: str = st.UNKNOWN
    reason: Optional[str] = None

    @property
    def remaining(self) -> Optional[float]:
        if self.usable is None:
            return None
        return self.usable - self.charged

    @property
    def limit_respected(self) -> Optional[bool]:
        if self.usable is None:
            return None
        return self.charged <= self.usable

    def as_text(self) -> str:
        if self.usable is None:
            return (f"{self.harm_contract_ref}: {st.PRE_MEASUREMENT} "
                    f"({self.reason or 'no usable reserve in this epoch'})")
        return (f"{self.harm_contract_ref}: charged {self.charged:g} / "
                f"reserve {self.usable:g} {self.unit}".rstrip())


@dataclass(frozen=True)
class MeasurementFreshnessView:
    """What the Kernel has actually been fed, and when.

    ``clock_health`` is carried verbatim from the collector's own vocabulary
    rather than folded into the freshness verdict: an unsynchronised clock and
    a stale sample are different faults with different fixes.
    """

    sample_count: int = 0
    last_observed_at: Optional[str] = None
    last_counter_id: Optional[str] = None
    cadence_ms: Optional[int] = None
    freshness_bound_ms: Optional[int] = None
    clock_health: str = "UNKNOWN"
    missing_intervals: int = 0
    status: str = st.UNKNOWN
    reason: Optional[str] = None


@dataclass(frozen=True)
class CockpitHeaderView:
    """The seven header facts, already resolved.  Frozen; never edited."""

    mode: str = "DISCONNECTED"
    case_id: Optional[str] = None
    case_terminal: Optional[str] = None
    case_paused: Optional[bool] = None
    trial_id: Optional[str] = None
    kernel_state: Optional[str] = None
    trial_outcome: Optional[str] = None
    active_vector: Optional[str] = None
    harm: Tuple[HarmBalanceView, ...] = ()
    measurement: MeasurementFreshnessView = field(
        default_factory=MeasurementFreshnessView)
    emergency_stop_requested: bool = False
    trials_used: Optional[int] = None
    max_trials: Optional[int] = None
    deadline_at: Optional[str] = None
    #: Why the projection had nothing to read, when it had nothing to read.
    unavailable_reason: Optional[str] = None

    @property
    def is_live(self) -> bool:
        return self.mode == "LIVE"

    @property
    def emergency_stop_available(self) -> bool:
        """Whether there is a trial the stop could act on.

        Derived rather than stored: a flag beside ``kernel_state`` is a second
        opinion about the same fact, and the two would eventually disagree --
        with the header offering a stop for a settled trial, or hiding one for
        a running one.
        """
        if not self.trial_id or not self.kernel_state:
            return False
        return self.kernel_state not in {state.value
                                         for state in TERMINAL_TRIAL_STATES}


def _mode_badge(mode: str) -> dc.DataClassBadge:
    return dc.classify(mode=mode, value=mode, observed=True)


def harm_balances(state: Mapping[str, Any], case_id: Optional[str]
                  ) -> Tuple[HarmBalanceView, ...]:
    """Reserve, charge and return per harm contract, from the ledger alone.

    Pure arithmetic over append-only records; the reduced state carries no
    running total and this does not create one.  Pending charges -- charged
    against a trial that has not settled -- count against the balance, because
    a reserve an operator can no longer spend is spent as far as the decision
    to start another trial is concerned.
    """
    if not case_id:
        return ()
    case = (state.get("cases") or {}).get(case_id)
    if case is None:
        return ()
    usable = case.get("usableReserve") or {}
    movements: List[Mapping[str, Any]] = [
        entry for entry in (state.get("harmLedger") or ())
        if entry.get("caseId") == case_id]
    for entries in (state.get("pendingHarmCharges") or {}).values():
        movements.extend(entry for entry in entries
                         if entry.get("caseId") == case_id)

    refs = sorted(set(usable) | {str(entry.get("harmContractRef"))
                                 for entry in movements
                                 if entry.get("harmContractRef")})
    views: List[HarmBalanceView] = []
    for ref in refs:
        quantity = usable.get(ref) or {}
        unit = str(quantity.get("unit") or "")
        rows = [entry for entry in movements
                if entry.get("harmContractRef") == ref]

        def total(kind: str, *, missing_only: bool = False) -> float:
            return sum(
                float((entry.get("amount") or {}).get("value", 0) or 0)
                for entry in rows
                if entry.get("movementKind") == kind
                and (not missing_only
                     or bool(entry.get("chargedForMissingInterval"))))

        limit = quantity.get("value")
        charged = total("CHARGE")
        if limit is None:
            status, reason = st.UNKNOWN, "no usable reserve frozen for this ref"
        elif charged > float(limit):
            status, reason = st.ERROR, "charge exceeds the frozen usable reserve"
        elif charged == float(limit):
            status, reason = st.BLOCKED, "usable reserve is exhausted"
        else:
            status, reason = st.OK, None
        views.append(HarmBalanceView(
            harm_contract_ref=ref, unit=unit,
            usable=None if limit is None else float(limit),
            reserved=total("RESERVE"), charged=charged, returned=total("RETURN"),
            charged_for_missing_interval=total("CHARGE", missing_only=True),
            status=status, reason=reason))
    return tuple(views)


def measurement_freshness(state: Mapping[str, Any]) -> MeasurementFreshnessView:
    """What the Kernel has ingested, with the epoch's own freshness bound.

    Fails to ``UNKNOWN`` rather than to a reassuring default in both the ways
    it can: no samples at all, and samples whose clock the collector would not
    vouch for.
    """
    samples = list((state.get("samples") or {}).values())
    bounds = [int(record["body"]["freshnessBoundMs"])
              for record in (state.get("contracts") or {}).values()
              if record.get("family") == "MeasurementContract"
              and "freshnessBoundMs" in (record.get("body") or {})]
    bound = max(bounds) if bounds else None
    if not samples:
        return MeasurementFreshnessView(
            freshness_bound_ms=bound, status=st.UNKNOWN,
            reason="no measurement has reached the Kernel in this session")
    latest = max(samples, key=lambda item: str(item.get("observedAt") or ""))
    clock = str(latest.get("clockHealth") or "UNKNOWN")
    missing = sum(len(item.get("missingIntervals") or ()) for item in samples)
    if clock == "SYNCHRONISED":
        status, reason = st.OK, None
    elif clock == "DRIFTING_WITHIN_BOUND":
        status, reason = st.DEGRADED, "collector clock is drifting within bound"
    elif clock == "UNKNOWN":
        status, reason = st.UNKNOWN, "the source did not state its clock health"
    else:
        status, reason = st.STALE, f"collector clock is {clock}"
    return MeasurementFreshnessView(
        sample_count=len(samples),
        last_observed_at=str(latest.get("observedAt") or "") or None,
        last_counter_id=str(latest.get("counterId") or "") or None,
        cadence_ms=(int(latest["cadenceMs"])
                    if latest.get("cadenceMs") is not None else None),
        freshness_bound_ms=bound, clock_health=clock,
        missing_intervals=missing, status=status, reason=reason)


def _case_trials(state: Mapping[str, Any], case_id: Optional[str]
                 ) -> List[Tuple[str, Mapping[str, Any]]]:
    if not case_id:
        return []
    return sorted(
        ((trial_id, trial)
         for trial_id, trial in (state.get("trials") or {}).items()
         if trial.get("caseId") == case_id),
        key=lambda item: item[0])


def header_view(state: Mapping[str, Any], *, case_id: Optional[str] = None,
                mode: str = "DISCONNECTED", trial_id: Optional[str] = None,
                emergency_stop_requested: bool = False,
                unavailable_reason: Optional[str] = None) -> CockpitHeaderView:
    """Project the seven always-visible header facts.

    ``trial_id`` names the trial the operator is currently looking at; when it
    is absent the newest trial of the case is used, because that is what
    "current trial" means to somebody watching a case run.
    """
    if not state:
        return CockpitHeaderView(
            mode=mode, case_id=case_id,
            emergency_stop_requested=emergency_stop_requested,
            unavailable_reason=unavailable_reason
            or "no Kernel session is attached to this console")
    case = (state.get("cases") or {}).get(case_id) if case_id else None
    trials = _case_trials(state, case_id)
    if trial_id is None and trials:
        trial_id = trials[-1][0]
    trial = (state.get("trials") or {}).get(trial_id) if trial_id else None
    kernel_state = str(trial.get("state")) if trial else None
    if kernel_state is None and case is not None:
        kernel_state = ("CASE_TERMINATED" if case.get("terminal")
                        else "CASE_PAUSED" if case.get("paused")
                        else "CASE_OPEN")
    return CockpitHeaderView(
        mode=mode, case_id=case_id,
        case_terminal=(str(case.get("terminal")) if case
                       and case.get("terminal") else None),
        case_paused=(bool(case.get("paused")) if case else None),
        trial_id=trial_id, kernel_state=kernel_state,
        trial_outcome=(str(trial.get("outcome")) if trial else None),
        active_vector=(str(case.get("activeVector")) if case
                       and case.get("activeVector") else None),
        harm=harm_balances(state, case_id),
        measurement=measurement_freshness(state),
        emergency_stop_requested=emergency_stop_requested,
        trials_used=len(trials) if case_id else None,
        max_trials=(int(case["maxTrials"])
                    if case and case.get("maxTrials") is not None else None),
        deadline_at=(str(case.get("deadlineAt")) if case
                     and case.get("deadlineAt") else None),
        unavailable_reason=unavailable_reason)


def header_items(view: CockpitHeaderView) -> Tuple[HeaderItem, ...]:
    """The seven cells as text, each carrying its own data class.

    Pure and separate from the widget, which is what makes "the header always
    shows these seven things" a test rather than a screenshot review.
    """
    unknown = view.unavailable_reason or "no Kernel session is attached"
    mode_badge = _mode_badge(view.mode)
    derived = dc.resolve(dc.DERIVED, reason="arithmetic over the harm ledger")

    if view.case_id:
        case_value = f"{view.case_id} / {view.trial_id or 'no trial yet'}"
        case_badge = dc.classify(mode=view.mode, value=view.case_id)
        case_status = st.OK
        case_detail = (f"trial {view.trials_used}/{view.max_trials}"
                       if view.max_trials else None)
    else:
        case_value, case_badge = st.PRE_MEASUREMENT, dc.resolve(
            dc.UNKNOWN, reason=unknown)
        case_status, case_detail = st.UNKNOWN, unknown

    if view.kernel_state:
        kernel_value = view.kernel_state
        kernel_badge = dc.classify(mode=view.mode, value=view.kernel_state)
        # An unsettled trial is not an unknown Kernel state: the state itself
        # is known and normal, and it is the *outcome* that has not been
        # written yet.  Only a settled outcome colours this cell.
        kernel_status = (
            st.OK if view.trial_outcome in (None,
                                            TrialOutcome.NOT_SETTLED.value)
            else _OUTCOME_STATUS.get(str(view.trial_outcome), st.UNKNOWN))
        kernel_detail = (f"outcome {view.trial_outcome}"
                         if view.trial_outcome else view.case_terminal)
    else:
        kernel_value, kernel_badge = st.PRE_MEASUREMENT, dc.resolve(
            dc.UNKNOWN, reason=unknown)
        kernel_status, kernel_detail = st.UNKNOWN, unknown

    if view.active_vector:
        vector_value = view.active_vector
        vector_badge = dc.classify(mode=view.mode, value=view.active_vector)
        vector_status, vector_detail = st.OK, None
    else:
        vector_value = st.PRE_MEASUREMENT
        reason = ("no target vector has been released for this case"
                  if view.case_id else unknown)
        vector_badge = dc.resolve(dc.UNKNOWN, reason=reason)
        vector_status, vector_detail = st.UNKNOWN, reason

    if view.harm:
        harm_value = "  ".join(item.as_text() for item in view.harm)
        harm_status = st.worst([item.status for item in view.harm])
        harm_detail = next((item.reason for item in view.harm if item.reason),
                           None)
        harm_badge = derived
    else:
        harm_value = st.PRE_MEASUREMENT
        reason = ("no harm contract is frozen in this epoch"
                  if view.case_id else unknown)
        harm_badge = dc.resolve(dc.UNKNOWN, reason=reason)
        harm_status, harm_detail = st.UNKNOWN, reason

    measurement = view.measurement
    if measurement.sample_count:
        measurement_value = (f"{measurement.sample_count} sample(s), clock "
                             f"{measurement.clock_health}")
        measurement_badge = dc.classify(mode=view.mode,
                                        value=measurement.last_observed_at)
        measurement_detail = (
            f"last {st.format_utc(measurement.last_observed_at)}"
            if measurement.last_observed_at else measurement.reason)
    else:
        measurement_value = st.PRE_MEASUREMENT
        reason = (measurement.reason
                  or "no measurement has reached the Kernel in this session")
        measurement_badge = dc.resolve(dc.UNKNOWN, reason=reason)
        measurement_detail = reason

    if view.emergency_stop_requested:
        stop_value, stop_status = "REQUESTED", st.BLOCKED
        stop_detail = "honoured at the Kernel's next poll boundary"
    elif view.emergency_stop_available:
        stop_value, stop_status = "ARMED", st.OK
        stop_detail = "OPERATOR_ABORT -> Write Gateway stop -> rollback -> recovery"
    else:
        stop_value, stop_status = "no trial to stop", st.NOT_APPLICABLE
        stop_detail = ("nothing has been applied, so there is nothing to stop")
    stop_badge = dc.classify(mode=view.mode, value=stop_value)

    return (
        HeaderItem("case_trial", COCKPIT_HEADER_LABELS["case_trial"],
                   case_value, case_badge, case_status, case_detail),
        HeaderItem("kernel_state", COCKPIT_HEADER_LABELS["kernel_state"],
                   kernel_value, kernel_badge, kernel_status, kernel_detail),
        HeaderItem("target_vector", COCKPIT_HEADER_LABELS["target_vector"],
                   vector_value, vector_badge, vector_status, vector_detail),
        HeaderItem("harm", COCKPIT_HEADER_LABELS["harm"], harm_value,
                   harm_badge, harm_status, harm_detail),
        HeaderItem("measurement", COCKPIT_HEADER_LABELS["measurement"],
                   measurement_value, measurement_badge, measurement.status,
                   measurement_detail),
        HeaderItem("mode", COCKPIT_HEADER_LABELS["mode"], view.mode,
                   mode_badge,
                   st.OK if view.is_live else st.NOT_APPLICABLE,
                   None if view.is_live else mode_badge.reason
                   or "not a live radio"),
        HeaderItem("emergency_stop", COCKPIT_HEADER_LABELS["emergency_stop"],
                   stop_value, stop_badge, stop_status, stop_detail),
    )


# --------------------------------------------------------------------------- #
# Trial & Safety
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class CandidateRow:
    """One frozen catalog candidate with the Kernel's availability on it."""

    candidate_id: str
    target_ref: str = ""
    availability: str = CandidateAvailability.AVAILABLE.value
    status: str = st.UNKNOWN
    reason: Optional[str] = None


@dataclass(frozen=True)
class TrialRow:
    """One trial's lifecycle, as the Kernel recorded it.

    ``apply_counted`` and ``harm_clock_started_at`` are shown because task
    section 6.4 makes the first state-changing apply the instant the trial
    count and the harm clock start, and an operator reading a harm charge
    needs to see the clock it was charged against.
    """

    trial_id: str
    candidate_id: str = ""
    state: str = ""
    outcome: str = TrialOutcome.NOT_SETTLED.value
    stop_reason: Optional[str] = None
    guards_armed: bool = False
    apply_counted: bool = False
    harm_clock_started_at: Optional[str] = None
    observation_started_at: Optional[str] = None
    commit_acknowledged: bool = False
    finalize_acknowledged: bool = False
    configuration_reread: bool = False
    recovery_reread: bool = False
    recovery_verified: bool = False
    watchdog_ids: Tuple[str, ...] = ()
    armed_harm_contract_refs: Tuple[str, ...] = ()
    settlement_event_id: Optional[str] = None
    status: str = st.UNKNOWN

    @property
    def is_terminal(self) -> bool:
        return self.state in {item.value for item in TERMINAL_TRIAL_STATES}

    @property
    def rolled_back(self) -> bool:
        """A post-commit non-success that went through stop and rollback.

        Read off the recovery reread rather than off the outcome: the Kernel
        records the reread only when the rollback path actually ran, which is
        the difference between "did not succeed" and "was undone".
        """
        return bool(self.recovery_reread or self.recovery_verified)


@dataclass(frozen=True)
class TrialSafetyView:
    """Everything ``Trial & Safety`` draws, already decided by the Kernel."""

    mode: str = "DISCONNECTED"
    case_id: Optional[str] = None
    epoch_hash: str = ""
    active_vector: Optional[str] = None
    candidates: Tuple[CandidateRow, ...] = ()
    trials: Tuple[TrialRow, ...] = ()
    harm: Tuple[HarmBalanceView, ...] = ()
    resource_locks: Tuple[Tuple[str, str], ...] = ()
    emergency_stop_requested: bool = False
    case_terminal: Optional[str] = None
    case_paused: Optional[bool] = None
    trials_used: int = 0
    max_trials: Optional[int] = None
    deadline_at: Optional[str] = None
    unavailable_reason: Optional[str] = None

    @property
    def current_trial(self) -> Optional[TrialRow]:
        return self.trials[-1] if self.trials else None


#: Availability value -> GUI status.  ``AVAILABLE`` is the only OK; every other
#: value is a stated restriction, never a hidden row.
_AVAILABILITY_STATUS: Mapping[str, str] = {
    CandidateAvailability.AVAILABLE.value: st.OK,
    CandidateAvailability.LOCKED.value: st.BLOCKED,
    CandidateAvailability.IN_FLIGHT.value: st.DEGRADED,
    CandidateAvailability.CONSUMED.value: st.NOT_APPLICABLE,
    CandidateAvailability.TEMP_BLOCKED.value: st.BLOCKED,
    CandidateAvailability.INELIGIBLE.value: st.UNSUPPORTED,
    CandidateAvailability.EXHAUSTED.value: st.UNAVAILABLE,
}

_AVAILABILITY_REASON: Mapping[str, str] = {
    CandidateAvailability.LOCKED.value: "reserved by a proposal that has not settled",
    CandidateAvailability.IN_FLIGHT.value: "currently being trialled",
    CandidateAvailability.CONSUMED.value: "already trialled to a settled outcome in this epoch",
    CandidateAvailability.TEMP_BLOCKED.value: "capability missing, deployment draining or resource lock held elsewhere",
    CandidateAvailability.INELIGIBLE.value: "outside the active target vector's scope or validity region",
    CandidateAvailability.EXHAUSTED.value: "no admissible way remains to try it in this epoch",
}

#: Trial outcome -> GUI status.  Nine members, nine entries: an outcome the
#: table did not name would resolve to ``UNKNOWN``, and an operator would be
#: told nothing rather than told something wrong.
_OUTCOME_STATUS: Mapping[str, str] = {
    TrialOutcome.NOT_SETTLED.value: st.UNKNOWN,
    TrialOutcome.SUCCESS.value: st.OK,
    TrialOutcome.FAIL.value: st.DEGRADED,
    TrialOutcome.INDETERMINATE.value: st.UNKNOWN,
    TrialOutcome.INVALID.value: st.BLOCKED,
    TrialOutcome.EXEC_ERROR.value: st.ERROR,
    TrialOutcome.OPERATOR_ABORTED.value: st.BLOCKED,
    TrialOutcome.SAFETY_STOPPED.value: st.BLOCKED,
    TrialOutcome.RECOVERY_FAILED.value: st.ERROR,
}


def trial_safety_view(state: Mapping[str, Any], *,
                      case_id: Optional[str] = None,
                      mode: str = "DISCONNECTED",
                      emergency_stop_requested: bool = False,
                      unavailable_reason: Optional[str] = None
                      ) -> TrialSafetyView:
    """Project candidate availability, trial lifecycle, harm and rollback."""
    if not state:
        return TrialSafetyView(
            mode=mode, case_id=case_id,
            emergency_stop_requested=emergency_stop_requested,
            unavailable_reason=unavailable_reason
            or "no Kernel session is attached to this console")
    case = (state.get("cases") or {}).get(case_id) if case_id else None
    epoch_id = state.get("activeEpoch")
    epoch = (state.get("epochs") or {}).get(epoch_id) or {}
    availability = state.get("candidateAvailability") or {}
    targets = {
        record["body"]["contractId"]: record["body"]
        for record in (state.get("contracts") or {}).values()
        if record.get("family") == "TargetContract"}
    candidates = []
    # **도메인 카탈로그는 `candidates` 가 비어 있다.**  그 형식(2026-09-19)은 점을
    # 나열하지 않고 블록의 값 집합으로 적으므로, 이 목록만 돌면 소진된 후보가 화면에
    # 한 줄도 안 나온다 -- 커널 상태에는 `CONSUMED` 로 들어 있는데도.  "소진된 후보를
    # 숨기면 카탈로그가 더 작아 보인다" 는 것이 이 표의 존재 이유이므로, 나열된 점이
    # 없으면 availability 가 실제로 기록한 점들을 보여 준다 (2026-09-23).
    listed = list((state.get("catalog") or {}).get("candidates") or ())
    if not listed and availability:
        listed = [{"candidateId": candidate_id} for candidate_id in sorted(availability)]
    for item in listed:
        value = str(availability.get(item["candidateId"],
                                     CandidateAvailability.AVAILABLE.value))
        target_ref = str(item.get("targetRef") or "")
        candidates.append(CandidateRow(
            candidate_id=str(item["candidateId"]),
            target_ref=(f"{target_ref} "
                        f"({targets.get(target_ref, {}).get('objectiveFamily', '?')})"
                        if target_ref else ""),
            availability=value,
            status=_AVAILABILITY_STATUS.get(value, st.UNKNOWN),
            reason=_AVAILABILITY_REASON.get(value)))

    trials = []
    for trial_id, trial in _case_trials(state, case_id):
        outcome = str(trial.get("outcome", TrialOutcome.NOT_SETTLED.value))
        stop_reason = trial.get("stopReason")
        trials.append(TrialRow(
            trial_id=trial_id,
            candidate_id=str(trial.get("candidateId") or ""),
            state=str(trial.get("state") or ""),
            outcome=outcome,
            stop_reason=(getattr(stop_reason, "value", stop_reason)
                         if stop_reason else None),
            guards_armed=bool(trial.get("guardsArmed")),
            apply_counted=bool(trial.get("applyCounted")),
            harm_clock_started_at=trial.get("harmClockStartedAt"),
            observation_started_at=trial.get("observationStartedAt"),
            commit_acknowledged=bool(trial.get("commitAcknowledged")),
            finalize_acknowledged=bool(trial.get("finalizeAcknowledged")),
            configuration_reread=bool(trial.get("configurationReread")),
            recovery_reread=bool(trial.get("recoveryConfigurationReread")),
            recovery_verified=bool(trial.get("recoveryVerified")),
            watchdog_ids=tuple(str(item)
                               for item in (trial.get("planWatchdogIds") or ())),
            armed_harm_contract_refs=tuple(
                str(item)
                for item in (trial.get("armedHarmContractRefs") or ())),
            settlement_event_id=trial.get("settlementEventId"),
            status=_OUTCOME_STATUS.get(outcome, st.UNKNOWN)))

    return TrialSafetyView(
        mode=mode, case_id=case_id,
        epoch_hash=str(epoch.get("epochHash") or ""),
        active_vector=(str(case.get("activeVector")) if case
                       and case.get("activeVector") else None),
        candidates=tuple(candidates), trials=tuple(trials),
        harm=harm_balances(state, case_id),
        resource_locks=tuple(sorted(
            (str(resource), str(owner))
            for resource, owner in (state.get("resourceLocks") or {}).items())),
        emergency_stop_requested=emergency_stop_requested,
        case_terminal=(str(case.get("terminal")) if case
                       and case.get("terminal") else None),
        case_paused=(bool(case.get("paused")) if case else None),
        trials_used=len(trials),
        max_trials=(int(case["maxTrials"])
                    if case and case.get("maxTrials") is not None else None),
        deadline_at=(str(case.get("deadlineAt")) if case
                     and case.get("deadlineAt") else None),
        unavailable_reason=unavailable_reason)


# --------------------------------------------------------------------------- #
# Evidence Ledger
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class LedgerEntry:
    """One append-only Kernel event, as a row.

    ``position`` is the store's own ordinal.  It is shown because the ledger's
    guarantee is *append-only*, and an operator can only check that claim
    against a monotone position they can see.
    """

    position: int
    event_id: str
    event_kind: str
    object_id: str
    timestamp: str
    sequence: int
    content_hash: str = ""
    source_component: str = ""

    def as_text(self) -> str:
        return (f"{self.position:>5}  {self.timestamp}  {self.event_kind}  "
                f"{self.object_id}  [{self.event_id}]")


@dataclass(frozen=True)
class EvidenceCellRow:
    """One evidence obligation, with closure and witnesses kept apart.

    ``post_closure_witnesses`` is a separate count from ``contributions`` on
    purpose: task section 5.16 appends a compatible pass after a closed fail as
    a witness that does *not* rewrite the record, and a row that folded the two
    together would show a failed cell as having improved.
    """

    cell_id: str
    target_ref: str = ""
    status: str = EvidenceCellStatus.OPEN.value
    sealed: bool = False
    sealed_until_vector_ref: Optional[str] = None
    contributions: int = 0
    reused_contributions: int = 0
    pending_contributions: int = 0
    pending_status: Optional[str] = None
    post_closure_witnesses: int = 0
    gui_status: str = st.UNKNOWN
    reason: Optional[str] = None

    @property
    def is_closed(self) -> bool:
        return self.status in {EvidenceCellStatus.CLOSED_PASS.value,
                               EvidenceCellStatus.CLOSED_FAIL.value}

    @property
    def blocks_exhaustion(self) -> bool:
        return self.status in {item.value
                               for item in OPEN_EVIDENCE_CELL_STATUSES}


@dataclass(frozen=True)
class ConfirmationRow:
    """One confirmation record, showing only what task section 4.2 allows.

    Content hash, instant, event id and whether the content moved afterwards.
    There is no name, no role and no identity on this row, and there is no
    field on the dataclass that could carry one.
    """

    event_id: str
    action: str
    content_hash: str
    confirmed_object_type: str
    timestamp: str
    invalidated: bool = False


@dataclass(frozen=True)
class EvidenceLedgerView:
    """The append-only stream, the cells, the harm ledger and confirmations."""

    mode: str = "DISCONNECTED"
    case_id: Optional[str] = None
    epoch_hash: str = ""
    reducer_version: str = ""
    terminal_state_hash: str = ""
    events: Tuple[LedgerEntry, ...] = ()
    event_count: int = 0
    cells: Tuple[EvidenceCellRow, ...] = ()
    harm: Tuple[HarmBalanceView, ...] = ()
    harm_movements: Tuple[Mapping[str, Any], ...] = ()
    confirmations: Tuple[ConfirmationRow, ...] = ()
    exhaustion_certificates: Tuple[Tuple[str, str], ...] = ()
    unavailable_reason: Optional[str] = None

    @property
    def closure_progress(self) -> Optional[float]:
        """Closed cells over all cells, or ``None`` when there are no cells.

        ``None`` rather than ``1.0``: a case with no evidence obligation has
        not closed everything, it has nothing to close, and reporting a
        complete bar for it would be the overstatement task section 9.13
        forbids.
        """
        if not self.cells:
            return None
        return sum(1 for cell in self.cells if cell.is_closed) / len(self.cells)

    @property
    def open_obligations(self) -> Tuple[EvidenceCellRow, ...]:
        return tuple(cell for cell in self.cells if cell.blocks_exhaustion)


_CELL_STATUS: Mapping[str, str] = {
    EvidenceCellStatus.OPEN.value: st.UNKNOWN,
    EvidenceCellStatus.PARTIAL.value: st.DEGRADED,
    EvidenceCellStatus.TEMP_BLOCKED.value: st.BLOCKED,
    EvidenceCellStatus.BUDGET_LOCKED.value: st.BLOCKED,
    EvidenceCellStatus.CLOSED_PASS.value: st.OK,
    EvidenceCellStatus.CLOSED_FAIL.value: st.ERROR,
    EvidenceCellStatus.DORMANT_SEALED.value: st.NOT_APPLICABLE,
    EvidenceCellStatus.PROVISIONAL_HISTORICAL.value: st.STALE,
    EvidenceCellStatus.POST_CLOSURE_WITNESS.value: st.EXTERNAL,
}

_CELL_REASON: Mapping[str, str] = {
    EvidenceCellStatus.OPEN.value: "no usable contribution yet",
    EvidenceCellStatus.PARTIAL.value: "below the contract's closure quota",
    EvidenceCellStatus.TEMP_BLOCKED.value: "temporarily unobtainable; not a failure and not closure",
    EvidenceCellStatus.BUDGET_LOCKED.value: "blocked by an exhausted reserve or trial cap",
    EvidenceCellStatus.DORMANT_SEALED.value: "sealed until its target vector is released",
    EvidenceCellStatus.PROVISIONAL_HISTORICAL.value: "carried from a prior epoch; provisional until a confirmation trial",
    EvidenceCellStatus.POST_CLOSURE_WITNESS.value: "recorded after closure; never counted into the closed quota",
}

def evidence_ledger_view(state: Mapping[str, Any],
                         events: Sequence[Any] = (), *,
                         case_id: Optional[str] = None,
                         mode: str = "DISCONNECTED",
                         reducer_version: str = "",
                         terminal_state_hash: str = "",
                         confirmations: Sequence[Any] = (),
                         limit: int = 500,
                         unavailable_reason: Optional[str] = None
                         ) -> EvidenceLedgerView:
    """Project the append-only stream and the evidence obligations.

    ``limit`` bounds what is *rendered*, never what is counted:
    ``event_count`` is the full length, so a truncated view can never read as
    a short ledger.
    """
    if not state and not events:
        return EvidenceLedgerView(
            mode=mode, case_id=case_id,
            unavailable_reason=unavailable_reason or NO_EVENT_STREAM)
    epoch_id = state.get("activeEpoch")
    epoch = (state.get("epochs") or {}).get(epoch_id) or {}

    rows = []
    for position, envelope in enumerate(events, start=1):
        rows.append(LedgerEntry(
            position=position,
            event_id=str(getattr(envelope, "event_id", "")),
            event_kind=str(getattr(envelope, "event_kind", "")),
            object_id=str(getattr(envelope, "object_id", "")),
            timestamp=str(getattr(envelope, "timestamp", "")),
            sequence=int(getattr(envelope, "sequence", 0) or 0),
            content_hash=str(getattr(envelope, "content_hash", "")),
            source_component=str(getattr(
                getattr(envelope, "source_component", ""), "value",
                getattr(envelope, "source_component", "")))))

    cells = []
    for cell_id, cell in sorted((state.get("evidenceCells") or {}).items()):
        if case_id and cell.get("caseId") not in (None, case_id):
            continue
        contributions = list(cell.get("contributions") or ())
        status = str(cell.get("status") or EvidenceCellStatus.OPEN.value)
        cells.append(EvidenceCellRow(
            cell_id=str(cell_id),
            target_ref=str(cell.get("targetRef") or ""),
            status=status,
            sealed=cell.get("sealedUntilVectorRef") is not None,
            sealed_until_vector_ref=cell.get("sealedUntilVectorRef"),
            contributions=len(contributions),
            reused_contributions=sum(
                1 for item in contributions
                if item.get("reusedFromEpoch") is not None),
            pending_contributions=len(cell.get("pendingEvidenceUpdates") or ()),
            pending_status=cell.get("pendingStatus"),
            post_closure_witnesses=sum(
                1 for item in contributions
                if str(item.get("resultingStatus")
                       or item.get("status")
                       or "") == EvidenceCellStatus.POST_CLOSURE_WITNESS.value),
            gui_status=_CELL_STATUS.get(status, st.UNKNOWN),
            reason=_CELL_REASON.get(status)))

    movements = tuple(
        dict(entry) for entry in (state.get("harmLedger") or ())
        if not case_id or entry.get("caseId") == case_id)

    confirmation_rows = tuple(
        ConfirmationRow(
            event_id=str(getattr(record, "event_id", "")),
            action=str(getattr(getattr(record, "action", ""), "value",
                               getattr(record, "action", ""))),
            content_hash=str(getattr(record, "confirmed_content_hash", "")),
            confirmed_object_type=str(getattr(record, "confirmed_object_type",
                                              "")),
            timestamp=str(getattr(record, "timestamp", "")),
            invalidated=bool(getattr(record, "changed_after_confirmation",
                                     False)))
        for record in confirmations)

    return EvidenceLedgerView(
        mode=mode, case_id=case_id,
        epoch_hash=str(epoch.get("epochHash") or ""),
        reducer_version=reducer_version,
        terminal_state_hash=terminal_state_hash,
        events=tuple(rows[-limit:]) if limit else tuple(rows),
        event_count=len(rows),
        cells=tuple(cells),
        harm=harm_balances(state, case_id),
        harm_movements=movements,
        confirmations=confirmation_rows,
        exhaustion_certificates=tuple(sorted(
            (str(key), str((value or {}).get("aggregateState", "")))
            for key, value in
            (state.get("exhaustionCertificates") or {}).items())),
        unavailable_reason=unavailable_reason
        or (NO_EVENT_STREAM if not rows else None))


# --------------------------------------------------------------------------- #
# Contract context (task section 9.2)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class TargetOptionRow:
    """One option of the target contract, with the space it may move in."""

    option_ref: str
    capability_ref: str = ""
    parameter_space: Mapping[str, Any] = field(default_factory=dict)
    preconditions: Tuple[str, ...] = ()
    document_status: str = ""


@dataclass(frozen=True)
class HarmLimitRow:
    """One harm bound, with everything task section 5.8 requires beside it.

    A measured sample maximum is *not* an admission bound on its own, so the
    enforced timeout, the conservative margin, the operating scope and the
    calibration/proof references travel with the number.  A row that showed
    only ``admissibleBound`` would let a bare sample maximum read as a
    certified limit.
    """

    harm_contract_ref: str
    bound_id: str = ""
    harm_kind: str = ""
    admissible_value: Optional[float] = None
    measured_value: Optional[float] = None
    conservative_margin: Optional[float] = None
    unit: str = ""
    enforced_timeout_ms: Optional[int] = None
    operating_scope: Mapping[str, Any] = field(default_factory=dict)
    proof_ref: Optional[str] = None
    calibration_records: Tuple[str, ...] = ()
    reserve: Optional[float] = None
    missing_interval_charge: Optional[float] = None


@dataclass(frozen=True)
class MeasurementRow:
    """One measurement contract's window, hold, freshness and clock rule."""

    measurement_ref: str
    counter_id: str = ""
    cadence_ms: Optional[int] = None
    window_width_ms: Optional[int] = None
    window_stride_ms: Optional[int] = None
    overlap: str = ""
    hold_ms: Optional[int] = None
    freshness_bound_ms: Optional[int] = None
    clock_requirement: str = ""
    aggregation: str = ""
    estimator: str = ""
    gap_policy: str = ""
    minimum_entity_count: Optional[int] = None
    scope_selector: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CapabilityRow:
    """What one capability manifest says this deployment can and cannot do."""

    capability_id: str
    supported_objectives: Tuple[str, ...] = ()
    actuator_refs: Tuple[str, ...] = ()
    measurement_refs: Tuple[str, ...] = ()
    interface_versions: Mapping[str, str] = field(default_factory=dict)
    constraints: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ContractContextView:
    """Task section 9.2's six things, for the objective now in front of us.

    Intent Profile, target options, harm limits, measurement/hold, capability
    limitation, unsupported reason.  Every one is read off the *frozen epoch*,
    so what the operator reads is what the Kernel would act on rather than
    what a form was last set to.
    """

    mode: str = "DISCONNECTED"
    case_id: Optional[str] = None
    intent_text: Optional[str] = None
    objective_family: Optional[str] = None
    scope_selector: Mapping[str, Any] = field(default_factory=dict)
    target_ref: Optional[str] = None
    hold_ms: Optional[int] = None
    options: Tuple[TargetOptionRow, ...] = ()
    predicates: Tuple[Tuple[str, str, bool], ...] = ()
    harm_limits: Tuple[HarmLimitRow, ...] = ()
    measurements: Tuple[MeasurementRow, ...] = ()
    capabilities: Tuple[CapabilityRow, ...] = ()
    unsupported_requests: Tuple[str, ...] = ()
    standard_mapping: Tuple[Tuple[str, str], ...] = ()
    standard_mapping_status: str = st.UNKNOWN
    standard_mapping_reason: Optional[str] = None
    unavailable_reason: Optional[str] = None


def _quantity(value: Any) -> Tuple[Optional[float], str]:
    if not isinstance(value, Mapping):
        return None, ""
    try:
        return float(value.get("value")), str(value.get("unit") or "")
    except (TypeError, ValueError):
        return None, str(value.get("unit") or "")


def standard_mapping_for(objective_family: Optional[str]
                         ) -> Tuple[Tuple[Tuple[str, str], ...], str,
                                    Optional[str]]:
    """The published interface/service-model versions this objective maps onto.

    Read from the objective registry, which is the one place that answers it.
    A family the registry does not carry is ``UNSUPPORTED`` naming itself --
    task section 7.7 forbids dressing a project identifier up as an official
    O-RAN objective name, and inventing a mapping for an unregistered family
    would be exactly that.
    """
    if not objective_family:
        return (), st.UNKNOWN, "no objective has been read from the intent yet"
    try:
        from .objective_registry import project_registry

        row = project_registry().row(objective_family)
    except Exception:
        return ((), st.UNSUPPORTED,
                f"{objective_family} is not in the objective registry, so no "
                f"published standard mapping is declared for it")
    rows: List[Tuple[str, str]] = [
        ("project contract id", row.project_contract_id),
        ("A1 interface", row.policy_interface),
        ("A1 policy type", f"{row.policy_type_id or st.PRE_MEASUREMENT} "
                           f"({row.policy_type_kind})"),
    ]
    rows += [("E2 control", text) for text in row.control_service_models]
    rows += [("E2 report", text) for text in row.measurement_service_models]
    rows += [("O1 measurement",
              f"{text}{'' if delivered else '  (mapped, not delivered)'}")
             for text, delivered in row.o1_measurements]
    if row.mapping_notes:
        rows.append(("note", row.mapping_notes))
    status = st.OK if row.submittable else st.UNSUPPORTED
    reason = None if row.submittable else "; ".join(row.blocking_reasons)
    return tuple(rows), status, reason


def contract_context_view(state: Mapping[str, Any], *,
                          case_id: Optional[str] = None,
                          mode: str = "DISCONNECTED",
                          preview: Any = None,
                          unavailable_reason: Optional[str] = None
                          ) -> ContractContextView:
    """Project the frozen epoch's contracts for the drafted objective."""
    objective_family = getattr(preview, "objective_family", None)
    intent_text = getattr(preview, "intent_text", None)
    scope = dict(getattr(preview, "scope_selector", {}) or {})
    mapping, mapping_status, mapping_reason = standard_mapping_for(
        objective_family)
    if not state:
        return ContractContextView(
            mode=mode, case_id=case_id, intent_text=intent_text,
            objective_family=objective_family, scope_selector=scope,
            standard_mapping=mapping,
            standard_mapping_status=mapping_status,
            standard_mapping_reason=mapping_reason,
            unavailable_reason=unavailable_reason
            or "no frozen epoch is attached to this console")

    contracts = list((state.get("contracts") or {}).values())

    def bodies(family: str) -> List[Mapping[str, Any]]:
        return [record["body"] for record in contracts
                if record.get("family") == family]

    targets = bodies("TargetContract")
    if objective_family:
        selected = [body for body in targets
                    if body.get("objectiveFamily") == objective_family]
    else:
        selected = targets
    target = selected[0] if selected else None

    options = tuple(
        TargetOptionRow(
            option_ref=str(item.get("contractId") or ""),
            capability_ref=str(item.get("capabilityRef") or ""),
            parameter_space=dict(item.get("parameterSpace") or {}),
            preconditions=tuple(str(p) for p in (item.get("preconditions")
                                                 or ())),
            document_status=str(item.get("documentStatus") or ""))
        for item in ((target or {}).get("options") or ()))

    predicates = tuple(
        (str(item.get("predicateId") or ""), str(item.get("description") or ""),
         bool(item.get("mandatory")))
        for item in ((target or {}).get("predicates") or ()))

    harm_limits: List[HarmLimitRow] = []
    for body in bodies("HarmContract"):
        reserve, _unit = _quantity(body.get("reserve"))
        missing, _mu = _quantity(body.get("missingIntervalCharge"))
        for bound in (body.get("bounds") or ()):
            admissible, unit = _quantity(bound.get("admissibleBound"))
            measured, _u = _quantity(bound.get("measuredBound"))
            margin, _m = _quantity(bound.get("conservativeMargin"))
            harm_limits.append(HarmLimitRow(
                harm_contract_ref=str(body.get("contractId") or ""),
                bound_id=str(bound.get("boundId") or ""),
                harm_kind=str(body.get("harmKind") or ""),
                admissible_value=admissible, measured_value=measured,
                conservative_margin=margin, unit=unit,
                enforced_timeout_ms=(int(bound["enforcedTimeoutMs"])
                                     if bound.get("enforcedTimeoutMs")
                                     is not None else None),
                operating_scope=dict(bound.get("operatingScope") or {}),
                proof_ref=bound.get("proofRef"),
                calibration_records=tuple(
                    str(item)
                    for item in (bound.get("calibrationRecords") or ())),
                reserve=reserve, missing_interval_charge=missing))

    measurements = tuple(
        MeasurementRow(
            measurement_ref=str(body.get("contractId") or ""),
            counter_id=str(body.get("counterId") or ""),
            cadence_ms=body.get("cadenceMs"),
            window_width_ms=body.get("windowWidthMs"),
            window_stride_ms=body.get("windowStrideMs"),
            overlap=str(body.get("overlap") or ""),
            hold_ms=body.get("holdMs"),
            freshness_bound_ms=body.get("freshnessBoundMs"),
            clock_requirement=str(body.get("clockRequirement") or ""),
            aggregation=str(body.get("aggregation") or ""),
            estimator=str(body.get("estimator") or ""),
            gap_policy=str(body.get("gapPolicy") or ""),
            minimum_entity_count=body.get("minimumEntityCount"),
            scope_selector=dict(body.get("scopeSelector") or {}))
        for body in sorted(bodies("MeasurementContract"),
                           key=lambda item: str(item.get("contractId"))))

    capabilities = tuple(
        CapabilityRow(
            capability_id=str(body.get("capabilityId")
                              or body.get("contractId") or ""),
            supported_objectives=tuple(
                str(item) for item in (body.get("supportedObjectives") or ())),
            actuator_refs=tuple(str(item)
                                for item in (body.get("actuatorRefs") or ())),
            measurement_refs=tuple(
                str(item) for item in (body.get("measurementRefs") or ())),
            interface_versions=dict(body.get("interfaceVersions") or {}),
            constraints=tuple(
                f"{item.get('measurementRef', '?')} "
                f"{item.get('operator', '?')} "
                f"{(item.get('bound') or {}).get('value', '?')} "
                f"{(item.get('bound') or {}).get('unit', '')}".strip()
                for item in (body.get("constraints") or ())))
        for body in sorted(bodies("CapabilityManifest"),
                           key=lambda item: str(item.get("contractId"))))

    return ContractContextView(
        mode=mode, case_id=case_id, intent_text=intent_text,
        objective_family=objective_family, scope_selector=scope,
        target_ref=(str(target.get("contractId")) if target else None),
        hold_ms=(target or {}).get("holdMs"),
        options=options, predicates=predicates,
        harm_limits=tuple(harm_limits), measurements=measurements,
        capabilities=capabilities,
        unsupported_requests=tuple(
            str(item)
            for item in (getattr(preview, "unsupported_requests", ()) or ())),
        standard_mapping=mapping, standard_mapping_status=mapping_status,
        standard_mapping_reason=mapping_reason,
        unavailable_reason=unavailable_reason)


# --------------------------------------------------------------------------- #
# One correlation, end to end (task section 9.1)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class CorrelationStep:
    """One link of the chain, with its own status, reason and data class."""

    key: str
    label: str
    value: str
    status: str = st.UNKNOWN
    badge: Optional[dc.DataClassBadge] = None
    reason: Optional[str] = None

    def as_text(self) -> str:
        glyph = st.resolve(self.status).glyph
        mark = self.badge.glyph if self.badge is not None else ""
        text = f"{glyph} {mark} {self.label}: {self.value}".replace("  ", " ")
        return f"{text} - {self.reason}" if self.reason else text


#: The seven links task section 9.1 names, in order.  Declared as data so the
#: chain cannot silently lose one.
CORRELATION_STEPS: Tuple[Tuple[str, str], ...] = (
    ("intent", "Intent"),
    ("contract_preview", "Contract preview"),
    ("standard_mapping", "Objective / standard mapping"),
    ("candidate_trial", "Candidate / trial"),
    ("evidence", "Evidence"),
    ("verdict", "Verdict"),
    ("recovery", "Recovery"),
)


def correlation_chain(*, header: CockpitHeaderView,
                      trial_safety: TrialSafetyView,
                      evidence: EvidenceLedgerView,
                      context: ContractContextView,
                      correlation_id: Optional[str] = None
                      ) -> Tuple[CorrelationStep, ...]:
    """The seven links, each resolved on its own.

    Deliberately seven independent statuses rather than one roll-up: an
    operator may legitimately have an intent read, a candidate tried and an
    evidence cell still open, and a single chain status would hide exactly
    that.  Every link that has nothing to show says why.
    """
    mode = header.mode
    steps: List[CorrelationStep] = []

    def unknown(key: str, label: str, reason: str) -> CorrelationStep:
        return CorrelationStep(key, label, st.PRE_MEASUREMENT, st.UNKNOWN,
                               dc.resolve(dc.UNKNOWN, reason=reason),
                               reason)

    if context.intent_text:
        steps.append(CorrelationStep(
            "intent", "Intent", context.intent_text, st.OK,
            dc.resolve(dc.REPLAY if mode != "LIVE" else dc.LIVE,
                       reason=None if mode == "LIVE"
                       else "read by the Intent Agent, not observed")))
    else:
        steps.append(unknown("intent", "Intent",
                             "no intent has been read in this session"))

    if context.objective_family and context.target_ref:
        steps.append(CorrelationStep(
            "contract_preview", "Contract preview",
            f"{context.objective_family} -> {context.target_ref}", st.OK,
            dc.classify(mode=mode, value=context.target_ref)))
    else:
        steps.append(unknown("contract_preview", "Contract preview",
                             "no contract instance has been drafted"))

    if context.standard_mapping:
        steps.append(CorrelationStep(
            "standard_mapping", "Objective / standard mapping",
            "; ".join(f"{label} {value}"
                      for label, value in context.standard_mapping[:3]),
            context.standard_mapping_status,
            dc.resolve(dc.REPLAY, reason="declared by the objective registry"),
            context.standard_mapping_reason))
    else:
        steps.append(CorrelationStep(
            "standard_mapping", "Objective / standard mapping",
            st.PRE_MEASUREMENT, context.standard_mapping_status,
            dc.resolve(
                dc.UNSUPPORTED
                if context.standard_mapping_status == st.UNSUPPORTED
                else dc.UNKNOWN,
                reason=context.standard_mapping_reason),
            context.standard_mapping_reason))

    trial = trial_safety.current_trial
    if trial is not None:
        steps.append(CorrelationStep(
            "candidate_trial", "Candidate / trial",
            f"{trial.candidate_id} -> {trial.trial_id}", st.OK,
            dc.classify(mode=mode, value=trial.trial_id)))
    else:
        steps.append(unknown("candidate_trial", "Candidate / trial",
                             "no trial has been opened for this case"))

    if evidence.cells:
        closed = sum(1 for cell in evidence.cells if cell.is_closed)
        steps.append(CorrelationStep(
            "evidence", "Evidence",
            f"{closed}/{len(evidence.cells)} cell(s) closed; "
            f"{', '.join(sorted({cell.status for cell in evidence.cells}))}",
            st.worst([cell.gui_status for cell in evidence.cells]),
            dc.resolve(dc.DERIVED,
                       reason="closed cells over registered cells")))
    else:
        steps.append(unknown("evidence", "Evidence",
                             "no evidence cell is registered for this case"))

    if trial is not None and trial.outcome != TrialOutcome.NOT_SETTLED.value:
        steps.append(CorrelationStep(
            "verdict", "Verdict", trial.outcome, trial.status,
            dc.classify(mode=mode, value=trial.outcome)))
    else:
        steps.append(unknown("verdict", "Verdict",
                             "the Kernel has not settled a trial yet"))

    if trial is None:
        steps.append(unknown("recovery", "Recovery",
                             "no trial has been opened for this case"))
    elif trial.rolled_back:
        steps.append(CorrelationStep(
            "recovery", "Recovery",
            f"rolled back; recovery verified "
            f"{'yes' if trial.recovery_verified else 'no'}",
            st.OK if trial.recovery_verified else st.ERROR,
            dc.classify(mode=mode, value="rolled back")))
    elif trial.is_terminal:
        steps.append(CorrelationStep(
            "recovery", "Recovery", "not required", st.NOT_APPLICABLE,
            dc.classify(mode=mode, value="not required"),
            "the trial settled without a post-commit non-success"))
    else:
        steps.append(unknown("recovery", "Recovery",
                             "the trial has not reached a terminal state"))
    return tuple(steps)


# --------------------------------------------------------------------------- #
# One snapshot for the whole Cockpit
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class CockpitSnapshot:
    """Header, Trial & Safety and Evidence Ledger from one reduced state.

    Built once per poll and published whole, so the three surfaces can never
    show three different instants of the same case.
    """

    header: CockpitHeaderView
    trial_safety: TrialSafetyView
    evidence: EvidenceLedgerView
    context: ContractContextView = field(default_factory=ContractContextView)
    chain: Tuple[CorrelationStep, ...] = ()
    correlation_id: Optional[str] = None

    @property
    def mode(self) -> str:
        return self.header.mode

    def step(self, key: str) -> Optional[CorrelationStep]:
        """One link of the correlation chain by name, or ``None``."""
        return next((step for step in self.chain if step.key == key), None)


def project(state: Mapping[str, Any], *, case_id: Optional[str] = None,
            mode: str = "DISCONNECTED", events: Sequence[Any] = (),
            trial_id: Optional[str] = None,
            emergency_stop_requested: bool = False,
            reducer_version: str = "", terminal_state_hash: str = "",
            confirmations: Sequence[Any] = (), preview: Any = None,
            unavailable_reason: Optional[str] = None) -> CockpitSnapshot:
    """One reduced state in, one Cockpit snapshot out.

    ``correlation_id`` is the case id, and it is the same id the Contract
    Studio's preview, the Kernel's trials, the harm ledger and the evidence
    cells are keyed by -- which is what makes task section 9.1's "one
    correlation from intent to recovery" a property of the data rather than a
    label on a screen.
    """
    header = header_view(
        state, case_id=case_id, mode=mode, trial_id=trial_id,
        emergency_stop_requested=emergency_stop_requested,
        unavailable_reason=unavailable_reason)
    trial_safety = trial_safety_view(
        state, case_id=case_id, mode=mode,
        emergency_stop_requested=emergency_stop_requested,
        unavailable_reason=unavailable_reason)
    evidence = evidence_ledger_view(
        state, events, case_id=case_id, mode=mode,
        reducer_version=reducer_version,
        terminal_state_hash=terminal_state_hash,
        confirmations=confirmations,
        unavailable_reason=unavailable_reason)
    context = contract_context_view(
        state, case_id=case_id, mode=mode, preview=preview,
        unavailable_reason=unavailable_reason)
    return CockpitSnapshot(
        header=header, trial_safety=trial_safety, evidence=evidence,
        context=context,
        chain=correlation_chain(header=header, trial_safety=trial_safety,
                                evidence=evidence, context=context,
                                correlation_id=case_id),
        correlation_id=case_id)


def project_session(session: Any) -> CockpitSnapshot:
    """Project a live :class:`KernelSubmissionSession` without touching it.

    Reads only: the session's own ``path.kernel`` reduced state, the append
    only stream behind it, and the confirmations the session has taken.  It
    starts nothing, confirms nothing and stops nothing.
    """
    path = getattr(session, "path", None)
    kernel = getattr(path, "kernel", None)
    if kernel is None:
        return project({}, case_id=getattr(session, "case_id", None),
                       mode=str(getattr(session, "mode", "DISCONNECTED")),
                       unavailable_reason="this session has no Kernel attached")
    try:
        state = kernel.reduced_state()
    except Exception as exc:                               # pragma: no cover
        return project({}, case_id=getattr(session, "case_id", None),
                       mode=str(getattr(session, "mode", "DISCONNECTED")),
                       unavailable_reason=f"reduced state unavailable: {exc}")
    confirmed = getattr(session, "confirmed", None)
    records = []
    if confirmed is not None and getattr(confirmed, "confirmation", None):
        records.append(confirmed.confirmation)
    records.extend(getattr(session, "_invalidated", ()) or ())
    return project(
        state, case_id=getattr(session, "case_id", None),
        mode=str(getattr(session, "mode", "DISCONNECTED")),
        events=kernel_events(kernel),
        trial_id=getattr(session, "trial_id", None),
        emergency_stop_requested=bool(
            getattr(session, "emergency_stop_requested", False)),
        reducer_version=str(getattr(getattr(kernel, "_reducer", None),
                                    "reducer_version", "")),
        terminal_state_hash=_terminal_state_hash(kernel, state),
        confirmations=tuple(records),
        preview=getattr(session, "preview", None))


def _terminal_state_hash(kernel: Any, state: Mapping[str, Any]) -> str:
    """The Kernel's own terminal-state digest, or an empty string.

    Computed from the reduced state this projection already holds, through the
    Kernel's *own* hash function -- not by calling
    ``AssuranceKernel.terminal_state_hash``, which replays the whole stream a
    second time.  One poll of a long run would otherwise cost two full
    replays, and the value is identical either way because it is the same
    function over the same state.

    Empty rather than a placeholder when it cannot be produced: the digest is
    what a replay is checked against, and a Kernel that will not produce one
    has not produced one.
    """
    reducer = getattr(kernel, "_reducer", None)
    version = getattr(reducer, "reducer_version", None)
    if not version or not state:
        return ""
    try:
        from assurance.kernel.reducer import terminal_state_hash

        return str(terminal_state_hash(state, reducer_version=version))
    except Exception:
        return ""


__all__ = [
    "COCKPIT_HEADER_KEYS", "COCKPIT_HEADER_LABELS", "CORRELATION_STEPS",
    "CandidateRow", "CapabilityRow", "CockpitHeaderView", "CockpitSnapshot",
    "ConfirmationRow", "ContractContextView", "CorrelationStep",
    "EvidenceCellRow", "EvidenceLedgerView", "HarmBalanceView", "HarmLimitRow",
    "HeaderItem", "LedgerEntry", "MeasurementFreshnessView", "MeasurementRow",
    "NO_EVENT_STREAM", "TargetOptionRow", "TrialRow", "TrialSafetyView",
    "contract_context_view", "correlation_chain", "evidence_ledger_view",
    "harm_balances", "header_items", "header_view", "kernel_events",
    "measurement_freshness", "project", "project_session",
    "standard_mapping_for", "trial_safety_view",
]
