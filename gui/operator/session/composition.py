"""The Live composition: one deployment, one proposer, one write path.

This is the module that makes a Live session mean something.  The console
already had every read-side projection it needed; what no track owned was the
line from *the operator pressed Submit* to *the preserved three-stage
coordinator ran once against a named deployment and its result came back as
evidence*.  That line is here.

Three rules shape it, and each of them is a test:

**One runtime, not two.**  A submission enters the episode entry the headless
rApp uses, and nothing in this package re-implements a decision.  The GUI
cannot run a shortened coordinator because it has no way to call one.

**And on the deployed build, not even that one.**  The cutover made this
composition *history-only*: it can no longer reach the preserved runtime by
importing it.  Everything that runtime owns - the integration loader, the
episode entry, the contract validator - arrives as ``episode_support``, handed
in by ``tools.legacy.episode_support`` through
:class:`~gui.operator.app.OperatorConsole`.  A console built by ``main.py``
holds ``None`` there, so a Live session on the deployed build is a Kernel
session and this class is unreachable from it.

**One episode per submission.**  A submission that arrives while one is in
flight is refused with a reason, not queued into a second coordinator run and
not silently dropped.  ``processIntentCalls`` on the rApp adapter is the
independent witness for that, and the flow test reads it.

**Provenance is fixed at submit.**  The proposer in use is captured when the
episode starts; a model change made while it runs is recorded and applied to the
*next* episode, because rewriting the provenance of an episode that already ran
would falsify the record it produced.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger("gui.operator.session.composition")

#: Why a withdrawal button stays disabled.  The upper contract has no intent
#: withdrawal operation: a policy leaves through its own lifecycle, driven by
#: the deployment, and a console button that deleted a row locally would claim
#: an effect it did not have.
WITHDRAWAL_UNSUPPORTED = (
    "the upper contract exposes no intent-withdrawal operation; a policy is "
    "removed through its own lifecycle by the deployment that owns it "
    "[GAP-04]")

#: Why every legacy-episode control refuses on the deployed build.  It names
#: the replacement rather than only stating the absence: a Live session here is
#: a Kernel session, attached by the deployment's composition root.
NO_LEGACY_EPISODE = (
    "this build has no legacy episode runtime: the deployed Live path is the "
    "Assurance Kernel behind the Write Gateway, attached with "
    "attach_kernel_session by the deployment's composition root. The preserved "
    "pre-Kernel Coordinator is history-only and is reached through "
    "tools.legacy.coordinator_console")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class SubmissionRefused(RuntimeError):
    """A submission that must not become an episode, with the reason.

    Raised rather than returned: a refusal that a caller can ignore is a
    refusal that eventually gets ignored.
    """

    @property
    def reason(self) -> str:
        return str(self)


@dataclass
class LiveComposition:
    """A resolved deployment plus the proposer that will reason about it.

    Construction performs no I/O beyond what ``LiveIntegration.load`` already
    validated: this object is a binding, not a connection.  Nothing here opens a
    session, and the console still refuses to start one until Preflight has seen
    a reachable R1 transport.
    """

    integration: Any
    llm_manager: Any = None
    coordinator: Any = None
    #: The preserved episode runtime, or ``None``.  ``None`` is the deployed
    #: build, and then :meth:`submit` refuses by name: this class holds no
    #: import of that runtime and cannot go and get one.
    episode_support: Any = None
    evidence_records: Tuple[Mapping[str, Any], ...] = ()
    #: The episode entry.  ``None`` means the one ``episode_support`` names,
    #: which is the only entry a console ever uses.  A verification driver may
    #: wrap it to *observe* how many times it was entered - it cannot replace
    #: what it does, because the wrapper still has to call the real entry to
    #: return a result.
    episode_runner: Optional[Callable[..., Mapping[str, Any]]] = None
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _in_flight: Optional[str] = field(default=None, repr=False)
    _pending_backend: Optional[str] = field(default=None, repr=False)
    _submissions: int = field(default=0, repr=False)

    # -- proposer ----------------------------------------------------------- #

    @property
    def registry(self) -> Any:
        from ..sources.llm_registry import LlmRegistry

        owner = self.coordinator if self.coordinator is not None else self.llm_manager
        if owner is None:
            raise SubmissionRefused("no LLM backend manager is attached")
        return LlmRegistry(owner)

    def backend_views(self) -> Tuple[Any, ...]:
        try:
            return self.registry.views()
        except Exception:
            logger.debug("backend projection failed", exc_info=True)
            return ()

    def active_backend(self) -> Optional[str]:
        manager = self.llm_manager
        try:
            return manager.active_backend_name() if manager is not None else None
        except Exception:
            return None

    def backend_names(self) -> Tuple[str, ...]:
        manager = self.llm_manager
        if manager is None:
            return ()
        try:
            return tuple(str(name) for name in manager.get_available_names())
        except Exception:
            logger.debug("backend enumeration failed", exc_info=True)
            return ()

    def refresh_backends(self) -> Tuple[str, ...]:
        """Re-run the existing manager-owned discovery.  Worker thread."""
        return tuple(self.registry.refresh())

    def select_backend(self, name: str) -> Tuple[bool, str]:
        """Choose the proposer for the next episode.

        Returns ``(applied, message)``.  While an episode is in flight the
        choice is remembered and applied at the next submission instead of being
        applied underneath a running episode, whose recorded provenance would
        then no longer describe the model that produced it.
        """
        wanted = str(name or "").strip()
        if not wanted:
            return False, "no backend was selected"
        with self._lock:
            if self._in_flight is not None:
                self._pending_backend = wanted
                return False, (
                    f"episode {self._in_flight} is running; {wanted} will be "
                    "used from the next intent and this episode keeps the "
                    "provenance it started with")
        return self._apply_backend(wanted)

    def _apply_backend(self, name: str) -> Tuple[bool, str]:
        try:
            ok = bool(self.registry.select(name))
        except Exception as exc:
            return False, f"{name} was not selected: {type(exc).__name__}: {exc}"
        return (ok, f"{name} is the proposer for the next intent" if ok
                else f"{name} was refused by the backend manager")

    # -- preflight and status ------------------------------------------------ #

    def preflight_kwargs(self, *, runs_root: Optional[str] = None) -> Dict[str, Any]:
        """Everything Preflight needs to check this deployment, read-only."""
        return {
            "r1_client": self.integration.r1_client(),
            "capability_manifest": dict(self.integration.capability_manifest),
            "llm_backend_names": self.backend_names(),
            "runs_root": runs_root,
        }

    def identity(self) -> Dict[str, Any]:
        return dict(self.integration.identity())

    # -- the write path ------------------------------------------------------ #

    def submit(self, intent_text: str, *, policy_context: Mapping[str, Any],
               run_id: Optional[str] = None,
               present: Optional[Callable[[Mapping[str, Any]], None]] = None,
               runner: Optional[Callable[..., Mapping[str, Any]]] = None,
               now: Optional[datetime] = None) -> Any:
        """Run exactly one authoritative episode.  Worker thread.

        The contract intent identity is minted here, per submission, and carried
        in the policy context beside the correlation id the deployment owns.
        That is a producer's own identifier - it is not a measurement, and it is
        recorded next to the coordinator's internal id so a reader can join the
        two records rather than guess.
        """
        from ..sources.live import IntentSubmission, submit_intent

        text = str(intent_text or "").strip()
        if not text:
            raise SubmissionRefused("no intent text was entered")
        episode_runner = (runner if runner is not None else self.episode_runner)
        if episode_runner is None:
            support = self.episode_support
            if support is None:
                raise SubmissionRefused(NO_LEGACY_EPISODE)
            episode_runner = support.episode_runner()
        intent_id = str(uuid.uuid4())
        with self._lock:
            if self._in_flight is not None:
                raise SubmissionRefused(
                    f"intent {self._in_flight} is still running; a second "
                    "episode would be a second coordinator run for one "
                    "operator action")
            pending = self._pending_backend
            self._pending_backend = None
            self._in_flight = intent_id
            self._submissions += 1
        if pending:
            self._apply_backend(pending)
        proposer = self.active_backend()
        context = {**dict(policy_context), "intentId": intent_id}
        try:
            request = self.integration.episode_request(
                intent_text=text, policy_context=context,
                identifiers={"run_id": run_id} if run_id else {},
                evidence_records=self.evidence_records)
            submission = IntentSubmission(
                integration_path=self.integration.integration_path,
                request=request,
                target_scope=_scope_label(context),
                objective=str(context.get("objectiveKind") or "unknown"),
                constraints=(f"policyRevision={context.get('policyRevision')}",
                             f"validity {context.get('notBefore')} .. "
                             f"{context.get('expiresAt')}"),
                policy_revision=str(context.get("policyRevision") or "1"))
            result = submit_intent(
                submission, present=present, runner=episode_runner,
                validate_intent=getattr(self.episode_support,
                                        "validate_intent_parse", None),
                **self.integration.runner_kwargs(
                    llm_manager=self.llm_manager, now=now))
        finally:
            with self._lock:
                self._in_flight = None
        return LiveEpisode(result=result, intent_id=intent_id,
                           proposer_at_submit=proposer,
                           policy_context=context,
                           identity=self.identity(),
                           episode_support=self.episode_support)

    @property
    def in_flight(self) -> Optional[str]:
        with self._lock:
            return self._in_flight

    @property
    def submissions(self) -> int:
        with self._lock:
            return self._submissions


@dataclass(frozen=True)
class LiveEpisode:
    """One completed episode, with the identity that produced it."""

    result: Any
    intent_id: str
    proposer_at_submit: Optional[str]
    policy_context: Mapping[str, Any]
    identity: Mapping[str, Any]
    #: The runtime that produced this episode, carried so ``r1_outbound`` can
    #: re-check the dispatched policy with that runtime's own validator rather
    #: than importing one.  ``None`` leaves ``contractValid`` unknown, which is
    #: the honest answer when nothing here can check it.
    episode_support: Any = None

    @property
    def authoritative(self) -> Mapping[str, Any]:
        return self.result.authoritative

    @property
    def decision(self) -> Any:
        return self.result.decision

    @property
    def intent_row(self) -> Any:
        return self.result.intent

    def trial(self) -> Mapping[str, Any]:
        """The R1 trial the episode produced, or an empty mapping."""
        data = dict(self.authoritative)
        cycles = data.get("cycles") or []
        for cycle in reversed(list(cycles)):
            trial = (cycle or {}).get("profile_trial")
            if isinstance(trial, Mapping) and trial:
                return trial
        trial = data.get("profile_trial")
        return trial if isinstance(trial, Mapping) else {}

    def r1_outbound(self) -> Dict[str, Any]:
        """What actually left over R1, and whether it satisfies the contract.

        ``contractValid`` is the frozen policy schema's verdict on the exact
        object the deployment received, re-checked here rather than assumed from
        the fact that the request returned 201.  ``policyId`` present with no
        object would be a half-observation, so both are read from the same
        trial record or neither is reported.
        """
        from ..sources.live import POLICY_TYPE

        support = self.episode_support
        trial = self.trial()
        policy = trial.get("policy") if isinstance(trial, Mapping) else None
        trace = policy.get("trace") if isinstance(policy, Mapping) else {}
        trace = trace if isinstance(trace, Mapping) else {}
        job = trial.get("dataJob") if isinstance(trial, Mapping) else None
        outbound: Dict[str, Any] = {
            "policyId": trial.get("policyId") if isinstance(trial, Mapping) else None,
            "policyTypeId": POLICY_TYPE if isinstance(policy, Mapping) else None,
            "location": trial.get("location") if isinstance(trial, Mapping) else None,
            "dataJobId": (job or {}).get("dataJobId") if isinstance(job, Mapping) else None,
            "intentId": trace.get("intentId"),
            "correlationId": trace.get("correlationId"),
            "policyRevision": trace.get("policyRevision"),
            "observedAt": _utc_now(),
        }
        if not isinstance(policy, Mapping):
            outbound.update({"contractValid": None,
                             "contractReason": "no policy object was dispatched"})
            return outbound
        if support is None:
            outbound.update({
                "contractValid": None,
                "contractReason": "no episode runtime is attached, so the "
                                  "dispatched policy was not re-validated here"})
            return outbound
        try:
            support.validate_policy(dict(policy),
                                    "AIC_UECellSteering_1.0.0.policy")
            outbound.update({"contractValid": True, "contractReason": None})
        except Exception as exc:
            outbound.update({"contractValid": False,
                             "contractReason": f"{type(exc).__name__}: {exc}"})
        return outbound

    def policy_status(self) -> Mapping[str, Any]:
        trial = self.trial()
        status = trial.get("policy_status") if isinstance(trial, Mapping) else None
        return status if isinstance(status, Mapping) else {}

    def store_records(self) -> Dict[str, Any]:
        """The episode, its cycles and its proposer calls, in store shapes.

        No model text can travel here: the LLM record carries identity, timing
        and the prompt hash the engine already computed, and the store refuses
        anything else.
        """
        from ..store.records import cycle_trace, episode_trace, llm_call_trace

        data = dict(self.authoritative)
        outcome = data.get("terminal_outcome")
        episode_id = str(data.get("episode_id") or f"episode-{self.intent_id[:8]}")
        cycles = list(data.get("cycles") or [])
        episode = episode_trace(
            episode_id=episode_id,
            intent_text=str(data.get("intent_text") or ""),
            terminal_outcome=outcome,
            terminalReason=data.get("terminal_reason"),
            latencyMs=data.get("latency_ms"),
            negotiationRounds=(data.get("nego_stats") or {}).get("rounds"),
            rolledBack=data.get("rolled_back"),
            cycles=len(cycles),
            contractIntentId=self.intent_id,
            coordinatorIntentId=_coordinator_intent_id(data),
            proposerAtSubmit=self.proposer_at_submit,
            integration=dict(self.identity),
            r1Outbound=self.r1_outbound(),
            observedAt=_utc_now())
        cycle_records = []
        llm_records = []
        for index, cycle in enumerate(cycles):
            values = dict(cycle or {})
            cycle_records.append(cycle_trace(
                episode_id=episode_id, cycle_index=index,
                routedTo=values.get("routed_to"),
                feasible=values.get("feasible"),
                rawConfidence=values.get("raw_confidence"),
                calibratedProbability=values.get("calibrated_probability"),
                thetaStar=values.get("theta_star"),
                thresholdAppliedTo=values.get("threshold_applied_to")
                or "calibrated_probability",
                schemaValid=values.get("schema_valid"),
                assuranceDecision=values.get("assurance_decision"),
                profileError=values.get("profile_error"),
                trialSuccess=values.get("trial_success")))
            if values.get("proposal_generated") or values.get("prompt_hash"):
                llm_records.append(llm_call_trace(
                    seq=len(llm_records), stage="FEASIBILITY",
                    backend_name=str(values.get("proposer_id")
                                     or self.proposer_at_submit or "unknown"),
                    tUtc=_utc_now(), episodeId=episode_id,
                    cycleId=values.get("cycle_id"),
                    modelVersion=values.get("model_version"),
                    promptHash=values.get("prompt_hash"),
                    latencyMs=values.get("inference_ms"),
                    schemaValid=values.get("schema_valid"),
                    success=values.get("proposal_generated")))
        return {"episode": episode, "cycles": cycle_records,
                "llmCalls": llm_records}


def _coordinator_intent_id(data: Mapping[str, Any]) -> Optional[str]:
    cycles = data.get("cycles") or []
    for cycle in cycles:
        value = (cycle or {}).get("current_intent_id")
        if value:
            return str(value)
    parsed = data.get("parsed_intent") or {}
    value = parsed.get("id") if isinstance(parsed, Mapping) else None
    return str(value) if value else None


def _scope_label(context: Mapping[str, Any]) -> str:
    """A human-readable scope for the confirmation, from the context itself."""
    scope = context.get("ueId")
    if isinstance(scope, Mapping):
        inner = scope.get("guAmfUeNgapId") or {}
        identity = (inner.get("amfUeNgapId") if isinstance(inner, Mapping)
                    else None)
        if identity is not None:
            return f"UE amfUeNgapId={identity}"
    cells = context.get("allowedCells") or ()
    if isinstance(cells, Sequence) and cells:
        return f"{len(cells)} allowed cell(s)"
    return "declared policy scope"


__all__ = ["LiveComposition", "LiveEpisode", "NO_LEGACY_EPISODE",
           "SubmissionRefused", "WITHDRAWAL_UNSUPPORTED"]
