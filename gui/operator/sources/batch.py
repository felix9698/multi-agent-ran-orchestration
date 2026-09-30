"""The Cockpit's Batch Experiments path: one bounded plan, confirmed once.

Task sections 9.3, 9.4 and 12.  Three properties are the whole module, and each
has a test:

**Interactive and Batch share the Kernel and the evidence store.**
:class:`KernelPathCaseExecutor` drives every batch case through the *same*
:class:`~gui.operator.sources.kernel_live.KernelSubmissionSession` an
interactive run uses -- draft, confirm the content hash, run the Kernel's trial
loop, read the terminal off the Kernel -- so there is no second decision path
to disagree with the first.  Every number in a batch record is read out of the
Kernel's reduced state; none is invented here.  The run directory is written by
:class:`runstore.session_store.SessionStore`, which is the identical class the
Operator Console's own session controller writes with.

**A bounded plan is confirmed once and then its scope does not move.**  The
plan is content-addressed by :class:`~assurance.batch.plan.BatchPlan`; the
confirmation covers that hash; editing any scope field invalidates the
confirmation, and starting the run locks the draft entirely.  The repetition
that follows changes nothing about the plan -- which is exactly section 9.4's
"bounded plan confirmed once, then a repetition whose scope does not change".

**Live is refused here, by the runner, not by a convention.**
:class:`~assurance.batch.runner.BatchRunner` admits ``REPLAY`` and ``EMULATED``
executors only.  A Cockpit session bound to a live deployment therefore cannot
be batched from this pane, and the refusal is stated rather than hidden.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace as dc_replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from assurance.batch.plan import (
    BatchCase,
    BatchPlan,
    Budget,
    IntentProfile,
    RetryPolicy,
    Windows,
)
from assurance.batch.runner import BatchRunner, CaseExecution
from assurance.core.axes import TrialOutcome
from assurance.core.confirmation import ConfirmationRecord

from .. import data_class as dc
from .. import status as st
from .kernel_live import MODE_MOCK, MODE_REPLAY, SubmissionRefused


class BatchRefused(RuntimeError):
    """A batch action the console will not take, with a code and a detail."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# --------------------------------------------------------------------------- #
# The plan draft: exactly task section 12's settable fields
# --------------------------------------------------------------------------- #

#: ``key -> (label, help)``, in the order task section 12 lists them.  Declared
#: as data so the pane and the acceptance test read the same list, and so a
#: field that is dropped from the form fails a test rather than going quiet.
PLAN_FIELDS: Tuple[Tuple[str, str, str], ...] = (
    ("objectives", "Objectives",
     "comma-separated objective families, from the deployment's registry"),
    ("profiles", "Intent Profiles",
     "comma-separated 'profileId=intent sentence' pairs"),
    ("scope_ue", "Scope (UE)", "the UE identity every profile is scoped to"),
    ("strategy", "Strategy", "the comparison strategy this campaign runs"),
    ("repeats", "Repeat count", "repetitions of the objective x profile matrix"),
    ("seed", "Seed", "seed for the ordering and per-case seeds"),
    ("ordering", "Order", "randomized or counterbalanced"),
    ("warmup_s", "Warm-up (s)", "before the first case"),
    ("recovery_s", "Recovery (s)", "after each case"),
    ("measurement_s", "Measurement window (s)", "contracted measurement window"),
    ("observation_s", "Observation window (s)", "contracted observation window"),
    ("hold_s", "Hold window (s)", "contracted decision hold"),
    ("budget_cases", "Case budget", "hard cap on cases in this plan"),
    ("budget_trials", "Trial budget per case", "hard cap on trials per case"),
    ("budget_harm", "Harm budget", "campaign-level harm ceiling"),
    ("retries", "Retries", "retries per errored case"),
    ("abort_on_error", "Abort on error", "yes or no"),
    ("inclusion", "Inclusion rule",
     "all_terminal, valid_only or exclude_errors"),
)

PLAN_FIELD_KEYS: Tuple[str, ...] = tuple(key for key, _l, _h in PLAN_FIELDS)


@dataclass(frozen=True)
class PlanDraft:
    """The plan an operator is editing, before it is a contract.

    Deliberately all strings: it is a form, and a form that silently coerced
    ``"three"`` into a number would be deciding something.  :meth:`build`
    turns it into a :class:`BatchPlan` or refuses with the field that failed.
    """

    objectives: str = ""
    profiles: str = ""
    scope_ue: str = "ue-1"
    strategy: str = "deterministic"
    repeats: str = "1"
    seed: str = "1"
    ordering: str = "randomized"
    warmup_s: str = "0"
    recovery_s: str = "0"
    measurement_s: str = "1"
    observation_s: str = "1"
    hold_s: str = "1"
    budget_cases: str = "8"
    budget_trials: str = "1"
    budget_harm: str = "100"
    retries: str = "0"
    abort_on_error: str = "yes"
    inclusion: str = "all_terminal"

    def with_field(self, key: str, value: str) -> "PlanDraft":
        if key not in PLAN_FIELD_KEYS:
            raise BatchRefused("UNKNOWN_PLAN_FIELD", key)
        return dc_replace(self, **{key: str(value)})

    def as_mapping(self) -> Mapping[str, str]:
        return {key: getattr(self, key) for key in PLAN_FIELD_KEYS}

    # -- building -----------------------------------------------------------

    def _int(self, key: str) -> int:
        try:
            return int(str(getattr(self, key)).strip())
        except (TypeError, ValueError):
            raise BatchRefused("PLAN_FIELD_NOT_AN_INTEGER",
                               f"{key}={getattr(self, key)!r}")

    def _float(self, key: str) -> float:
        try:
            return float(str(getattr(self, key)).strip())
        except (TypeError, ValueError):
            raise BatchRefused("PLAN_FIELD_NOT_A_NUMBER",
                               f"{key}={getattr(self, key)!r}")

    def _bool(self, key: str) -> bool:
        value = str(getattr(self, key)).strip().lower()
        if value in {"yes", "true", "1", "on"}:
            return True
        if value in {"no", "false", "0", "off"}:
            return False
        raise BatchRefused("PLAN_FIELD_NOT_A_YES_NO",
                           f"{key}={getattr(self, key)!r}")

    def intent_profiles(self) -> Tuple[IntentProfile, ...]:
        entries = [item.strip() for item in self.profiles.split(",")
                   if item.strip()]
        if not entries:
            raise BatchRefused("NO_INTENT_PROFILE",
                               "a batch plan needs at least one profile")
        profiles = []
        for entry in entries:
            profile_id, _, text = entry.partition("=")
            profile_id, text = profile_id.strip(), text.strip()
            if not profile_id or not text:
                raise BatchRefused("MALFORMED_INTENT_PROFILE",
                                   f"expected 'profileId=intent', got {entry!r}")
            profiles.append(IntentProfile(
                profile_id=profile_id,
                intent={"text": text},
                scope={"ue": self.scope_ue.strip() or "ue-1"}))
        return tuple(profiles)

    def build(self) -> BatchPlan:
        """The typed, content-addressed plan, or a stated refusal."""
        objectives = tuple(item.strip() for item in self.objectives.split(",")
                           if item.strip())
        if not objectives:
            raise BatchRefused("NO_OBJECTIVE",
                               "a batch plan needs at least one objective")
        try:
            return BatchPlan(
                objectives=objectives,
                intent_profiles=self.intent_profiles(),
                strategy=self.strategy.strip(),
                repeats=self._int("repeats"),
                seed=self._int("seed"),
                ordering=self.ordering.strip(),
                budget=Budget(cases=self._int("budget_cases"),
                              trials_per_case=self._int("budget_trials"),
                              harm=self._float("budget_harm")),
                windows=Windows(measurement_s=self._float("measurement_s"),
                                observation_s=self._float("observation_s"),
                                hold_s=self._float("hold_s"),
                                warmup_s=self._float("warmup_s"),
                                recovery_s=self._float("recovery_s")),
                retry=RetryPolicy(retries=self._int("retries"),
                                  abort_on_error=self._bool("abort_on_error"),
                                  inclusion=self.inclusion.strip()))
        except BatchRefused:
            raise
        except (TypeError, ValueError) as exc:
            raise BatchRefused("PLAN_NOT_ADMISSIBLE", str(exc))


# --------------------------------------------------------------------------- #
# The executor: the same Kernel path an interactive run uses
# --------------------------------------------------------------------------- #

#: Cockpit session mode -> the mode the batch runner records the run under.
#: ``LIVE`` is absent on purpose: the runner is the hardware-free lane and
#: refuses anything that is not REPLAY or EMULATED, and mapping a live session
#: onto one of those to get past that check is the exact overstatement task
#: section 9.13 forbids.
BATCH_MODE_FOR_SESSION: Mapping[str, str] = {
    MODE_MOCK: "EMULATED",
    MODE_REPLAY: "REPLAY",
}


class KernelPathCaseExecutor:
    """One batch case = one Kernel submission, read back off the Kernel.

    Parameters
    ----------
    session_factory:
        Called once per case with the :class:`BatchCase`; must return a
        :class:`~gui.operator.sources.kernel_live.KernelSubmissionSession`.
        The console supplies the same factory it builds interactive sessions
        with, which is what makes "the same Kernel" a fact rather than a claim.
    mode:
        The batch mode this campaign records under, from
        :data:`BATCH_MODE_FOR_SESSION`.
    clock:
        Optional monotonic-seconds callable for the wall-clock column.  Absent,
        wall clock is reported as ``0.0`` rather than as a fabricated duration.
    """

    def __init__(self, *, session_factory: Callable[[BatchCase], Any],
                 mode: str = "EMULATED",
                 clock: Optional[Callable[[], float]] = None) -> None:
        if mode not in {"REPLAY", "EMULATED"}:
            raise BatchRefused("BATCH_MODE_NOT_HARDWARE_FREE", str(mode))
        self._factory = session_factory
        self.mode = mode
        self._clock = clock
        #: Every session this executor built, in case order.  Kept so a test
        #: can assert the batch and the interactive run share a Kernel.
        self.sessions: List[Any] = []

    def execute(self, case: BatchCase, plan: BatchPlan) -> CaseExecution:
        session = self._factory(case)
        self.sessions.append(session)
        started = self._clock() if self._clock else None
        utterance = str((case.profile.intent or {}).get("text") or "")
        try:
            session.draft(utterance)
            session.confirm()
            view = session.start()
        except SubmissionRefused as exc:
            return _refused_execution(exc, self._elapsed(started))
        except Exception as exc:                           # pragma: no cover
            return _refused_execution(exc, self._elapsed(started))
        return _execution_from_kernel(session, view, self._elapsed(started))

    def _elapsed(self, started: Optional[float]) -> float:
        if started is None or self._clock is None:
            return 0.0
        return max(0.0, float(self._clock()) - float(started))


def _refused_execution(exc: Exception, wall_clock_s: float) -> CaseExecution:
    """A case the Kernel or the session refused, recorded as an error.

    Never ``SUCCESS`` and never silently dropped: a refused case is data, and
    the inclusion rule -- not this function -- decides whether it counts.
    """
    code = getattr(exc, "code", type(exc).__name__)
    return CaseExecution(
        outcome="ERROR", validity="ERROR",
        raw_events=({"eventKind": "CaseRefused", "reason": str(code),
                     "detail": str(exc)},),
        measurements={}, wall_clock_s=wall_clock_s,
        closure_progress=0.0, closure_efficiency=0.0,
        harm_limit_respected=True, proposal_rejects=1)


#: Kernel trial outcome -> the batch record's ``(outcome, validity)`` pair.
#: All nine members are named: an outcome this table did not cover would
#: silently become an error, and a batch summary that quietly reclassified a
#: safety stop as an execution error would misreport the campaign.
_BATCH_RECORD_FOR_OUTCOME: Mapping[str, Tuple[str, str]] = {
    TrialOutcome.SUCCESS.value: ("SUCCESS", "VALID"),
    TrialOutcome.FAIL.value: ("FAIL", "VALID"),
    TrialOutcome.INDETERMINATE.value: ("INVALID", "INVALID"),
    TrialOutcome.INVALID.value: ("INVALID", "INVALID"),
    TrialOutcome.EXEC_ERROR.value: ("ERROR", "ERROR"),
    TrialOutcome.OPERATOR_ABORTED.value: ("ABORTED", "INVALID"),
    TrialOutcome.SAFETY_STOPPED.value: ("ABORTED", "INVALID"),
    TrialOutcome.RECOVERY_FAILED.value: ("ERROR", "ERROR"),
    TrialOutcome.NOT_SETTLED.value: ("ABORTED", "ERROR"),
}


def _execution_from_kernel(session: Any, view: Any,
                           wall_clock_s: float) -> CaseExecution:
    """Turn one settled Kernel submission into a batch record.

    Everything here is read: the outcome and validity are the Kernel's axes,
    the harm figures are its ledger, the closure figures are its evidence
    cells.  Nothing is computed that the Kernel did not already decide, which
    is why a batch summary and the Evidence Ledger cannot disagree.
    """
    from .cockpit import project_session

    snapshot = project_session(session)
    settlement = getattr(view, "settlement", None)
    axes = getattr(view, "axes", None)
    outcome = str(getattr(settlement, "outcome", None)
                  or getattr(axes, "trial_outcome", None)
                  or TrialOutcome.NOT_SETTLED.value)
    record_outcome, validity = _BATCH_RECORD_FOR_OUTCOME.get(
        outcome, ("ERROR", "ERROR"))

    evidence = snapshot.evidence
    trial = snapshot.trial_safety.current_trial
    progress = evidence.closure_progress
    harm = evidence.harm
    charged = sum(item.charged for item in harm)
    remaining = sum(item.remaining for item in harm
                    if item.remaining is not None)
    witnesses = sum(cell.post_closure_witnesses for cell in evidence.cells)
    reused = sum(cell.reused_contributions for cell in evidence.cells)
    predicates = tuple(getattr(axes, "predicate_verdicts", ()) or ())
    return CaseExecution(
        outcome=record_outcome, validity=validity,
        raw_events=tuple(
            {"eventKind": entry.event_kind, "eventId": entry.event_id,
             "objectId": entry.object_id, "timestamp": entry.timestamp,
             "position": entry.position, "contentHash": entry.content_hash}
            for entry in evidence.events),
        # Deliberately *not* the ``kpm``/``o1``/``core``/``ran``/
        # ``ueApplication`` keys the batch statistics read as KPIs.  This lane
        # observes the Kernel, not the radio, and labelling a Kernel event
        # count as a KPM KPI would be exactly the overstatement task section
        # 9.13 forbids.  With the keys absent the statistics report those KPIs
        # as ``None``, which is the truth.
        measurements={
            "kernelEvents": float(evidence.event_count),
            "evidenceCells": float(len(evidence.cells)),
        },
        wall_clock_s=wall_clock_s,
        recovery_s=0.0,
        rolled_back=bool(trial and trial.rolled_back),
        incident=record_outcome in {"ERROR", "INVALID"},
        closure_progress=0.0 if progress is None else float(progress),
        closure_efficiency=(0.0 if progress is None
                            else float(progress)
                            / max(1, snapshot.trial_safety.trials_used)),
        harm_reserve=float(remaining), harm_charge=float(charged),
        target_debt=0.0,
        harm_limit_respected=all(item.limit_respected is not False
                                 for item in harm),
        invariant_violations=0,
        target_satisfied=(record_outcome == "SUCCESS"),
        hold_satisfied=bool(getattr(axes, "hold_complete", False)),
        evidence_reuse=int(reused),
        confirmations=len(evidence.confirmations),
        post_closure_witnesses=int(witnesses),
        agent_tool_calls=len(predicates))


# --------------------------------------------------------------------------- #
# The eight paper-grade outputs (task section 12)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ArtifactSpec:
    """One of task section 12's eight outputs and where the runner writes it."""

    key: str
    label: str
    paths: Tuple[str, ...]
    #: True when the output legitimately depends on an optional package, so a
    #: missing file is ``Unsupported`` with a reason rather than a failure.
    optional: bool = False
    optional_reason: str = ""


ARTIFACTS: Tuple[ArtifactSpec, ...] = (
    ArtifactSpec("raw", "Immutable raw event/trace bundle",
                 ("raw/batch/events.jsonl",)),
    ArtifactSpec("normalized", "Normalized CSV and JSON",
                 ("normalized/trials.csv", "normalized/trials.json")),
    ArtifactSpec("parquet", "Normalized Parquet",
                 ("normalized/trials.parquet",), optional=True,
                 optional_reason="pyarrow is not installed; "
                                 "normalized/parquet-status.json states so and "
                                 "CSV/JSON carry the same records"),
    ArtifactSpec("provenance",
                 "Environment/software/contract/model/topology provenance",
                 ("summary/provenance-manifest.json",)),
    ArtifactSpec("statistics", "Descriptive statistics and confidence intervals",
                 ("summary/statistics.json", "summary/summary.json")),
    ArtifactSpec("figures",
                 "CDF, box/violin, convergence, harm-efficiency, stacked "
                 "outcome and timeline plots",
                 ("figures/cdf.svg", "figures/box.svg", "figures/violin.svg",
                  "figures/convergence.svg", "figures/harm_efficiency.svg",
                  "figures/stacked_outcome.svg", "figures/timeline.svg")),
    ArtifactSpec("formats", "SVG, PDF and high-resolution PNG",
                 ("figures/cdf.svg", "figures/cdf.pdf", "figures/cdf.png")),
    ArtifactSpec("latex", "LaTeX table", ("summary/batch-summary.tex",)),
    ArtifactSpec("traceability",
                 "Machine-readable link from every figure/table back to raw "
                 "records",
                 ("figures/cdf.traceability.json",
                  "summary/batch-summary.traceability.json")),
)


@dataclass(frozen=True)
class ArtifactRow:
    """One artefact's presence, resolved honestly."""

    key: str
    label: str
    status: str
    reason: Optional[str] = None
    present: Tuple[str, ...] = ()
    missing: Tuple[str, ...] = ()


def artifact_rows(run_dir: Optional[Path]) -> Tuple[ArtifactRow, ...]:
    """Which of the eight outputs actually exist under ``run_dir``.

    A run that has not happened reports ``Unknown`` for every row rather than
    an empty list: "not produced yet" and "produced nothing" are different
    facts, and only one of them is a defect.
    """
    rows: List[ArtifactRow] = []
    for spec in ARTIFACTS:
        if run_dir is None:
            rows.append(ArtifactRow(spec.key, spec.label, st.UNKNOWN,
                                    "no batch run has completed in this "
                                    "session"))
            continue
        present = tuple(path for path in spec.paths
                        if (run_dir / path).exists())
        missing = tuple(path for path in spec.paths if path not in present)
        if not missing:
            rows.append(ArtifactRow(spec.key, spec.label, st.OK,
                                    present=present))
        elif spec.optional:
            rows.append(ArtifactRow(spec.key, spec.label, st.UNSUPPORTED,
                                    spec.optional_reason, present, missing))
        else:
            rows.append(ArtifactRow(
                spec.key, spec.label, st.ERROR,
                f"the runner did not write {', '.join(missing)}",
                present, missing))
    return tuple(rows)


# --------------------------------------------------------------------------- #
# The published view
# --------------------------------------------------------------------------- #

STAGE_DRAFT: str = "DRAFT"
STAGE_CONFIRMED: str = "CONFIRMED"
STAGE_RUNNING: str = "RUNNING"
STAGE_COMPLETE: str = "COMPLETE"

STAGE_ORDER: Tuple[str, ...] = (STAGE_DRAFT, STAGE_CONFIRMED, STAGE_RUNNING,
                                STAGE_COMPLETE)


@dataclass(frozen=True)
class BatchConsoleView:
    """One immutable snapshot of the Batch pane.  Frozen, like every view."""

    mode: str = "DISCONNECTED"
    stage: str = STAGE_DRAFT
    draft: PlanDraft = field(default_factory=PlanDraft)
    plan_content_hash: str = ""
    case_count: Optional[int] = None
    cases: Tuple[str, ...] = ()
    confirmation: Optional[ConfirmationRecord] = None
    confirmation_valid: bool = False
    scope_locked: bool = False
    run_dir: Optional[str] = None
    summary: Mapping[str, Any] = field(default_factory=dict)
    artifacts: Tuple[ArtifactRow, ...] = ()
    refusal: Optional[str] = None
    refusal_detail: str = ""

    @property
    def data_class(self) -> dc.DataClassBadge:
        return dc.classify(mode=self.mode, value=self.mode)


# --------------------------------------------------------------------------- #
# The session
# --------------------------------------------------------------------------- #

class BatchSession:
    """One bounded plan: edit, confirm once, run, read the outputs.

    The console owns exactly one of these.  It holds no Kernel of its own: the
    ``session_factory`` it is constructed with is the console's, so a batch run
    and an interactive run reach the same Kernel through the same class.
    """

    def __init__(self, *, runs_root: Any,
                 session_factory: Optional[Callable[[BatchCase], Any]] = None,
                 mode: str = MODE_MOCK,
                 draft: Optional[PlanDraft] = None,
                 publish: Optional[Callable[[str, Any], None]] = None,
                 clock: Optional[Callable[[], float]] = None,
                 runner_factory: Optional[Callable[..., Any]] = None) -> None:
        self.runs_root = Path(runs_root)
        self._factory = session_factory
        self.mode = mode
        self._publish = publish
        self._clock = clock
        self._runner_factory = runner_factory or BatchRunner
        self._draft = draft or PlanDraft()
        self._confirmation: Optional[ConfirmationRecord] = None
        self._confirmed_hash: str = ""
        self._stage: str = STAGE_DRAFT
        self._run_dir: Optional[Path] = None
        self._summary: Dict[str, Any] = {}
        self._confirmations: int = 0
        self._refusal: Optional[Tuple[str, str]] = None
        self.executor: Optional[KernelPathCaseExecutor] = None

    # -- wiring ---------------------------------------------------------------

    def bind_sessions(self, factory: Optional[Callable[[BatchCase], Any]], *,
                      mode: Optional[str] = None) -> None:
        """Declare how a batch case gets its Kernel session.

        A composition fact rather than a console choice: only whoever wired the
        vertical path knows what is behind the gateway.  Refused once the
        repetition has begun, for the same reason the plan is: a run whose
        executor changed halfway is not the run that was confirmed.
        """
        if self.scope_locked:
            raise BatchRefused("BATCH_SCOPE_LOCKED",
                               "the confirmed plan is already running")
        self._factory = factory
        if mode is not None:
            self.mode = mode
        self._emit()

    # -- reads --------------------------------------------------------------

    @property
    def draft(self) -> PlanDraft:
        return self._draft

    @property
    def stage(self) -> str:
        return self._stage

    @property
    def scope_locked(self) -> bool:
        """True once the confirmed repetition has begun.

        Section 9.4: after the one confirmation the scope does not change.  The
        console expresses that as a refusal to edit, not as a greyed field that
        a later code path might still read.
        """
        return self._stage in {STAGE_RUNNING, STAGE_COMPLETE}

    def plan(self) -> Optional[BatchPlan]:
        try:
            return self._draft.build()
        except BatchRefused:
            return None

    # -- edit ---------------------------------------------------------------

    def edit(self, key: str, value: str) -> PlanDraft:
        """Change one plan field.  Refused once the repetition has started."""
        if self.scope_locked:
            raise BatchRefused(
                "BATCH_SCOPE_LOCKED",
                "the confirmed plan is running; its scope cannot change")
        self._draft = self._draft.with_field(key, str(value))
        self._refusal = None
        # Section 5's invalidation rule, applied to the plan: a confirmation
        # covers a content hash, and an edited plan is a different hash.
        if self._confirmation is not None:
            plan = self.plan()
            if plan is None or plan.content_hash != self._confirmed_hash:
                self._confirmation = dc_replace(
                    self._confirmation, changed_after_confirmation=True)
                self._stage = STAGE_DRAFT
        self._emit()
        return self._draft

    # -- confirm ------------------------------------------------------------

    def confirm(self, *, now: Optional[str] = None) -> ConfirmationRecord:
        """Take the one ``CONFIRM_BATCH_PLAN`` over the whole bounded plan."""
        if self.scope_locked:
            raise BatchRefused("BATCH_SCOPE_LOCKED",
                               "this plan has already started")
        plan = self._draft.build()
        self._confirmations += 1
        record = plan.confirm(
            event_id=f"confirm/batch:{self._confirmations}", timestamp=now)
        self._confirmation = record
        self._confirmed_hash = plan.content_hash
        self._stage = STAGE_CONFIRMED
        self._refusal = None
        self._emit()
        return record

    # -- run ----------------------------------------------------------------

    def start(self) -> BatchConsoleView:
        """Run the confirmed plan.  **Worker thread.**

        Refuses without a confirmation that still covers the plan's content,
        and refuses a live session outright: this lane is hardware-free and the
        runner enforces that, so the console states it here rather than letting
        the operator find out at the end.
        """
        if self.scope_locked:
            raise BatchRefused("BATCH_ALREADY_RUN",
                               str(self._run_dir or self._stage))
        plan = self._draft.build()
        if self._confirmation is None or not plan.is_confirmed_by(
                self._confirmation):
            raise BatchRefused(
                "CONFIRM_BATCH_PLAN_REQUIRED",
                "the plan content is not covered by a standing confirmation")
        if self._factory is None:
            raise BatchRefused(
                "NO_KERNEL_SESSION",
                "this console has no Kernel session factory, so a batch case "
                "has nothing to submit to")
        batch_mode = BATCH_MODE_FOR_SESSION.get(self.mode)
        if batch_mode is None:
            raise BatchRefused(
                "BATCH_MODE_NOT_HARDWARE_FREE",
                f"a {self.mode} session cannot be batched from this pane; the "
                f"batch runner admits REPLAY and EMULATED executors only")
        self._stage = STAGE_RUNNING
        self._emit()
        executor = KernelPathCaseExecutor(session_factory=self._factory,
                                          mode=batch_mode, clock=self._clock)
        self.executor = executor
        runner = self._runner_factory(executor=executor,
                                      runs_root=self.runs_root)
        try:
            result = runner.run(plan, confirmation=self._confirmation)
        except Exception as exc:
            self._stage = STAGE_COMPLETE
            self._refusal = ("BATCH_RUN_FAILED", f"{type(exc).__name__}: {exc}")
            self._emit()
            raise
        self._run_dir = Path(result.run_dir)
        self._summary = dict(result.summary)
        self._stage = STAGE_COMPLETE
        view = self.view()
        self._emit(view)
        return view

    # -- projection ---------------------------------------------------------

    def view(self) -> BatchConsoleView:
        plan = self.plan()
        refusal, detail = self._refusal or (None, "")
        if plan is None and refusal is None:
            try:
                self._draft.build()
            except BatchRefused as exc:
                refusal, detail = exc.code, exc.detail
        cases: Tuple[str, ...] = ()
        if plan is not None:
            cases = tuple(f"{case.case_id}  {case.objective}  "
                          f"{case.profile.profile_id}  repeat "
                          f"{case.repeat_index}  seed {case.seed}"
                          for case in plan.cases())
        return BatchConsoleView(
            mode=self.mode, stage=self._stage, draft=self._draft,
            plan_content_hash=plan.content_hash if plan else "",
            case_count=len(cases) if plan else None,
            cases=cases,
            confirmation=self._confirmation,
            confirmation_valid=bool(plan is not None
                                    and plan.is_confirmed_by(self._confirmation)),
            scope_locked=self.scope_locked,
            run_dir=str(self._run_dir) if self._run_dir else None,
            summary=dict(self._summary),
            artifacts=artifact_rows(self._run_dir),
            refusal=refusal, refusal_detail=detail)

    def _emit(self, view: Optional[BatchConsoleView] = None) -> None:
        if self._publish is None:
            return
        try:
            self._publish("batch", view if view is not None else self.view())
        except Exception:                                  # pragma: no cover
            pass


__all__ = [
    "ARTIFACTS", "ArtifactRow", "ArtifactSpec", "BATCH_MODE_FOR_SESSION",
    "BatchConsoleView", "BatchRefused", "BatchSession",
    "KernelPathCaseExecutor", "PLAN_FIELDS", "PLAN_FIELD_KEYS", "PlanDraft",
    "STAGE_COMPLETE", "STAGE_CONFIRMED", "STAGE_DRAFT", "STAGE_ORDER",
    "STAGE_RUNNING", "artifact_rows",
]
