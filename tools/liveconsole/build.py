"""Compose a **live** Kernel submission session and attach it to the Cockpit.

This is the live sibling of ``tools/hfconsole``: the same Cockpit, the same
``KernelSubmissionSession``, the same epoch freeze and trial state machine --
but the Write Gateway's adapter is the real ``R1Adapter`` and the effect
travels the only path an objective effect may travel,
``R1 -> Non-RT RIC -> A1-P -> xApp -> FlexRIC -> E2SM-RC -> OAI gNB``
(``docs/architecture/FINAL-ARCHITECTURE.md`` section 4.2).

The order is ``tools/g3ota/run_ota.py``'s, because that order is the finding of
three failed integrations rather than a preference:

1. resolve the deployment from **one** authority, the live profile, and refuse
   by name if a digest moved (:mod:`tools.liveconsole.profile`);
2. observe the UE -- identity, AMF and the cell it is on *right now* -- from the
   live KPM indication stream, because the AMF hands out a new ``amfUeNgapId``
   on every registration;
3. free the UE's A1-P scope, archiving every occupant verbatim first;
4. build the R1 consumer, discover the policy type, wire the vertical path, and
   hand it to the Cockpit's session in ``LIVE`` mode.

One objective per session
-------------------------
``build_live_objective_runtime`` freezes one family's bundle into one epoch and
opens one evidence cell, and ``KernelSubmissionSession.draft`` refuses a
sentence whose family is not in that frozen epoch (Sol F-1).  So this root
composes exactly one steering objective per session and says so; the
multi-objective session factory is a separate work package, not a bigger
``objective_registry`` dict.

Never badging a mock as LIVE
----------------------------
The session mode is **derived**, not declared: :func:`session_mode` returns
``LIVE`` only when this root built every live port itself.  Injecting any port
-- the KPM stream, the R1 transport, the actuation adapter, the clock, the
scope clearer -- yields ``MOCK``, so the hermetic tests that exercise this file
can never produce a session the Cockpit would badge Live.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from assurance.advisors.grammar import (
    IntentGrammarEntry, IntentParseError, parse_utterance, scope_selector)
from assurance.collector.live import build_live_collectors
from assurance.contracts.target import ComparisonOperator
from assurance.core.timebase import parse_utc
from assurance.gateway.live import (
    build_live_r1_supplementary_adapter, scalar_leaf_readback,
)
from assurance.gateway.r1_binding_journal import JsonFileR1BindingJournal
from assurance.gateway.r1_operation_journal import JsonlR1OperationJournal
from assurance.live import KpmUeAttributionReader
from assurance.live.objective_runtime import build_live_objective_runtime
from assurance.live.pin_to_cell_driver import (
    PIN_TO_CELL_GRAMMAR,
    LivePinToCellDeployment,
    LiveTiming,
    LiveUeObservation,
    build_live_pin_to_cell_runtime, kpm_node_nb_id,
)
from assurance.objectives import FAMILY_MODULES
from assurance.objectives.action102_support import (
    ATTENUATION_ACTION_ID,
    CAP_ACTION_ID,
    MCS_ACTION_ID,
    PRIORITY_ACTION_ID,
    CONTROLLED_UE_SCOPE_KEY,
    SUPPLEMENTARY_ACTIONS,
    SupplementaryCapError,
    SupplementaryCapRequest,
    live_cap_admissible,
    supplementary_action,
    supplementary_axis_declarations,
    with_supplementary_cap,
)

from gui.operator.sources.kernel_live import (
    MODE_LIVE,
    MODE_MOCK,
    KernelSubmissionSession,
)

from tools.g3ota.composition import (
    A1P_OBJECTIVE_KIND,
    KpmTail,
    LivePolicyBuilder,
    RecordingPolicyPort,
    WallClockPorts,
    build_policy_type_discovery,
    build_r1_policy_port,
    clear_ue_scope,
    kpm_slot_occupancy,
    live_topology,
    producer_episode,
    scope_occupants,
    TERMINAL_SUCCESS_STATES,
)
from tools.g3ota.objectives import ObjectiveRefused, resolve_submittable_family
from tools.g5ota.objective_live import (
    FAMILY_UTTERANCES,
    bundle_contracts,
    bundle_direction,
    family_grammar,
    family_utterance,
    live_scope,
)

from tools.liveconsole.profile import (
    LIVE_CONSOLE_KEY,
    ActionProducer,
    ActionProducerType,
    LiveConsoleError,
    LiveDeployment,
    load_live_deployment,
)

# The in-repo Campaign 5 half: the policy types the action producer already
# advertises, the builder that turns a gateway command into one of their
# bodies, and the corroborated readback that refuses to call an ACK an effect.
# Imported here, in the composition root, because ``assurance/**`` may not
# import ``oran`` -- the boundary test says so and this file is the seam.
from oran.campaign5 import CAMPAIGN5_FAMILIES, family_by_policy_type
from oran.campaign5.builders import Campaign5BuilderError, make_policy_builder
from oran.campaign5.families import Campaign5Error
from oran.campaign5.errors import A1Error
from oran.rapp.r1_client import R1Refusal
from oran.campaign5.readback import (
    CorroboratedConfigReadback, make_status_projection,
)

__all__ = [
    "GATE3_REGRESSION_CASE",
    "LiveCase",
    "LiveConsoleSession",
    "LiveSession",
    "family_for_utterance",
    "attach_live_session",
    "build_live_session",
    "live_capable_families",
    "observe_selected_ue",
    "operator_utterance",
    "requested_amf_ue_ngap_id",
    "scope_preconditions",
    "session_mode",
    "write_run_evidence",
]

#: The objective this console runs when ``--objective`` is omitted: Gate 3's
#: ``UeCellSteeringPinToCell`` regression case, wired by
#: :func:`~assurance.live.pin_to_cell_driver.build_live_pin_to_cell_runtime`.
#: It is not a family in the Gate 4 registry -- it is the case the deployment
#: was brought up against -- so it is named here rather than looked up.
GATE3_REGRESSION_CASE = "UeCellSteeringPinToCell"

#: The seams a hermetic test may inject.  Each one replaces a live port, so
#: each one is also a reason the composed session is not Live.
INJECTABLE = ("read_new_lines", "policy_port", "adapter_override", "ports",
              "scope_clearer")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def session_mode(injected: Sequence[str] = ()) -> str:
    """``LIVE`` only when this root composed every live port itself.

    Sol's constraint, made structural rather than documented: a session claims
    ``LIVE`` because the Write Gateway wrote to a live deployment, and the only
    party who can say so is whoever built the ports.  If any port was handed in
    -- a scripted KPM stream, a fake transport, a mock adapter, a virtual clock,
    a stubbed scope clearer -- then something in the chain did not reach the
    deployment and the session is ``MOCK``.  ``app.py`` reads the declaration
    and never second-guesses it, so the declaration has to be honest here.
    """
    return MODE_MOCK if tuple(injected) else MODE_LIVE


def requested_amf_ue_ngap_id(
    *, explicit: Optional[int] = None, profile_value: Optional[Any] = None,
    utterance: Optional[str] = None,
) -> Optional[int]:
    """Which UE the operator asked for, from the three places they can say it.

    In precedence order: the command line, the ``ueId=`` scope of the sentence
    they typed, then the profile.  The sentence is read with the deterministic
    grammar's own :func:`~assurance.advisors.grammar.scope_selector`, so the UE
    this root observes and the UE the draft scopes to cannot diverge.

    Every source that states one must state the *same* one; two sources naming
    two UEs is refused rather than resolved by precedence, because a run that
    quietly preferred the flag over the sentence would put one UE in the
    evidence and address another on the radio.
    """
    stated: Dict[str, int] = {}
    if explicit is not None:
        stated["--amf-ue-ngap-id"] = int(explicit)
    if utterance:
        typed = scope_selector(utterance).get("ueId")
        if typed is not None:
            try:
                stated["the typed sentence's ueId="] = int(str(typed))
            except (TypeError, ValueError):
                raise LiveConsoleError(
                    f"the sentence scopes to ueId={typed!r}, which is not an "
                    "amfUeNgapId; this deployment addresses a UE by the "
                    "identifier the AMF published on the KPM stream") from None
    if profile_value is not None:
        try:
            stated[f"{LIVE_CONSOLE_KEY}.amfUeNgapId"] = int(profile_value)
        except (TypeError, ValueError):
            raise LiveConsoleError(
                f"{LIVE_CONSOLE_KEY}.amfUeNgapId is {profile_value!r}, which is "
                "not an integer amfUeNgapId") from None
    if not stated:
        return None
    chosen = set(stated.values())
    if len(chosen) > 1:
        raise LiveConsoleError(
            "the run names more than one UE: "
            + "; ".join(f"{source} {value}" for source, value in sorted(stated.items()))
            + ". One case addresses one UE; say the same one everywhere, or "
              "say it in only one place.")
    return chosen.pop()


def supplementary_clause(
    sentence: str, participants: Sequence[Any]
) -> str:
    """The sentence with the supplementary controls it carries spelled out.

    Appended rather than woven in, and after the objective's own clause, for
    the reason the frozen grammar cares about: it reads the *first* number in a
    sentence as the objective's bound, so a cap value has to come later or it
    would be read as the cell.  The controlled UE is named on its own scope key
    (``controlledUeId=``), which the grammar already carries and which cannot
    displace the objective's ``ueId=``.
    """
    clauses = [
        f"cap controlledUeId={item.request.controlled_ue_scope_id} at "
        f"{int(item.request.candidate_caps[0])} PRB"
        for item in participants if item.action_id == CAP_ACTION_ID
    ]
    return sentence + "".join(f"; {clause}" for clause in clauses)


def requested_controlled_ue(
    *, explicit: Optional[int] = None, profile_value: Optional[Any] = None,
    utterance: Optional[str] = None, objective_ue: Optional[int] = None,
) -> Optional[int]:
    """Which UE a SUPPLEMENTARY control may act on, from the three places.

    The same rule :func:`requested_amf_ue_ngap_id` uses for the objective UE,
    on a *different* scope key.  The sentence names it as ``controlledUeId=``
    and not as a second ``ueId=``: the frozen grammar reads scope tokens into
    one mapping, so a second ``ueId=`` would silently rewrite which UE the
    objective addresses -- the sentence would say one thing and the contract
    another.  Using a key of its own is what lets the sentence carry it with no
    change to the grammar and no change to how an existing sentence reads.

    ``None`` composes no supplementary control.  That is the deliberate
    default: which UE may be capped is a decision about the traffic in the
    cell, and capping whichever UE happened to be second in an inventory is not
    a choice a composition root may make.
    """
    stated: Dict[str, int] = {}
    if explicit is not None:
        stated["--controlled-amf-ue-ngap-id"] = int(explicit)
    if utterance:
        typed = scope_selector(utterance).get("controlledUeId")
        if typed is not None:
            try:
                stated["the typed sentence's controlledUeId="] = int(str(typed))
            except (TypeError, ValueError):
                raise LiveConsoleError(
                    f"the sentence names controlledUeId={typed!r}, which is not "
                    "an amfUeNgapId; this deployment addresses a UE by the "
                    "identifier the AMF published on the KPM stream") from None
    if profile_value is not None:
        try:
            stated[f"{LIVE_CONSOLE_KEY}.controlledUe.amfUeNgapId"] = int(profile_value)
        except (TypeError, ValueError):
            raise LiveConsoleError(
                f"{LIVE_CONSOLE_KEY}.controlledUe.amfUeNgapId is "
                f"{profile_value!r}, which is not an integer amfUeNgapId") from None
    if not stated:
        return None
    chosen = set(stated.values())
    if len(chosen) > 1:
        raise LiveConsoleError(
            "the run names more than one controlled UE: "
            + "; ".join(f"{source} {value}" for source, value in sorted(stated.items()))
            + ". One supplementary control acts on one UE; say the same one "
              "everywhere, or say it in only one place.")
    controlled = chosen.pop()
    if objective_ue is not None and controlled == int(objective_ue):
        raise LiveConsoleError(
            f"the controlled UE and the objective UE are both amfUeNgapId "
            f"{controlled}. A supplementary control acts on a *different*, "
            "heavy, non-target UE: capping the UE the objective protects is "
            "the one composition this contract refuses outright.")
    return controlled


def observe_selected_ue(
    reader: Any, *, now: Callable[[], str], sleep_ms: Callable[[int], None],
    freshness_ms: int, attempts: int = 20,
    amf_ue_ngap_id: Optional[int] = None,
) -> LiveUeObservation:
    """The one UE this case addresses, or a refusal that says why there isn't one.

    Sol's first blocker.  ``observe_live_ue`` answers "the newest fresh
    observation", which for a one-shot Gate 3 episode against a single-UE lab
    was adequate and is now not: this console generates the operator's sentence
    from whatever it observed, so on a two-UE cell it would draft an intent for
    whichever UE happened to be last in the newest indication and then actuate
    that one.  Nothing downstream could notice -- the identity is internally
    consistent all the way to the policy body.

    So the UE is either **named** or **unambiguous**, and there is no third
    case:

    * named (``--amf-ue-ngap-id``, the sentence's ``ueId=``, or the profile) --
      that UE must be observable fresh, or this refuses naming it and listing
      who *is* observable;
    * unnamed -- exactly one distinct UE must be fresh on the stream.  Zero is
      refused as "nothing to address"; two or more is refused as "say which",
      never resolved by recency.

    Every refusal states the freshness window and what the reader actually saw
    -- lines drained, UE-level records parsed, records discarded for a stale
    epoch or an unknown node -- because "no UE is attached" and "the stream is
    stale" need different actions from the operator and look identical from
    the outside.
    """
    fresh: Dict[int, LiveUeObservation] = {}
    for _ in range(max(1, int(attempts))):
        # Deliberately unfiltered: the reader keeps every UE it saw, so a
        # refusal can say who *is* observable rather than only that the one
        # asked for is not.  Selection happens below, on the full history.
        reader.refresh()
        fresh = {}
        instant = now()
        for candidate in {int(item.amf_ue_ngap_id)
                          for item in reader.observations()}:
            if amf_ue_ngap_id is not None and candidate != int(amf_ue_ngap_id):
                continue
            observation = reader.at_or_before(
                instant, lookback_ms=freshness_ms, amf_ue_ngap_id=candidate)
            if observation is not None:
                fresh[candidate] = observation
        if len(fresh) == 1:
            return next(iter(fresh.values()))
        if len(fresh) > 1:
            break
        sleep_ms(500)

    seen = (f"freshness window {int(freshness_ms)} ms; "
            f"{reader.lines_read} stream line(s) drained, "
            f"{len(reader.observations())} UE-level record(s) parsed, "
            f"{reader.rejected_records} record(s) discarded for a stale "
            f"connection epoch or an unknown E2 node")
    if amf_ue_ngap_id is not None:
        observable = ", ".join(str(item) for item in sorted(
            {int(item.amf_ue_ngap_id) for item in reader.observations()})) or "none"
        raise LiveConsoleError(
            f"amfUeNgapId {int(amf_ue_ngap_id)} is not observable on the live "
            f"KPM stream right now, so there is no identity to address and no "
            f"serving cell to move it from. UEs the stream has published at "
            f"all: {observable}. ({seen})")
    if not fresh:
        raise LiveConsoleError(
            "no fresh KPM UE attribution indication: no UE is observable, so "
            "there is no identity to address and no serving cell to move it "
            f"from. ({seen}) A stream with lines drained but no UE-level "
            "record is publishing no format-3 indication; one with records "
            "discarded is publishing another E2 association's.")
    raise LiveConsoleError(
        "more than one UE is observable and this case addresses one: "
        + ", ".join(f"amfUeNgapId {identifier} on cell {observation.serving_nci}"
                    for identifier, observation in sorted(fresh.items()))
        + ". Name the one you mean with --amf-ue-ngap-id, or scope the "
          f"sentence with ueId=. ({seen})")


def live_capable_families() -> Tuple[str, ...]:
    """The families this console may submit **live**, on this binding.

    Sol F-7: generic ``submittable`` is a different set from LIVE-capable.  A
    family reaches this list only when all three hold -- the Gate 4 registry
    records it submittable, its module declares a policy type this deployment
    advertises, and this repository defines a sentence an Operator could have
    typed for it (``FAMILY_UTTERANCES``).  A family with no sentence has no way
    into a Cockpit session at all, so listing it would offer an objective the
    console cannot start.
    """
    capable: List[str] = []
    for family in FAMILY_UTTERANCES:
        if family not in FAMILY_MODULES:
            continue
        try:
            resolve_submittable_family(family)
        except ObjectiveRefused:
            continue
        capable.append(family)
    return tuple(sorted(capable))


#: The measurement a selection-only grammar entry points at.  It exists so
#: :func:`~assurance.advisors.grammar.parse_utterance` can be asked "does this
#: family's keyword appear" without a contract bundle, and it never reaches
#: one: the case's real grammar is built from the family's frozen bundle.
_SELECTION_MEASUREMENT = "measurement/family-selection-only"


def family_for_utterance(utterance: str, *,
                         families: Sequence[str] = ()) -> str:
    """Which live-capable objective family this sentence names.

    The keyword matching is the deterministic grammar's own -- each family is
    offered to :func:`~assurance.advisors.grammar.parse_utterance` as a
    one-entry registry, so the console cannot recognise a sentence the agent
    would not.  What is added here is the arbitration, and it is fail-closed:
    a sentence matching two families is **refused**, not resolved by registry
    order.  It has to be, because the keyword sets overlap by construction --
    ``joint qos steering`` contains ``steer`` -- and letting dict order decide
    would silently draft one objective for a sentence that names another.
    """
    text = (utterance or "").strip()
    if not text:
        raise LiveConsoleError("no intent text was entered")
    candidates = tuple(families) or live_capable_families()
    matched: List[str] = []
    for family in candidates:
        spec = FAMILY_UTTERANCES.get(family)
        if spec is None:
            continue
        registry = {family: IntentGrammarEntry(
            objective_family=family, keywords=tuple(spec["keywords"]),
            measurement_ref=_SELECTION_MEASUREMENT,
            default_operator=ComparisonOperator.EQUAL, default_unit="nci")}
        try:
            parse_utterance(text, objective_registry=registry,
                            source_record="family-selection")
        except IntentParseError:
            continue
        matched.append(family)
    if not matched:
        raise LiveConsoleError(
            f"no live-capable objective recognises {text!r}. This deployment "
            "can start: "
            + "; ".join(
                f"{family} (say {', '.join(FAMILY_UTTERANCES[family]['keywords'])})"
                for family in candidates if family in FAMILY_UTTERANCES))
    if len(matched) > 1:
        raise LiveConsoleError(
            f"{text!r} names more than one objective: " + ", ".join(matched)
            + ". One case runs one objective, and which one it is may not be "
              "decided by the order a registry happens to iterate in; say one.")
    return matched[0]


def operator_utterance(typed: str, generated: str, *,
                       grammar: Mapping[str, Any], case_id: str) -> str:
    """The Operator's own sentence, accepted only when it reads identically.

    The same rule as ``tools/g3ota/run_ota.py``: an Operator may put their own
    words in the evidence, but not say one thing and submit another.  Both
    sentences are read by the *same* deterministic grammar and the typed one is
    accepted only when the whole reading matches -- family, scope, every bound,
    and every part the grammar could not serve.
    """
    source = f"operator-utterance:{case_id}"
    try:
        typed_reading = parse_utterance(
            typed, objective_registry=grammar, source_record=source)
    except IntentParseError as exc:
        raise LiveConsoleError(
            f"the typed sentence is not recognised by the frozen grammar: {exc}"
        ) from None
    generated_reading = parse_utterance(
        generated, objective_registry=grammar, source_record=source)
    if typed_reading != generated_reading:
        raise LiveConsoleError(
            "the typed sentence does not read as the contract this session "
            f"would submit.\n  typed:    {typed!r}\n    -> {typed_reading}\n"
            f"  session:  {generated!r}\n    -> {generated_reading}")
    return typed


#: How many undrained lines one fan-out consumer may hold before the oldest
#: are dropped.  A cap is needed because a sitting runs for as long as an
#: operator sits at it while the KPM stream never stops, and it is safe *here*
#: because every consumer of this tail keeps only the newest record per
#: identity: dropping the oldest can only make a lookup miss, which is
#: ``COUNTER_ABSENT`` -- Gateway ``UNKNOWN`` -- and never a stale value
#: reported as a fresh one.  Overflow is counted, not hidden.
FAN_OUT_QUEUE_LIMIT = 4096


class _FanOutConsumer:
    """One consumer's view of the shared tail.  Callable, and detachable.

    Callable because that is the ``read_new_lines`` shape every reader here
    takes; detachable because a sitting outlives its cases.  A case that has
    reached a terminal state will never poll again, and a queue nobody drains
    is a queue that grows for the rest of the sitting.
    """

    def __init__(self, fan_out: "_FanOutTail", limit: int) -> None:
        self._fan_out = fan_out
        self._limit = int(limit)
        self._queue: List[str] = []
        self._attached = True
        #: Lines dropped because this consumer stopped draining.  Recorded so
        #: an overflowing queue is a fact in the evidence rather than a silent
        #: gap in what a readback saw.
        self.dropped = 0

    @property
    def attached(self) -> bool:
        return self._attached

    @property
    def pending(self) -> int:
        return len(self._queue)

    def offer(self, lines: Sequence[str]) -> None:
        self._queue.extend(lines)
        overflow = len(self._queue) - self._limit
        if overflow > 0:
            del self._queue[:overflow]
            self.dropped += overflow

    def detach(self) -> None:
        """Stop receiving, and release what was queued.  Idempotent."""
        if not self._attached:
            return
        self._attached = False
        self._queue.clear()
        self._fan_out.release(self)

    def __call__(self) -> Sequence[str]:
        if not self._attached:
            # Detached is not "silent": a detached consumer belongs to a case
            # that is over, and answering with lines it never queued would be
            # attributing the stream to a trial that has ended.
            return ()
        self._fan_out.drain()
        taken, self._queue = self._queue, []
        return taken


class _FanOutTail:
    """One drain of the KPM tail, delivered to every attached consumer.

    Two readers of one file tail is a race with no winner: the tail is a byte
    offset, so whichever drains first consumes the lines the other needed and
    the loser sees an empty stream it cannot tell from a silent one.  A sitting
    has at least two consumers -- the UE attribution reader, which lives as long
    as the sitting, and one configuration-counter reader per case that composes
    a supplementary control -- so the tail is drained here once and each
    consumer takes its own copy.

    Consumers are *released*, not accumulated.  A sitting keeps its finished
    cases, because the record of what the sitting did is the point of holding
    them; their stream consumers are a different thing, and keeping those would
    mean every line the gNB publishes for the rest of the session is copied
    into a queue no one will ever read.
    """

    def __init__(self, read_new_lines: Callable[[], Sequence[str]],
                 *, limit: int = FAN_OUT_QUEUE_LIMIT) -> None:
        self._read = read_new_lines
        self._limit = int(limit)
        self._consumers: List[_FanOutConsumer] = []

    @property
    def consumers(self) -> Tuple["_FanOutConsumer", ...]:
        """Every attached consumer, for the boundedness tests."""
        return tuple(self._consumers)

    def consumer(self) -> "_FanOutConsumer":
        """A ``read_new_lines`` of this tail that no other consumer starves."""
        attached = _FanOutConsumer(self, self._limit)
        self._consumers.append(attached)
        return attached

    def release(self, attached: "_FanOutConsumer") -> None:
        self._consumers = [item for item in self._consumers if item is not attached]

    def drain(self) -> None:
        """Read the tail once and hand the same lines to every consumer."""
        lines = list(self._read())
        if not lines:
            return
        for attached in self._consumers:
            attached.offer(lines)


@dataclass(frozen=True)
class CapReadbackAttribution:
    """The identity a configuration readback must match, captured at prepare.

    All four fields, and no fewer.  ``amfUeNgapId`` alone is not an identity a
    configuration can be verified against: the same UE reappears on another
    cell after a handover and on another ``connectionEpoch`` after an E2
    re-association, and an indication from either would otherwise "verify" a
    cap that the scheduler this transaction wrote to is not holding.

    ``serving_nci`` and ``e2_node`` are captured together from one
    :class:`~assurance.live.pin_to_cell_driver.LiveUeObservation`, which the
    topology resolved as a pair.  Matching the node is therefore matching the
    cell: an indication from another node is either another cell or another
    association, and both are refused.
    """

    amf_ue_ngap_id: int
    serving_nci: int
    e2_node: str
    connection_epoch: int

    @property
    def key(self) -> Tuple[str, str, str]:
        """The counter-sample key this attribution admits, and no other."""
        return (str(self.amf_ue_ngap_id), self.e2_node, str(self.connection_epoch))

    def to_record(self) -> Dict[str, Any]:
        return {
            "amfUeNgapId": self.amf_ue_ngap_id, "servingCell": self.serving_nci,
            "e2NodeId": self.e2_node, "connectionEpoch": self.connection_epoch,
        }


def joint_serving_attribution(
    *, reader: Any, ue_id: str, allowed_cells: Sequence[int],
    now: Callable[[], str], freshness_bound_ms: int,
    amf_of: Optional[Callable[[], Optional[int]]] = None,
) -> Callable[[], Optional[CapReadbackAttribution]]:
    """Resolve a UE-scoped control within the admitted joint cell surface.

    The shared attribution reader rejects unknown nodes and changed E2 epochs.
    A handover can change the cell, but never the UE or the admitted epochs.
    Both the policy builder and configuration reader use this resolver; the
    latter still requires a counter from the exact resolved node and epoch.
    Single-cell supplementary compositions keep their fixed attribution.

    ``amf_of`` resolves a role-labelled UE (``ue3``) to the id the network gives
    it now (docs/design/ue-identity-continuity.md); a numeric label is its own id.
    """
    allowed = frozenset(int(cell) for cell in allowed_cells)

    def resolve() -> Optional[CapReadbackAttribution]:
        amf = amf_of() if amf_of is not None else int(ue_id)
        if amf is None:
            return None
        reader.refresh()
        observed = reader.at_or_before(
            now(), lookback_ms=freshness_bound_ms, amf_ue_ngap_id=int(amf))
        if observed is None or observed.serving_nci not in allowed:
            return None
        return CapReadbackAttribution(
            observed.amf_ue_ngap_id, observed.serving_nci,
            observed.e2_node, observed.connection_epoch)

    return resolve


class KpmCapConfigReader:
    """The independent configuration readback, off the live KPM gate JSONL.

    The second of the two observations a corroborated readback needs.  The
    producer's own status is the first; this is a *separate* read of the same
    gNB's ``RAN.UE.DlPrbCap`` or ``RAN.UE.PfWeight`` indication, so a
    producer claiming a value the scheduler does not hold is caught.

    PF weights retain their real value; their live wiring is unproven on the
    radio until lab verification. The cap keeps its integer result.

    The indication has to be **the same UE, on the same serving cell, in the
    same E2 connection epoch** as the attribution captured before the write
    (:class:`CapReadbackAttribution`).  The KPM stream supplies the node and the
    epoch on every record (``assurance/collector/o1col.py``), so a same-UE
    record from another cell or another association is not a weaker
    verification -- it is a verification of something else, and it is refused.
    Samples are therefore indexed by the whole triple rather than by UE id: a
    record from elsewhere cannot even overwrite the one this transaction is
    entitled to read.

    A joint UE-scoped PF axis may supply ``attribution_provider`` to resolve
    that exact attribution again after steering. This provider is restricted
    to the admitted cell surface and E2 epochs, and its resolved attribution
    is recorded with every read. The fixed-cell default does not follow UEs.

    It answers ``None`` -- never a guess -- when the counter is absent for that
    exact identity, when the newest matching indication is older than the
    freshness bound, when the scope names a different controlled UE, or when
    the only records for the UE come from another cell or epoch.  ``None``
    becomes Gateway ``UNKNOWN``, which is the honest outcome and the one the
    Kernel can act on.
    """

    def __init__(self, tail: Any, adapter: Any, *, counter_name: str,
                 now: Callable[[], str], freshness_bound_ms: int,
                 expected: CapReadbackAttribution,
                 controlled_scope_key: str = CONTROLLED_UE_SCOPE_KEY,
                 readback_leaf: str = "maxDlPrbs",
                 attribution_provider: Optional[Callable[[], Optional[CapReadbackAttribution]]] = None) -> None:
        if (counter_name, readback_leaf) not in {
            (SUPPLEMENTARY_ACTIONS[action].readback_counter,
             SUPPLEMENTARY_ACTIONS[action].readback_leaf)
            for action in (CAP_ACTION_ID, PRIORITY_ACTION_ID)
        }:
            raise LiveConsoleError(f"no UE configuration reader for {counter_name}/{readback_leaf}")
        self._readback_leaf = readback_leaf
        self._tail = tail
        self._adapter = adapter
        self._counter_name = counter_name
        #: Where the plan scope carries the controlled UE this reader serves.
        #: A joint case scopes it per UE (``controlledUe@<ue>``).
        self._controlled_scope_key = controlled_scope_key
        self._now = now
        self._freshness_bound_ms = int(freshness_bound_ms)
        self._expected = expected
        self._attribution_provider = attribution_provider
        self._latest: Dict[Tuple[str, str, str], Tuple[str, float]] = {}
        #: Every answer, for the evidence record.  Display and audit only.
        self.reads: List[Dict[str, Any]] = []

    @property
    def expected(self) -> CapReadbackAttribution:
        return self._expected

    @property
    def dropped_lines(self) -> int:
        """Lines the shared tail could not hold for this reader, if any."""
        return int(getattr(self._tail, "dropped", 0))

    def close(self) -> None:
        """Stop consuming the shared stream.  Idempotent; keeps the record."""
        detach = getattr(self._tail, "detach", None)
        if callable(detach):
            detach()

    def _absorb(self) -> None:
        lines = self._tail()
        if not lines:
            return
        for sample in self._adapter.parse_lines(lines).samples:
            if sample.counter_id != self._counter_name:
                continue
            scope = sample.scope_snapshot
            ue = str(scope.get("amf_ue_ngap_id", ""))
            node = str(scope.get("e2_node", ""))
            epoch = str(scope.get("connection_epoch", ""))
            if not ue or not node or not epoch:
                # A record that does not say which UE, which node and which
                # association it describes cannot verify anything.
                continue
            key = (ue, node, epoch)
            previous = self._latest.get(key)
            if previous is None or sample.observed_at >= previous[0]:
                self._latest[key] = (sample.observed_at, float(sample.value.value))

    def _note(self, outcome: str, **detail: Any) -> None:
        self.reads.append({
            "outcome": outcome, "expected": self._expected.to_record(), **detail})

    def read(self, counter_name: str, scope: Mapping[str, Any]
             ) -> Optional[Mapping[str, Any]]:
        if counter_name != self._counter_name:
            return None
        self._absorb()
        # The gateway hands a readback the *plan* scope, which names the
        # objective UE.  A supplementary control acts on a different one and
        # the plan carries it in its own key; reading the objective UE's
        # counter here would report the wrong UE's configuration and call the
        # cap verified on it.
        controlled = scope.get(self._controlled_scope_key)
        ue = str(
            (controlled or {}).get("ueId")
            if isinstance(controlled, Mapping)
            else scope.get("controlledUeId") or "")
        # A role label (``ue3``) names this reader's UE and is resolved to its current id
        # (by the provider when there is one, else the case's identity); only a numeric
        # label is compared.
        role_labelled = bool(ue) and not ue.isdigit()
        if ue != str(self._expected.amf_ue_ngap_id) and not role_labelled:
            self._note("SCOPE_MISMATCH", ueId=ue)
            return None
        if self._attribution_provider is not None:
            expected = self._attribution_provider()
            if expected is None or (not role_labelled and str(expected.amf_ue_ngap_id) != ue):
                self._note("ATTRIBUTION_UNAVAILABLE", ueId=ue)
                return None
            self._expected = expected
        observed = self._latest.get(self._expected.key)
        if observed is None:
            # Saying *why* matters: "the counter is not in the stream" and
            # "the counter is there, for this UE, on another cell or another
            # association" need different actions from an operator and would
            # otherwise look identical.
            elsewhere = sorted(
                (node, epoch) for identifier, node, epoch in self._latest
                if identifier == str(self._expected.amf_ue_ngap_id))
            self._note(
                "ATTRIBUTION_MISMATCH" if elsewhere else "COUNTER_ABSENT",
                ueId=ue,
                observedElsewhere=[{"e2NodeId": node, "connectionEpoch": epoch}
                                   for node, epoch in elsewhere])
            return None
        age_ms = abs(
            (parse_utc(self._now()) - parse_utc(observed[0])).total_seconds() * 1000)
        if age_ms > self._freshness_bound_ms:
            self._note("STALE", ueId=ue, ageMs=int(age_ms))
            return None
        value = observed[1] if self._readback_leaf == "pfWeight" else int(observed[1])
        self._note("OBSERVED", ueId=ue, value=value, observedAt=observed[0])
        return {self._readback_leaf: value}


@dataclass(frozen=True)
class CellReadbackAttribution:
    """The identity a CELL-scoped configuration readback must match.

    Three fields where the UE-scoped one needs four, because there is no UE:
    the cell this transaction wrote to, the E2 node that reported it, and the
    association it was reported on.  ``connection_epoch`` is not optional --
    a gNB restart moves it, and an indication from the previous association
    describes a scheduler this transaction never wrote to.
    """

    serving_nci: int
    e2_node: str
    connection_epoch: int

    @property
    def key(self) -> Tuple[str, str]:
        return (self.e2_node, str(self.connection_epoch))

    def to_record(self) -> Dict[str, Any]:
        return {"servingNci": int(self.serving_nci), "e2NodeId": self.e2_node,
                "connectionEpoch": int(self.connection_epoch)}


class KpmCellConfigReader:
    """Read a cell-scoped configuration counter back from the KPM stream.

    The UE-scoped reader cannot serve these: a node-level indication carries
    ``e2_node`` and ``connection_epoch`` and NO ``amf_ue_ngap_id``, so that
    reader drops every one of them as "a record that does not say which UE".
    Measured on the live wire 2026-09-16: ``RAN.Cell.TxAttenuationDb`` arrives
    with scope ``{slot, e2_node, connection_epoch}`` while ``RAN.UE.DlPrbCap``
    adds ``amf_ue_ngap_id`` and the UE identifiers.

    Answers ``None`` -- never a guess -- when the counter is absent for this
    node and association, when the newest matching indication is older than
    the freshness bound, or when the plan scope names a different cell.
    ``None`` becomes Gateway ``UNKNOWN``, which is what the Kernel can act on.
    """

    def __init__(self, tail: Any, adapter: Any, *, counter_name: str,
                 now: Callable[[], str], freshness_bound_ms: int,
                 expected: CellReadbackAttribution,
                 scope_key: str = "cellId",
                 readback_leaf: str = "txAttenuationDb") -> None:
        if (counter_name, readback_leaf) not in {
            (SUPPLEMENTARY_ACTIONS[action].readback_counter,
             SUPPLEMENTARY_ACTIONS[action].readback_leaf)
            for action in (ATTENUATION_ACTION_ID, MCS_ACTION_ID)
        }:
            raise LiveConsoleError(
                f"no cell configuration reader for {counter_name}/{readback_leaf}")
        self._tail = tail
        self._adapter = adapter
        self._counter_name = counter_name
        self._scope_key = scope_key
        self._readback_leaf = readback_leaf
        self._now = now
        self._freshness_bound_ms = int(freshness_bound_ms)
        self._expected = expected
        self._latest: Dict[Tuple[str, str], Tuple[str, float]] = {}
        #: Every answer, for the evidence record.  Display and audit only.
        self.reads: List[Dict[str, Any]] = []

    @property
    def expected(self) -> CellReadbackAttribution:
        return self._expected

    @property
    def dropped_lines(self) -> int:
        return int(getattr(self._tail, "dropped", 0))

    def close(self) -> None:
        detach = getattr(self._tail, "detach", None)
        if callable(detach):
            detach()

    def _absorb(self) -> None:
        lines = self._tail()
        if not lines:
            return
        for sample in self._adapter.parse_lines(lines).samples:
            if sample.counter_id != self._counter_name:
                continue
            scope = sample.scope_snapshot
            node = str(scope.get("e2_node", ""))
            epoch = str(scope.get("connection_epoch", ""))
            if not node or not epoch:
                # Without the node and the association it cannot verify a cell.
                continue
            key = (node, epoch)
            previous = self._latest.get(key)
            if previous is None or sample.observed_at >= previous[0]:
                self._latest[key] = (sample.observed_at, float(sample.value.value))

    def _note(self, outcome: str, **detail: Any) -> None:
        self.reads.append({
            "outcome": outcome, "expected": self._expected.to_record(), **detail})

    def read(self, counter_name: str, scope: Mapping[str, Any]
             ) -> Optional[Mapping[str, Any]]:
        if counter_name != self._counter_name:
            # 2026-09-18: 이 분기가 **아무것도 남기지 않는 유일한 None** 이었다.
            # 전력 축은 `readbackLog` 가 1657 건 전부 `OBSERVED` 인데 커널은
            # `r1-power@… answered UNKNOWN: the contracted readback did not produce
            # an observation` 을 받았다 -- 두 기록이 모순인 이유가 여기다: 다른
            # 카운터 이름으로 물으면 조용히 None 이 되고 로그에 흔적이 없다.
            self._note("COUNTER_NAME_MISMATCH", requested=str(counter_name))
            return None
        self._absorb()
        requested = scope.get(self._scope_key)
        cell = str((requested or {}).get("cellId")
                   if isinstance(requested, Mapping) else requested or "")
        # 2026-09-18: 여기서 **서로 다른 이름 체계를 비교**하고 있었다.  계획의 scope 는
        # 셀을 DU 지역 이름으로 부르고(관측값 `"NRCellDU-1"`), `serving_nci` 는 NCI 다
        # (`12345678`).  둘은 같아질 수 없어 전력 축 되읽기가 **21 건 전부
        # `SCOPE_MISMATCH`** 였다 -- 축은 조립됐는데 검증이 불가능한 상태였다.
        # ([[a-kpm-record-names-one-cell-in-three-places]] 와 같은 계열이다.)
        #
        # 이 배선에서 셀을 실제로 특정하는 것은 `(e2_node, connection_epoch)` 이고
        # 아래 `self._expected.key` 조회가 이미 그것으로 건진다.  그러므로 계획이
        # **NCI 로 말할 때만** 대조하고, 지역 이름으로 말하면 그 조회에 맡긴다.
        if cell and cell.isdigit() and cell != str(self._expected.serving_nci):
            self._note("SCOPE_MISMATCH", cellId=cell)
            return None
        observed = self._latest.get(self._expected.key)
        if observed is None:
            # "not in the stream" and "there, but on another association" call
            # for different actions and would otherwise look identical.
            elsewhere = sorted(
                epoch for node, epoch in self._latest
                if node == self._expected.e2_node)
            self._note(
                "ATTRIBUTION_MISMATCH" if elsewhere else "COUNTER_ABSENT",
                cellId=cell,
                observedElsewhere=[{"connectionEpoch": epoch} for epoch in elsewhere])
            return None
        age_ms = abs(
            (parse_utc(self._now()) - parse_utc(observed[0])).total_seconds() * 1000)
        if age_ms > self._freshness_bound_ms:
            self._note("STALE", cellId=cell, ageMs=int(age_ms))
            return None
        value = float(observed[1])
        self._note("OBSERVED", cellId=cell, value=value, observedAt=observed[0])
        return {self._readback_leaf: value}


def controlled_scope_builder(
    family: Any, declared: Any, *,
    validity_provider: Callable[[Mapping[str, Any]], Mapping[str, str]],
    controlled_scope_key: str = CONTROLLED_UE_SCOPE_KEY,
    attribution_provider: Optional[Callable[[], Optional[CapReadbackAttribution]]] = None,
    expected: Optional[CapReadbackAttribution] = None,
) -> Callable[[Mapping[str, Any]], Dict[str, Any]]:
    """Build a supplementary policy body against the **controlled** UE scope.

    The plan's scope names the objective UE, because the objective is about
    that UE.  A supplementary cap acts on a different one, and the plan carries
    it in its own key.  This wrapper is the only place the two are swapped, and
    it refuses rather than falling back: a cap built against a scope with no
    controlled UE in it would be a cap on the very UE the target protects.
    """
    build = make_policy_builder(family, validity_provider=validity_provider)

    def build_for_controlled_ue(command: Mapping[str, Any], *,
                                last_revision: Optional[int] = None,
                                last_fencing_token: Optional[int] = None) -> Dict[str, Any]:
        """Forward ``last_revision`` and ``last_fencing_token``, not swallow them.

        ``last_fencing_token`` is the same trap one seam over, and it was found
        by looking for it here the moment the fence fix landed (2026-09-17): the
        adapter inspects this wrapper, so a keyword this wrapper does not name is
        a fix that passes its tests and does nothing on the bed.

        The adapter inspects this wrapper's signature once
        (``_accepts_last_revision``) and, finding no such parameter, skipped the
        durable seed entirely -- every body came from the builder's own
        numbering.  Live on 2026-09-17 that made a takeover PUT carry the
        revision the adopted policy already held, the producer refused it
        ("policy update requires a newer revision and fencingToken"), and the
        journal recorded ``seed=None, adopted=2``: the seed was never asked for.
        The same wrapper in the hermetic fixture had always forwarded it, which
        is why the seed logic passed its tests while doing nothing on the bed.
        """
        scope = command.get("scope")
        controlled = scope.get(controlled_scope_key) if isinstance(scope, Mapping) else None
        if not isinstance(controlled, Mapping) or not controlled:
            raise SupplementaryCapError(
                f"the plan scope carries no {controlled_scope_key!r}; a "
                f"{family.key} policy has no controlled UE to name")
        if attribution_provider is not None:
            current = attribution_provider()
            label = str(controlled.get("ueId"))
            if current is None or (label.isdigit() and str(current.amf_ue_ngap_id) != label):
                raise SupplementaryCapError("no fresh admitted serving-cell attribution for controlled UE")
            # A role-labelled scope names the UE by role; the policy body names the id it holds now.
            controlled = {**dict(controlled), "cellId": str(current.serving_nci),
                          "ueId": str(current.amf_ue_ngap_id)}
        elif not str(controlled.get("ueId")).isdigit():
            # With no resolver the case's own identity is the id; a role never reaches a body.
            if expected is None:
                raise SupplementaryCapError("a role-labelled controlled UE has no identity to name")
            controlled = {**dict(controlled), "ueId": str(expected.amf_ue_ngap_id)}
        # A joint PF axis may control any named UE, including the objective
        # UE. Preserve the legacy supplementary cap non-target restriction.
        # 2026-09-18: **전력 family 만 `gnbId` 를 요구한다**(`families.py:132`
        # `scope_fields=("cellId","gnbId")`; 다른 셀 축 mcs 는 `("cellId",)` 뿐).
        # 그것을 안 채워 에이전트가 감쇠를 고른 시행이 두 번 다
        # `VALIDATE failed: power: scope is missing identity leaf 'gnbId'` 로 거절됐다
        # (판 182915 시행 7·8).  nb_id 는 되읽기 신원의 `e2_node` 에 들어 있다.
        if "gnbId" in family.scope_fields and not controlled.get("gnbId"):
            node = getattr(expected, "e2_node", None) if expected is not None else None
            nb = kpm_node_nb_id(str(node)) if node else None
            if nb is not None:
                controlled = {**dict(controlled), "gnbId": str(nb)}
        objective = {key: scope.get(key) for key in family.scope_fields}
        if ((family.key != "priority" or controlled_scope_key == CONTROLLED_UE_SCOPE_KEY)
                and all(controlled.get(key) == objective.get(key) for key in family.scope_fields)):
            raise SupplementaryCapError(
                "the controlled UE is the objective UE; a supplementary control "
                "must act on a different, heavy, non-target UE")
        # A joint surface scopes the axis per UE (``dlPrbCap@<ue>``); the
        # family builder knows the family's own axis, so the command carries
        # that name here and the scoped name stays the gateway's.
        return build({**dict(command), "scope": dict(controlled),
                      "axis": family.axis,
                      # 2026-09-18: `wire_value` 는 **단일 스칼라 축 전용**이라 감쇠처럼
                      # `is_composite=True` 인 축을 거부한다("no single wire scalar; ask
                      # for policy_values instead").  그 거부 때문에 전력 축은 되읽기는
                      # 되는데(2026-09-18 두 셀 `OBSERVED`) **쓰기가 막혀** 있었다.
                      #
                      # 아래 `make_policy_builder` 가 `_coerce_value` 로 값을 잎에 펼치고,
                      # 그 docstring 이 규약을 말한다 -- "A single-leaf family
                      # (cap/priority/**power**) may carry a bare scalar ... a multi-leaf
                      # family (mcs) carries a mapping of its leaves."  그러므로 잎이
                      # 하나면 스칼라를, 여럿이면 `policy_values` 의 매핑을 넘긴다.
                      "value": (declared.wire_value(command.get("value"))
                                if not getattr(declared, "is_composite", False)
                                else (declared.policy_values(command.get("value"))
                                      if len(getattr(declared, "leaves", ()) or ()) > 1
                                      else declared.policy_values(
                                          command.get("value"))[declared.leaves[0]]))},
                     last_revision=last_revision,
                     last_fencing_token=last_fencing_token)

    return build_for_controlled_ue


@dataclass(frozen=True)
class LiveSupplementary:
    """One composed SUPPLEMENTARY participant of a live session."""

    action_id: str
    adapter_key: str
    policy_type_id: str
    axis: str
    adapter: Any
    policy_port: Any
    counter_reader: KpmCapConfigReader
    binding_journal: Any
    request: Optional[SupplementaryCapRequest]
    #: The UE/cell/epoch a configuration readback must match, captured before
    #: this participant wrote anything.
    expected: CapReadbackAttribution

    @property
    def controlled_ue(self) -> Mapping[str, str]:
        return {"cellId": str(self.expected.serving_nci),
                "ueId": str(self.expected.amf_ue_ngap_id)}

    def close(self) -> None:
        """Release this participant's view of the KPM stream.  Idempotent.

        Called when the case is over.  Everything a reader already decided is
        kept -- :attr:`KpmCapConfigReader.reads` is the evidence and outlives
        the case -- and only the queue that would otherwise grow is let go.
        """
        self.counter_reader.close()

    def describe(self) -> Mapping[str, Any]:
        """A read-only snapshot for the Cockpit's frozen view model."""
        transactions = self.binding_journal.transaction_ids()
        record = (self.binding_journal.binding_for(transactions[-1])
                  if transactions else None)
        last_read = self.counter_reader.reads[-1] if self.counter_reader.reads else {}
        return {
            "bindingState": record.state.value if record is not None else "",
            "policyId": (record.policy_id or "") if record is not None else "",
            "readbackState": str(last_read.get("outcome", "")),
            "rollbackState": record.detail if record is not None else "",
            "expectedAttribution": self.expected.to_record(),
            "detail": record.detail if record is not None else "",
        }


@dataclass(frozen=True)
class LiveCase:
    """One case: one frozen epoch, one UE observation, one submission session.

    A case is the unit that ends.  Its epoch froze one family's candidate
    catalog, its trial may run once, and when it reaches a terminal state it is
    over -- so the *next* intent is a different case with its own epoch and its
    own fresh observation of the UE, never a second trial on this one.
    """

    objective: str
    case_id: str
    runtime: Any
    grammar: Mapping[str, Any]
    utterance: str
    identity: LiveUeObservation
    target_nci: int
    preflight: Mapping[str, Any]
    policy_port: Any
    scope_cleared: Sequence[Mapping[str, Any]]
    evidence_prefix: Path
    session: KernelSubmissionSession
    #: The sentence this case's frozen contract *is* -- what the session would
    #: submit if the Operator typed nothing of their own.  Kept because every
    #: draft, first or re-draft, is checked against it: see
    #: :meth:`LiveConsoleSession.draft`.
    generated: str = ""
    policy_builders: Sequence[Any] = ()
    #: The SUPPLEMENTARY participants this case composed, in apply order.
    #: Per case, like the epoch and the UE observation: each case re-resolves
    #: the controlled UE and gets its own durable binding journal, so a cap
    #: from a previous case can never be reversed against this one's scope.
    supplementary: Tuple[LiveSupplementary, ...] = ()

    @property
    def is_terminal(self) -> bool:
        """True once this case's trial has settled, so a next case may open.

        Read off the session's own view rather than off a private field: the
        view is what the Cockpit paints, so "the operator can see this case is
        over" and "the sitting will open the next one" are the same fact.
        """
        if not self.has_started:
            return False
        settlement = self.session.view().settlement
        return settlement is not None and settlement.is_terminal

    @property
    def has_started(self) -> bool:
        return getattr(self.session, "trial_id", None) is not None

    def release_stream(self) -> None:
        """Let go of this case's view of the KPM stream.  Idempotent.

        A finished case keeps everything that says what it did -- its preflight,
        its readback decisions, its policy bindings.  What it does not keep is a
        queue of indications for a trial that has ended: the sitting runs on
        while the gNB keeps publishing, and a consumer nobody drains would grow
        for the rest of it.
        """
        for participant in self.supplementary:
            participant.close()


class LiveConsoleSession:
    """One Cockpit session, many intents -- the session factory (WP-L item 1).

    The Cockpit drives one object for the whole sitting, and that object is
    this one.  It is not a ``KernelSubmissionSession``: a submission session
    owns one frozen epoch and refuses a second trial (``TRIAL_ALREADY_RUN``)
    and a sentence outside its epoch (``OBJECTIVE_NOT_IN_EPOCH``), which is
    exactly right for a case and exactly wrong for a sitting.  So this holds a
    sequence of :class:`LiveCase` objects and presents the *current* one, and
    :meth:`draft` opens a new case when the last one is over or when the
    sentence names a different objective family.

    What is deliberately *not* reused across cases:

    * the epoch -- each case freezes its own candidate catalog, so neither
      refusal above can fire across cases;
    * the UE observation -- every case re-reads the identity off the live KPM
      stream, because the AMF hands out a new ``amfUeNgapId`` on every
      registration and a second intent addressed from the first one's memory
      would name a UE that may no longer exist;
    * the A1-P scope -- freed again, and archived again, per case.

    One steering actuator per session is still the rule: a case may not start
    while another is non-terminal, and :meth:`draft` refuses rather than
    queueing.
    """

    def __init__(self, *, open_case: Callable[..., LiveCase], mode: str,
                 families: Sequence[str],
                 scope_preconditions: Callable[[LiveCase], Sequence[str]],
                 withdraw: Callable[[LiveCase], Sequence[Mapping[str, Any]]],
                 publish: Optional[Callable[[str, Any], None]] = None) -> None:
        self.mode = mode
        self.families = tuple(families)
        self._open_case = open_case
        # Both touch the deployment, which this class describes but does not
        # reach: reading who holds the A1-P scope and withdrawing them are the
        # composition root's, and are handed in for the same reason the case
        # builder is.
        self._scope_preconditions = scope_preconditions
        self._withdraw = withdraw
        self._publish = publish
        self._cases: List[LiveCase] = []

    # -- what the console and the projection read --------------------------- #

    @property
    def cases(self) -> Tuple[LiveCase, ...]:
        return tuple(self._cases)

    @property
    def current(self) -> Optional[LiveCase]:
        return self._cases[-1] if self._cases else None

    def _require(self) -> KernelSubmissionSession:
        case = self.current
        if case is None:
            raise LiveConsoleError(
                "no case is open in this session; draft an intent first")
        return case.session

    #: Read straight from ``self.__dict__``, never through ``getattr``: this
    #: class defines ``__getattr__``, and touching an instance attribute from
    #: inside it before ``__init__`` has set one is unbounded recursion.
    _OWN = frozenset({"_cases", "_open_case", "_publish",
                      "_scope_preconditions", "_withdraw"})

    def __getattr__(self, name: str) -> Any:
        """Read-through to the current case's submission session.

        Only for names this class does not define itself, and only for reads --
        every control below is written out explicitly, so nothing is delegated
        by accident.  This is what lets ``gui.operator.sources.cockpit`` and
        ``app.py`` project a sitting exactly as they project one case, down to
        ``trial_id``, ``preview``, ``confirmed`` and ``_invalidated``.
        """
        if name.startswith("__") or name in self._OWN:
            raise AttributeError(name)
        cases = self.__dict__.get("_cases")
        if not cases:
            raise AttributeError(name)
        return getattr(cases[-1].session, name)

    @property
    def publisher(self) -> Optional[Callable[[str, Any], None]]:
        return self._publish

    def set_publisher(self, publish: Optional[Callable[[str, Any], None]]
                      ) -> None:
        """Redirect where this sitting emits, now and for every later case."""
        self._publish = publish
        for case in self._cases:
            case.session.set_publisher(publish)

    # -- the operator's four controls --------------------------------------- #

    def release_idle_cases(self) -> int:
        """Release the stream consumers of every case that will not poll again.

        A case will not poll again when the sitting has moved past it -- a
        later case is open -- or when it is the current one and has settled.
        Everything else keeps its stream, including the current case between
        being drafted and being started: that is precisely the case whose
        readbacks are still to come, and detaching it there would make its
        first configuration read see an empty stream.

        A released case keeps everything it decided; only the queue that would
        otherwise grow for the rest of the sitting is let go.  Returns how many
        were released; idempotent per case.
        """
        released = 0
        current = self._cases[-1] if self._cases else None
        for case in self._cases:
            if case is not current or case.is_terminal:
                case.release_stream()
                released += 1
        return released

    def open(self, family: Optional[str], *,
             utterance: Optional[str] = None) -> LiveCase:
        """Open the sitting's first case on a family the caller named.

        Separate from :meth:`draft` because the first case is opened before any
        sentence has been typed: the console needs an epoch, a badge and a
        preflight the moment it is attached, and a deployment that cannot be
        addressed has to be refused before a window opens.  ``family`` may be
        ``None``, which opens the Gate 3 regression case.
        """
        case = self._open_case(family, utterance=utterance)
        if self._publish is not None:
            case.session.set_publisher(self._publish)
        self._cases.append(case)
        # After appending, so "the sitting has moved past it" is true of every
        # earlier case and of none of this one.
        self.release_idle_cases()
        return case

    def draft(self, utterance: str) -> Any:
        """Read one sentence, opening a new case when this one is over.

        The family comes from the sentence, not from the command line: an
        operator who typed a QoS sentence after a steering one gets a QoS case.
        The case is reused only when it is still un-started *and* the sentence
        names the same family -- a re-draft, which is what makes design section
        5's invalidation rule bite on the standing confirmation.
        """
        text = (utterance or "").strip()
        if not text:
            raise LiveConsoleError("no intent text was entered")
        family = family_for_utterance(text, families=self.families)
        case = self.current
        if case is not None and case.has_started and not case.is_terminal:
            raise LiveConsoleError(
                f"case {case.case_id} is still running; one steering actuator "
                "per session means the next intent waits for this one to reach "
                "a terminal state")
        if case is None or case.has_started or case.objective != family:
            case = self._open_case(family, utterance=text)
            if self._publish is not None:
                case.session.set_publisher(self._publish)
            self._cases.append(case)
            # The sitting has moved past every earlier case; their stream
            # consumers go now rather than at the end of the sitting.  The
            # cases themselves are kept: they are the record of what it did.
            self.release_idle_cases()
        else:
            # A re-draft inside the open case.  ``open_case`` checked the
            # sentence that opened this case; nothing checked this one, and
            # that was the hole: the epoch is already frozen, so a sentence
            # naming another cell would be drafted against a candidate it does
            # not describe -- the preview showing constraint 12345678 beside
            # candidate 87654321, with nothing refusing it.  One rule for every
            # draft: the words and the contract read the same or neither is
            # submitted.
            operator_utterance(text, case.generated, grammar=case.grammar,
                               case_id=case.case_id)
        return case.session.draft(text)

    def confirm(self, preview: Any = None, **kwargs: Any) -> Any:
        return self._require().confirm(preview, **kwargs)

    def start(self, instance: Any = None) -> Any:
        """Meet the confirmed preconditions, then run the confirmed contract.

        The withdrawal happens *here* rather than when the case was composed
        because it is the operator's act: it is named in the preview, covered
        by the confirmation's content hash, and performed only after that
        confirmation exists.  If the scope changed since the confirmation was
        taken, the confirmation no longer describes what would happen, so this
        refuses instead of withdrawing something nobody agreed to.
        """
        case = self.current
        if case is None:
            raise LiveConsoleError("nothing has been drafted in this session")
        self._meet_preconditions(case)
        return case.session.start(instance)

    def _meet_preconditions(self, case: LiveCase) -> None:
        confirmed = getattr(case.session, "confirmed", None)
        named = tuple(getattr(getattr(confirmed, "preview", None),
                              "preconditions", ()) or ())
        standing = tuple(self._scope_preconditions(case))
        if standing != named:
            # Re-draft so the operator sees the new precondition rather than
            # only being told the old one is void.
            if case.utterance:
                try:
                    case.session.draft(case.utterance)
                except Exception:                          # pragma: no cover
                    pass
            raise LiveConsoleError(
                "this UE's A1-P scope changed after the contract was "
                f"confirmed: confirmed {list(named) or 'no precondition'}, now "
                f"{list(standing) or 'no precondition'}. The confirmation "
                "covered a different act; review and confirm again.")
        if not named:
            return
        deployment = case.preflight["deployment"]
        withdrawn = self._withdraw(case)
        case.preflight.setdefault("preconditionsMet", [])
        case.preflight["preconditionsMet"].append(
            {"named": list(named), "withdrawn": [dict(entry) for entry in withdrawn],
             "producerDatabase": deployment["producerDatabase"]})

    def abort(self, *args: Any, **kwargs: Any) -> Any:
        return self._require().abort(*args, **kwargs)

    def emergency_stop(self, *args: Any, **kwargs: Any) -> Any:
        return self._require().emergency_stop(*args, **kwargs)

    def request_emergency_stop(self) -> Any:
        case = self.current
        return None if case is None else case.session.request_emergency_stop()

    def view(self) -> Any:
        return self._require().view()


@dataclass(frozen=True)
class LiveSession:
    """A composed live sitting and everything a caller may honestly read.

    ``session`` is what the Cockpit drives -- a :class:`LiveConsoleSession`,
    which is one sitting over a sequence of cases.  Every per-case accessor
    here reads the *current* case, so a caller written for one case (the
    headless runner, the evidence writer) keeps working unchanged and simply
    describes whichever case is open.
    """

    session: LiveConsoleSession
    deployment: LiveDeployment
    injected: Tuple[str, ...] = ()

    @property
    def case(self) -> LiveCase:
        current = self.session.current
        if current is None:
            raise LiveConsoleError("no case is open in this session")
        return current

    @property
    def cases(self) -> Tuple[LiveCase, ...]:
        return self.session.cases

    @property
    def runtime(self) -> Any:
        return self.case.runtime

    @property
    def grammar(self) -> Mapping[str, Any]:
        return self.case.grammar

    @property
    def utterance(self) -> str:
        return self.case.utterance

    @property
    def identity(self) -> LiveUeObservation:
        return self.case.identity

    @property
    def preflight(self) -> Mapping[str, Any]:
        return self.case.preflight

    @property
    def objective(self) -> str:
        return self.case.objective

    @property
    def target_nci(self) -> int:
        return self.case.target_nci

    @property
    def case_id(self) -> str:
        return self.case.case_id

    @property
    def policy_port(self) -> Any:
        return self.case.policy_port

    @property
    def scope_cleared(self) -> Sequence[Mapping[str, Any]]:
        return self.case.scope_cleared

    @property
    def evidence_prefix(self) -> Path:
        return self.case.evidence_prefix

    @property
    def policy_builders(self) -> Sequence[Any]:
        return self.case.policy_builders

    @property
    def supplementary(self) -> Tuple[LiveSupplementary, ...]:
        return self.case.supplementary

    @property
    def mode(self) -> str:
        return self.session.mode

    @property
    def is_live(self) -> bool:
        return self.session.mode == MODE_LIVE

    def policy_ids(self) -> Tuple[str, ...]:
        """The A1-P policies this case's adapter bound, if it bound any."""
        bindings = getattr(self.runtime.adapter, "bindings", None)
        if not callable(bindings):
            return ()
        return tuple(sorted(set(bindings().values())))

    def policy_status(self) -> List[Dict[str, Any]]:
        """The producer's own rows for those policies -- read-only, never a claim."""
        return producer_episode(self.deployment.producer_database,
                                self.policy_ids())

    def transport_calls(self) -> Sequence[Mapping[str, Any]]:
        return tuple(getattr(self.policy_port, "calls", ()))

    def supplementary_policy_ids(self) -> Mapping[str, Tuple[str, ...]]:
        """Adapter key -> the A1 policies that adapter bound in this case."""
        answer: Dict[str, Tuple[str, ...]] = {}
        for participant in self.supplementary:
            bindings = getattr(participant.adapter, "bindings", None)
            answer[participant.adapter_key] = (
                tuple(sorted(set(bindings().values()))) if callable(bindings) else ())
        return answer

    def supplementary_readback_log(self) -> Mapping[str, Sequence[Mapping[str, Any]]]:
        """What the independent configuration counter answered, per adapter."""
        return {participant.adapter_key: tuple(participant.counter_reader.reads)
                for participant in self.supplementary}

    def policy_bodies(self) -> List[Mapping[str, Any]]:
        """Every A1 policy body this case built, as it was sent.

        The bodies are the only place a reader can check that the numbers on
        the wire are the ones the frozen epoch derived, so they belong in the
        evidence rather than only in the producer's copy of them.
        """
        return [body for builder in self.policy_builders
                for body in builder.bodies]


def _plus_ms(stamp: str, milliseconds: int) -> str:
    """A canonical UTC instant *milliseconds* after *stamp*."""
    from datetime import timedelta

    from assurance.core.timebase import format_utc

    return format_utc(parse_utc(stamp) + timedelta(milliseconds=int(milliseconds)))


def resolve_supplementary_actions(
    deployment: LiveDeployment, objective: Optional[str],
    requested: Optional[Sequence[str]], controlled_ue_scope_id: Optional[str],
) -> Tuple[str, ...]:
    """Which SUPPLEMENTARY controls this session may compose, and no others.

    Four gates, all of which must agree:

    * an operator named the controlled non-target UE.  There is no honest
      default: which UE may be capped is a decision about the traffic in the
      cell, and capping whichever UE happened to be second in an inventory is
      not a choice a composition root may make.  This gate is first, so a
      session that names no controlled UE composes exactly the steering-only
      session this root always built;
    * the composition policy admits the action live for this family;
    * the profile's action producer actually serves a policy type for it;
    * the deployment binds it to a fixed adapter key.

    ``None`` derives the intersection.  An explicit request for something the
    deployment does not serve, or for a family that may not carry it, is
    refused by name rather than dropped: a run that silently composed one
    control fewer would be evidence about a different trial.
    """
    producer = deployment.action_producer
    available = tuple(
        entry.action_id for entry in (producer.policy_types.values() if producer else ())
        if entry.action_id in SUPPLEMENTARY_ACTIONS
    )
    admissible = tuple(
        action_id for action_id in available
        if objective is not None and live_cap_admissible(objective)
        and action_id == CAP_ACTION_ID
    )
    if requested is None:
        return admissible if controlled_ue_scope_id else ()
    asked = tuple(dict.fromkeys(str(item) for item in requested))
    if asked and not controlled_ue_scope_id:
        raise LiveConsoleError(
            f"{list(asked)} needs the controlled non-target UE named; pass "
            "--controlled-amf-ue-ngap-id. A supplementary control acts on a UE the "
            "objective does not protect, and which UE that is is an operator "
            "decision.")
    unknown = [item for item in asked if item not in available]
    if unknown:
        raise LiveConsoleError(
            f"this deployment's action producer serves no policy type for "
            f"{unknown}; it advertises " + (", ".join(available) or "nothing"))
    refused = [item for item in asked if item not in admissible]
    if refused:
        raise LiveConsoleError(
            f"{objective} may not carry {refused} live; the composition policy "
            "admits the UE cap only in QoSTarget and UELevelTarget, and only "
            "beside its PRIMARY steering action")
    return asked


#: Axis kinds whose value is a REAL number, so the readback has to spell it the
#: way the frozen candidate does -- "3.0", never "3".
#:
#: This started life as ``declared.value_kind == "number"``, added 2026-09-15
#: when a pfWeight of 4.0 was applied and KPM-verified and the Kernel still
#: answered PARTIAL_APPLY: the producer stores an integral weight as ``4`` and
#: ``str(4)`` is ``"4"``, which does not hash to the planned ``"4.0"``.  The
#: exact-equality test meant the fix never reached the attenuation axis, whose
#: kind is ``"attenuation-db"``, and on 2026-09-18 the identical failure came
#: back on it: the radio really moved to 3.0 dB (three KPM confirmations), the
#: adapter recorded it OBSERVED, and the gateway still said "the readback
#: confirmed none" because its observed config carried ``"3"`` against a
#: planned ``"3.0"`` and matched no prefix at all.  A set, so the next
#: real-valued kind joins it here instead of repeating the bug.
REAL_VALUED_AXIS_KINDS = frozenset({"number", "attenuation-db"})


def _renamed_axis(project: Callable[..., Optional[Mapping[str, Any]]],
                  family_axis: str, surface_axis: str) -> Callable[..., Optional[Mapping[str, Any]]]:
    """A family's scalar readback, keyed by the surface axis a joint case uses.

    The corroborated readback answers on the family's own axis name; a joint
    surface scopes that name per UE, and the gateway merges what each
    participant *names*, so the participant has to name the surface axis.
    """
    if family_axis == surface_axis:
        return project

    def renamed(**kwargs: Any) -> Optional[Mapping[str, Any]]:
        observed = project(**kwargs)
        if observed is None:
            return None
        return {surface_axis if key == family_axis else key: value
                for key, value in dict(observed).items()}

    return renamed


#: A live trial runs well past its nominal hold (every poll also samples the UE
#: flows over SSH), then owes the trailing echo deadline, the cohort read and
#: FINALIZE's reread.  2026-09-15 attempt 30: a 16.5 s hold reached its reread
#: 50 s after COMMIT, 5 s after a one-hold window had let the producer restore
#: and delete the cap -> REREAD UNKNOWN -> PARTIAL_APPLY -> DELETE 404 -> lockdown.
#: Attempt 33 then measured the whole span on a 3x window: write 22:17:07, hold
#: end 22:18:13 (58 s of polls), trailing end 22:18:26, STOP 22:18:34, DELETE
#: answered 22:18:40 -- 93 s, 0.6 s past a 93.5 s window.  8x holds that span
#: with room; the window is only the producer's backstop behind REVERSE.
LIVE_HOLD_ELAPSED_FACTOR = 8
CONCLUDE_RESERVE_MS = 10000


def supplementary_policy_window_ms(*, enforced_timeout_ms: int, r1_deadline_ms: int,
                                   hold_ms: int, freshness_bound_ms: int) -> int:
    """How long a supplementary A1 policy stays valid: the producer's backstop, never the hold's end."""
    return int(enforced_timeout_ms + r1_deadline_ms + LIVE_HOLD_ELAPSED_FACTOR * hold_ms
               + freshness_bound_ms + CONCLUDE_RESERVE_MS)


def _build_supplementary(
    *,
    deployment: LiveDeployment,
    action_id: str,
    identity: LiveUeObservation,
    controlled: LiveUeObservation,
    cap_candidates: Sequence[int],
    objective_floor_kbps: float,
    controlled_reserve_kbps: float,
    calibration_ref: str,
    state_dir: Path,
    clock: Any,
    kpm_tail: Any,
    kpm_adapter: Any,
    freshness_bound_ms: int,
    injected_port: Optional[Any],
    validity: Mapping[str, str],
    axis: Optional[str] = None,
    controlled_scope_key: str = CONTROLLED_UE_SCOPE_KEY,
    validity_provider: Optional[Callable[[Mapping[str, Any]], Mapping[str, str]]] = None,
    state_tag: str = "",
    attribution_provider: Optional[Callable[[], Optional[CapReadbackAttribution]]] = None,
    # The READBACK may follow a UE that re-registers mid-case; the policy BODY may
    # not.  A trial's transaction addresses the id it was composed over -- a rebind
    # between trials is the only way to a new one -- and a body that changed under
    # it would write to a different UE than the one the epoch froze.  Passing one
    # provider to both is what made tests/test_identity_rebind_e2e see the OLD
    # participant build ueId 35 after a re-registration.  Defaults to
    # ``attribution_provider`` so every existing caller keeps its behaviour.
    readback_attribution_provider: Optional[Callable[[], Optional[CapReadbackAttribution]]] = None,
) -> LiveSupplementary:
    """Compose one SUPPLEMENTARY participant over the in-repo action producer.

    ``controlled`` is the controlled non-target UE **as the stream showed it**,
    not as a caller described it.  Its cell and connection epoch are captured
    here, before anything is written, and every later configuration readback
    has to match them: a same-UE indication from another cell or another E2
    association verifies a different thing, and a cap is not verified by it.

    ``axis`` and ``controlled_scope_key`` let a joint case scope the action
    to its UE (``dlPrbCap@<ue>`` carried under ``controlledUe@<ue>``); left
    at their defaults this is the single-cap composition it always was.
    ``validity_provider`` replaces the fixed window when a case runs more than
    one trial: each policy's window is then computed when its body is built.
    ``attribution_provider`` opts a joint UE-scoped PF participant into the
    admitted serving-cell resolver shared by policy construction and readback.
    """
    producer = deployment.action_producer
    assert producer is not None  # resolve_supplementary_actions checked it
    bound = producer.for_action(action_id)
    if bound is None:
        raise LiveConsoleError(
            f"the action producer serves no policy type for {action_id}")
    declared = supplementary_action(action_id)
    if declared.adapter != bound.adapter:
        raise LiveConsoleError(
            f"{action_id} is declared on adapter {declared.adapter!r} but the "
            f"profile binds it to {bound.adapter!r}; one action, one adapter")
    if declared.policy_type_id != bound.policy_type_id:
        raise LiveConsoleError(
            f"{action_id} is declared on policy type {declared.policy_type_id!r} "
            f"but the profile binds it to {bound.policy_type_id!r}")
    family = family_by_policy_type(bound.policy_type_id)

    # A second endpoint means a second transport: the action producer is a
    # different process at a different address, so it gets its own R1 client
    # and its own consumer state rather than sharing the steering one.
    adapter_key = f"{bound.adapter}{state_tag}"
    port = RecordingPolicyPort(
        injected_port if injected_port is not None
        else build_r1_policy_port(
            {**dict(deployment.values), "r1.apiRoot": producer.api_root},
            state_path=state_dir / f"{adapter_key}-state.json"))
    expected = CapReadbackAttribution(
        amf_ue_ngap_id=int(controlled.amf_ue_ngap_id),
        serving_nci=int(controlled.serving_nci),
        e2_node=controlled.e2_node,
        connection_epoch=int(controlled.connection_epoch),
    )
    if declared.scope_kind == "NRCellDU":
        # A node-level indication carries e2_node and connection_epoch and NO
        # amf_ue_ngap_id, so the UE reader drops every one of them.  Measured on
        # the wire 2026-09-16: RAN.Cell.TxAttenuationDb arrives with scope
        # {slot, e2_node, connection_epoch} while RAN.UE.DlPrbCap adds the UE.
        # The cell this acts on is the one the controlled UE is served by -- a
        # cell-scoped control needs no UE identity of its own, which is also why
        # none of the identity faults that cost episodes today can reach it.
        counter_reader = KpmCellConfigReader(
            kpm_tail, kpm_adapter, counter_name=declared.readback_counter,
            now=clock.now, freshness_bound_ms=freshness_bound_ms,
            expected=CellReadbackAttribution(
                serving_nci=int(controlled.serving_nci),
                e2_node=controlled.e2_node,
                connection_epoch=int(controlled.connection_epoch)),
            scope_key=declared.scope_key, readback_leaf=declared.readback_leaf)
    else:
        counter_reader = KpmCapConfigReader(
            kpm_tail, kpm_adapter, counter_name=declared.readback_counter,
            now=clock.now, freshness_bound_ms=freshness_bound_ms, expected=expected,
            controlled_scope_key=controlled_scope_key, readback_leaf=declared.readback_leaf,
            attribution_provider=(readback_attribution_provider
                                  if readback_attribution_provider is not None
                                  else attribution_provider))
    readback = CorroboratedConfigReadback(
        family, status_port=port, kpm_reader=counter_reader,
        monotonic_ms=clock.monotonic_ms, sleep_ms=clock.sleep_ms,
        cadence_ms=deployment.binding.r1.cadence_ms,
        deadline_ms=deployment.binding.r1.deadline_ms,
    )
    if action_id == PRIORITY_ACTION_ID:
        # Reuse the cap's bounded baseline polling before exposing a PF writer.
        # This is configuration presence evidence, still unproven on the radio.
        scope = {controlled_scope_key: {"ueId": str(controlled.amf_ue_ngap_id)}}
        if readback(scope=scope, transaction_id="", policy_id=None) is None:
            outcome = counter_reader.reads[-1]["outcome"]
            counter_reader.close()
            raise LiveConsoleError(
                f"the {axis or declared.axis} axis cannot verify "
                f"{declared.readback_counter}: {outcome}; no fresh same-UE/cell/epoch "
                "configuration counter was published")
    journal = JsonFileR1BindingJournal(state_dir / f"{adapter_key}-bindings.json")
    # Durable beside the bindings: what this adapter issued, so a run's write
    # count is a file rather than a reconstruction.
    operations = JsonlR1OperationJournal(
        state_dir / f"{adapter_key}-operations.jsonl")
    surface_axis = axis or declared.axis
    provider = (validity_provider if validity_provider is not None
                else (lambda command: dict(validity)))
    adapter = build_live_r1_supplementary_adapter(
        adapter_key=adapter_key,
        policy_type_id=bound.policy_type_id,
        near_rt_ric_id=deployment.binding.r1.near_rt_ric_id,
        policy_port=port,
        policy_builder=controlled_scope_builder(
            family, declared, validity_provider=provider,
            controlled_scope_key=controlled_scope_key,
            attribution_provider=attribution_provider, expected=expected),
        readback_port=_renamed_axis(
            scalar_leaf_readback(
                readback, axis=declared.axis, leaf=declared.readback_leaf,
                render=(lambda value: str(float(value)))
                if declared.value_kind in REAL_VALUED_AXIS_KINDS else str),
            declared.axis, surface_axis),
        # The scope the producer owns is the one in the policy body it stores.
        scope_key=lambda body: "/".join(
            f"{name}={body['config'][name]}" for name in family.scope_fields),
        binding_journal=journal,
        operation_journal=operations,
        status_projection=make_status_projection(family),
        clock=clock.now,
        # The producer's own refusals are decisions, not lost messages: a
        # schema answer (400), an unadvertised type (404) or a scope conflict
        # (409) means the producer answered and the request provably never
        # reached the gNB.  ``A1Error`` is the family the in-repo producer
        # raises for exactly those; a transport failure is *not* in this tuple
        # and stays ``UNKNOWN``, because a lost message cannot prove the write
        # did not land.
        # ``R1Refusal`` belongs here for the same reason the rest do: it is
        # raised only for 400/404/409, the statuses that mean the producer
        # answered.  Without it a 409 arrived as a plain transport error, the
        # write was classified UNKNOWN and the failed trial kept the scope
        # RESERVED, which then rejected the next trial on that axis
        # (observed live 2026-09-17, two trials lost in one episode).
        refusal_errors=(A1Error, Campaign5Error, Campaign5BuilderError,
                        SupplementaryCapError, R1Refusal),
    )
    request = None if action_id != CAP_ACTION_ID else SupplementaryCapRequest(
        # The cell is the one the controlled UE was *observed* on, not the one
        # the objective UE happens to be on.  They are usually the same cell
        # and the composition must not depend on it.
        controlled_ue={"cellId": str(controlled.serving_nci),
                       "ueId": str(controlled.amf_ue_ngap_id)},
        candidate_caps=tuple(cap_candidates),
        objective_throughput_floor_kbps=float(objective_floor_kbps),
        controlled_harm_reserve_kbps=float(controlled_reserve_kbps),
        calibration_ref=calibration_ref,
        controlled_ue_scope_id=str(controlled.amf_ue_ngap_id),
    )
    return LiveSupplementary(
        action_id=action_id, adapter_key=adapter_key,
        policy_type_id=bound.policy_type_id, axis=surface_axis,
        adapter=adapter, policy_port=port, counter_reader=counter_reader,
        binding_journal=journal, request=request, expected=expected,
    )


def _resolve_target(topology: Any, identity: LiveUeObservation,
                    target_nci: Optional[int]) -> int:
    """The cell this session may steer to, read off the deployment's own map."""
    topology_cells = sorted(set(topology.nb_id_to_nci.values()))
    if target_nci is not None:
        if int(target_nci) not in topology_cells:
            raise LiveConsoleError(
                f"{int(target_nci)} is not a cell this deployment advertises; "
                "the capability manifest names " + ", ".join(
                    str(cell) for cell in topology_cells))
        return int(target_nci)
    others = [cell for cell in topology_cells if cell != identity.serving_nci]
    if len(others) != 1:
        raise LiveConsoleError(
            "the deployment does not name exactly one cell to steer to; state "
            "it with --target-nci")
    return others[0]


def scope_preconditions(database: Any, *, amf_ue_ngap_id: int,
                        policy_type_id: str) -> Tuple[str, ...]:
    """The named things that must happen to the scope before a case can apply.

    Exactly one kind today: a policy that already reached ``APPLIED_VERIFIED``
    and still occupies this UE's scope.  The producer admits one policy per
    scope and counts an expired one, so the next case is refused HTTP 409 until
    that policy is withdrawn -- and withdrawing it is not a detail, because it
    is the durable record of an effect that really happened.  So it is named,
    in the operator's terms, and confirmed rather than done as a side effect of
    starting.

    A policy of another type is deliberately *not* listed: this run will not
    delete another objective's policy to make room for itself, and
    :func:`~tools.g3ota.composition.clear_ue_scope` refuses it by name.
    """
    named = []
    for entry in scope_occupants(database, amf_ue_ngap_id=amf_ue_ngap_id):
        if entry.get("episodeState") not in TERMINAL_SUCCESS_STATES:
            continue
        if str(entry.get("policyTypeId") or policy_type_id) != str(policy_type_id):
            continue
        named.append(
            f"withdraw policy {entry['policyId']} ({entry['episodeState']}) "
            f"on UE {amf_ue_ngap_id}")
    return tuple(sorted(named))


def build_live_session(
    profile: Any,
    *,
    objective: Optional[str] = None,
    target_nci: Optional[int] = None,
    amf_ue_ngap_id: Optional[int] = None,
    publish: Optional[Callable[[str, Any], None]] = None,
    utterance: Optional[str] = None,
    read_new_lines: Optional[Callable[[], Sequence[str]]] = None,
    policy_port: Optional[Any] = None,
    adapter_override: Optional[Any] = None,
    ports: Optional[Any] = None,
    scope_clearer: Optional[Callable[..., Sequence[Mapping[str, Any]]]] = None,
    stamp: Optional[str] = None,
    supplementary_actions: Optional[Sequence[str]] = None,
    action_policy_ports: Optional[Mapping[str, Any]] = None,
    controlled_amf_ue_ngap_id: Optional[int] = None,
    cap_candidates: Sequence[int] = (12,),
    objective_floor_kbps: float = 500.0,
    controlled_reserve_kbps: float = 1500.0,
    cap_calibration_ref: str = "calibration/live/ue-dl-prb-cap",
    counter_sample_loaders: Optional[Mapping[str, Any]] = None,
    arrival: Optional[Callable[[], str]] = None,
) -> LiveSession:
    """Compose one live Cockpit **sitting** over the official O-RAN control path.

    ``profile`` is the single authority: a path to the live profile JSON, or an
    already-resolved :class:`~tools.liveconsole.profile.LiveDeployment`.

    The sitting opens its first case immediately, so the console has an epoch,
    a badge and a preflight the moment it is attached, and a deployment that
    cannot be addressed is refused before a window opens.  Every later intent
    opens its own case through :class:`LiveConsoleSession`.

    ``objective`` names the first case's family from
    :func:`live_capable_families`; omitted, the sitting opens on the Gate 3
    ``UeCellSteeringPinToCell`` regression case.  After that the family comes
    from whatever the operator types.

    ``counter_sample_loaders`` and ``arrival`` feed the multi-counter families
    off the radio: the QoS bundles read an O1 PM counter whose live loader
    stamps its arrival from this host's wall clock, so both have to be movable
    for a reviewer to reproduce a QoS result without the laboratory.  Both are
    injection seams like the rest -- using either makes the sitting ``MOCK``.

    ``amf_ue_ngap_id`` names the UE.  It may equally be stated by the profile
    or by the ``ueId=`` scope of ``utterance``; when more than one of the three
    speaks they must agree.  When none does, the stream must show exactly one
    fresh UE -- see :func:`observe_selected_ue`.

    There is no ``withdraw_verified`` here any more.  Withdrawing a verified
    policy is now a **named precondition** of the next contract: it is shown in
    the preview, covered by the confirmation's content hash, and performed by
    :meth:`LiveConsoleSession.start` only after that confirmation exists.  The
    ``--withdraw-verified-scope`` flag survives where it is still needed, in
    the headless runner, which has no operator at a screen to click.

    ``controlled_amf_ue_ngap_id`` names the heavy non-target UE a supplementary
    control acts on.  Like the objective UE it may equally be stated by the
    profile (``liveConsole.controlledUe.amfUeNgapId``) or by the sentence's
    ``controlledUeId=`` scope, and when more than one speaks they must agree.
    Naming none composes no supplementary control, which is the steering-only
    case this root always built.

    ``supplementary_actions`` names the SUPPLEMENTARY controls each case
    composes beside its PRIMARY steering action.  ``None`` derives them, and
    four gates must agree: an operator named the controlled non-target UE, the
    composition policy admits the control for the family, the profile's
    ``actionProducer`` serves a policy type for it, and that type is bound to
    one fixed adapter.  A profile that names no second producer -- and a
    sitting that names no controlled UE -- composes exactly the steering-only
    cases this root always built.  An explicit list is checked against the same
    gates and refused by name: a case that silently composed one control fewer
    would be evidence about a different trial.

    Every keyword after ``utterance`` is an injection seam for the hermetic
    tests, and using any of them makes the sitting ``MOCK`` -- see
    :func:`session_mode`.
    """
    deployment = (profile if isinstance(profile, LiveDeployment)
                  else load_live_deployment(profile))
    injected = tuple(
        name for name, value in (
            ("read_new_lines", read_new_lines), ("policy_port", policy_port),
            ("adapter_override", adapter_override), ("ports", ports),
            ("scope_clearer", scope_clearer),
            # A caller-supplied counter sample is not something the radio
            # produced, and a caller-settable arrival clock is not this host's
            # first sight of a PM file.  Either makes the sitting MOCK, for the
            # same reason every other seam does.
            ("counter_sample_loaders", counter_sample_loaders),
            ("arrival", arrival))
        if value is not None)
    mode = session_mode(injected)

    if objective is None and adapter_override is not None:
        # ``build_live_pin_to_cell_runtime`` takes no adapter override, so
        # accepting one here would silently compose the *real* R1 adapter while
        # the caller believed it had a mock.  Refusing is the only honest answer.
        raise LiveConsoleError(
            "the Gate 3 UeCellSteeringPinToCell runtime takes no "
            "adapter_override; name an objective family to drive one over a "
            "mock adapter, or inject policy_port to keep the transport inside "
            "the process")
    if objective is not None and objective not in live_capable_families():
        # Refuse before anything is observed, addressed or submitted.  The
        # registry's blocking reasons are a recorded judgement; a console that
        # could argue past them would make the record decorative.
        try:
            resolve_submittable_family(objective)
        except ObjectiveRefused as exc:
            raise LiveConsoleError(f"{objective}: {exc}") from None
        raise LiveConsoleError(
            f"{objective} is submittable but this repository defines no "
            "operator sentence for it, so there is no way to draft it in a "
            "Cockpit session. Live-capable objectives: "
            + ", ".join(live_capable_families()))

    clock = ports or WallClockPorts
    sitting_stamp = stamp or _stamp()
    binding = deployment.binding
    timing = LiveTiming()
    topology = live_topology(binding, deployment.capability)
    policy_type_id = binding.r1.policy_type_id

    # -- the ports the whole sitting shares -------------------------------- #
    #
    # One tail, one R1 consumer, one discovery.  The tail especially: its
    # byte offset is the sitting's continuous view of the indication stream,
    # so a second case reads the history the first one left rather than
    # priming a new window and losing the instants either side of the switch.
    tail_lines = (read_new_lines if read_new_lines is not None
                  else KpmTail(deployment.kpm_jsonl_path).read_new_lines)
    # The tail is a byte offset, so it is drained once and fanned out.  The
    # attribution reader and the configuration-counter reader are two views of
    # one stream; letting them both call the tail would mean whichever asked
    # first ate the lines the other needed.
    fan_out = _FanOutTail(tail_lines)
    reader = KpmUeAttributionReader(
        read_new_lines=fan_out.consumer(), topology=topology)
    if policy_port is None:
        state_dir = deployment.r1_state_dir / sitting_stamp
        state_dir.mkdir(parents=True, exist_ok=True)
        policy_port = build_r1_policy_port(
            deployment.values, state_path=state_dir / "r1-state.json")
    port = RecordingPolicyPort(policy_port)
    discovery = build_policy_type_discovery(
        port, policy_type_id=policy_type_id,
        capability_manifest=deployment.capability)
    requested_ue = requested_amf_ue_ngap_id(
        explicit=amf_ue_ngap_id,
        profile_value=deployment.requested_amf_ue_ngap_id,
        utterance=utterance)
    controlled_ue = requested_controlled_ue(
        explicit=controlled_amf_ue_ngap_id,
        profile_value=deployment.requested_controlled_amf_ue_ngap_id,
        utterance=utterance, objective_ue=requested_ue)
    deployment.evidence_dir.mkdir(parents=True, exist_ok=True)
    clear = scope_clearer or clear_ue_scope
    # One reading of the KPM stream for the whole sitting, shared with the
    # supplementary participants: the configuration counter and the identity
    # attribution are two views of one tail, and a second reader would consume
    # lines the first one needs.
    kpm_adapter = build_live_collectors(binding)[1]

    def open_case(family: Optional[str], *, utterance: Optional[str] = None
                  ) -> LiveCase:
        """Freeze one case: observe the UE now, free the scope, wire the path."""
        index = len(sitting.cases) if sitting is not None else 0
        case_stamp = (sitting_stamp if index == 0
                      else f"{sitting_stamp}-{index + 1}")
        name = family or GATE3_REGRESSION_CASE

        # Fresh every case, never from the previous one's memory: the AMF hands
        # out a new amfUeNgapId on every registration, so a second intent
        # addressed from the first observation could name a UE that no longer
        # exists.
        identity = observe_selected_ue(
            reader, now=clock.now, sleep_ms=clock.sleep_ms,
            freshness_ms=timing.freshness_bound_ms,
            amf_ue_ngap_id=requested_ue)
        target = _resolve_target(topology, identity, target_nci)
        prefix = deployment.evidence_dir / f"LIVECONSOLE-{name}-{case_stamp}"
        archive_path = Path(f"{prefix}-scope-archive.json")

        def archive(records: Sequence[Mapping[str, Any]]) -> None:
            _write_scope_archive(
                archive_path, records, at=clock.now(),
                amf_ue_ngap_id=identity.amf_ue_ngap_id,
                database=deployment.producer_database)

        preflight: Dict[str, Any] = {
            "at": clock.now(),
            "sessionMode": mode,
            "injectedPorts": list(injected),
            "caseIndex": index + 1,
            "objective": name,
            "deployment": deployment.summary(),
            "requestedUe": requested_ue,
            "observedUe": {
                "amfUeNgapId": identity.amf_ue_ngap_id,
                "guAmI": dict(identity.gu_ami),
                "servingCell": identity.serving_nci,
                "e2Node": identity.e2_node,
                "connectionEpoch": identity.connection_epoch,
                "observedAt": identity.observed_at,
            },
            "targetCell": target,
            "kpmSlots": kpm_slot_occupancy(deployment.kpm_jsonl_path),
            "scopeOccupants": scope_occupants(
                deployment.producer_database,
                amf_ue_ngap_id=identity.amf_ue_ngap_id),
        }
        # Non-terminal leftovers are cleared here, before the epoch is built:
        # they are stuck attempts holding a fence, not decisions.  A *verified*
        # policy is left alone and becomes a named precondition instead.
        cleared = clear(
            binding, state_database=deployment.producer_database,
            amf_ue_ngap_id=identity.amf_ue_ngap_id,
            policy_type_id=policy_type_id, withdraw_verified=False,
            archive=archive)

        case_id = f"case/liveconsole-{family or 'pin-to-cell'}:{case_stamp}"
        builders: List[LivePolicyBuilder] = []
        pin_deployment = LivePinToCellDeployment(
            home_nci=identity.serving_nci, target_nci=int(target),
            ue_scope_id=str(identity.amf_ue_ngap_id), topology=topology,
            r1_deployment=binding.r1.deployment)

        def policy_builder_factory(kernel: Any, contracts: Mapping[str, Any],
                                   *, objective_kind: Optional[str] = None) -> Any:
            extra = ({} if objective_kind is None
                     else {"objective_kind": objective_kind})
            builder = LivePolicyBuilder(
                kernel=kernel, contracts=contracts, deployment=pin_deployment,
                identity=identity, case_id=case_id,
                policy_type_discovery=discovery,
                capability_manifest=deployment.capability, **extra)
            builders.append(builder)
            return builder

        # -- the SUPPLEMENTARY participants, per case ---------------------- #
        #
        # Composed here rather than once per sitting because everything they
        # bind to is per case: the controlled UE is re-checked against *this*
        # case's fresh observation, and each case gets its own durable binding
        # journal so a cap from a previous case can never be reversed against
        # this one's scope.
        composed_supplementary: List[LiveSupplementary] = []
        chosen = resolve_supplementary_actions(
            deployment, family, supplementary_actions,
            None if controlled_ue is None else str(controlled_ue))
        if chosen:
            producer = deployment.action_producer
            assert producer is not None  # resolve_supplementary_actions checked
            action_state_dir = (
                producer.state_dir or (deployment.r1_state_dir / "action"))
            action_state_dir = action_state_dir / case_stamp
            action_state_dir.mkdir(parents=True, exist_ok=True)
            # A supplementary policy lives no longer than the trial that
            # authorised it, and every number in its window is the
            # deployment's own.  The window opens now -- at composition --
            # but the policy is only *written* after admission, PREPARE,
            # READY and the primary's COMMIT (a handover), and the action
            # producer then has its control deadline to apply it; the hold
            # and the freshness bound come after that.  A window of hold +
            # freshness alone (7 s on this deployment) closed before the
            # PUT arrived and the live worker refused it as expired
            # (observed over the air 2026-09-06).  The producer's own expiry
            # restore at ``notAfter`` stays as the backstop behind the
            # gateway's REVERSE, never as the hold's end.
            validity = {
                "notBefore": clock.now(),
                "notAfter": _plus_ms(
                    clock.now(),
                    supplementary_policy_window_ms(
                        enforced_timeout_ms=timing.enforced_timeout_ms,
                        r1_deadline_ms=int(deployment.binding.r1.deadline_ms),
                        hold_ms=timing.hold_ms,
                        freshness_bound_ms=timing.freshness_bound_ms)),
            }
            controlled_scope_id = str(controlled_ue)
            if controlled_scope_id == str(identity.amf_ue_ngap_id):
                raise LiveConsoleError(
                    "the controlled UE is the objective UE; a supplementary "
                    "control must act on a different, heavy, non-target UE")
            # The controlled UE is observed in its own right, now, before any
            # participant exists: its cell and connection epoch are what every
            # later configuration readback must match, and a UE the stream
            # cannot attribute has no configuration to verify and no cap to
            # carry.  Refusing here costs a case; guessing costs a verified
            # cap on a UE that had moved.
            controlled = observe_selected_ue(
                reader, now=clock.now, sleep_ms=clock.sleep_ms,
                freshness_ms=timing.freshness_bound_ms,
                amf_ue_ngap_id=int(controlled_scope_id))
            for action_id in chosen:
                composed_supplementary.append(_build_supplementary(
                    deployment=deployment, action_id=action_id, identity=identity,
                    controlled=controlled,
                    cap_candidates=cap_candidates,
                    objective_floor_kbps=objective_floor_kbps,
                    controlled_reserve_kbps=controlled_reserve_kbps,
                    calibration_ref=cap_calibration_ref,
                    state_dir=action_state_dir, clock=clock,
                    kpm_tail=fan_out.consumer(), kpm_adapter=kpm_adapter,
                    freshness_bound_ms=timing.freshness_bound_ms,
                    injected_port=(action_policy_ports or {}).get(action_id),
                    validity=validity,
                ))

        # The multi-counter families (QoSTarget, QoSandTSP) read an O1 PM
        # counter whose live loader stamps its arrival from this host's wall
        # clock.  Both are injectable so the whole family can be driven on an
        # injected timebase, which is what lets a reviewer reproduce a QoS
        # result without the laboratory.
        counter_kwargs: Dict[str, Any] = {}
        if counter_sample_loaders is not None:
            counter_kwargs["counter_sample_loaders"] = dict(counter_sample_loaders)
        if arrival is not None:
            counter_kwargs["arrival"] = arrival

        runtime_kwargs: Dict[str, Any] = {}
        if adapter_override is not None:
            runtime_kwargs = {"adapter_name": "mock",
                              "adapter_override": adapter_override}
        if composed_supplementary:
            runtime_kwargs["supplementary_adapters"] = {
                item.adapter_key: item.adapter for item in composed_supplementary}
            runtime_kwargs["axis_adapters"] = {
                item.axis: item.adapter_key for item in composed_supplementary}
            runtime_kwargs["supplementary_axes"] = supplementary_axis_declarations(
                tuple(item.action_id for item in composed_supplementary))
            cap_participant = next(
                item for item in composed_supplementary
                if item.action_id == CAP_ACTION_ID)
            runtime_kwargs["bundle_transform"] = (
                lambda bundle: with_supplementary_cap(
                    bundle, cap_participant.request))

        if family is None:
            runtime: Any = build_live_pin_to_cell_runtime(
                deployment=pin_deployment, timing=timing, binding=binding,
                policy_port=port, policy_builder_factory=policy_builder_factory,
                reader=reader, identity=identity, now=clock.now,
                monotonic_ms=clock.monotonic_ms, sleep_ms=clock.sleep_ms,
                case_id=case_id)
            grammar: Mapping[str, Any] = PIN_TO_CELL_GRAMMAR
            generated = runtime.utterance()
            settle_ms = timing.cadence_ms
        else:
            runtime = build_live_objective_runtime(
                family_module=FAMILY_MODULES[family](),
                scope=live_scope(amf_ue_ngap_id=identity.amf_ue_ngap_id,
                                 home_nci=identity.serving_nci,
                                 target_nci=int(target)),
                binding=binding, policy_port=port,
                policy_builder_factory=lambda kernel, bundle: policy_builder_factory(
                    kernel, bundle_contracts(bundle),
                    objective_kind=A1P_OBJECTIVE_KIND[family]),
                reader=reader, identity=identity, now=clock.now,
                monotonic_ms=clock.monotonic_ms, sleep_ms=clock.sleep_ms,
                case_id=case_id, **counter_kwargs, **runtime_kwargs)
            # What the bundle actually expresses, which is not always what was
            # asked for.  A case whose observed baseline is not the one the
            # bundle names is refused here -- before the equipment is addressed
            # -- rather than discovered as a REJECTED_CONFIG_MISMATCH after a
            # permit has already been issued.
            direction = bundle_direction(runtime.bundle)
            if direction.baseline != int(identity.serving_nci):
                raise LiveConsoleError(
                    f"{family} as frozen moves {direction.baseline} -> "
                    f"{direction.target}, but the UE is observed on "
                    f"{identity.serving_nci}. Submitting would name one cell in "
                    "the sentence and another in the contract.")
            if direction.target != int(target):
                raise LiveConsoleError(
                    f"{family} as frozen moves to {direction.target}, not to "
                    f"the requested {int(target)}")
            grammar = family_grammar(family, runtime.bundle)
            generated = family_utterance(
                family, direction.target, str(identity.amf_ue_ngap_id))
            settle_ms = runtime.geometry.cadence_ms

        # A sentence the session would submit says everything the session
        # would do.  Composing a cap and generating a steering-only sentence
        # would make ``operator_utterance``'s equality guard refuse the very
        # sentence that asked for the cap -- and, worse, would let a typed
        # sentence with no cap clause pass while a cap was composed anyway.
        generated = supplementary_clause(generated, composed_supplementary)
        text = (operator_utterance(utterance, generated, grammar=grammar,
                                   case_id=case_id)
                if utterance else generated)
        preflight["caseId"] = case_id
        preflight["scopeCleared"] = [dict(entry) for entry in cleared]
        preflight["scopeArchive"] = (str(archive_path)
                                     if archive_path.is_file() else None)
        preflight["policyTypeIds"] = list(discovery.get("policyTypeIds") or ())
        preflight["supplementaryActions"] = [
            {"actionId": item.action_id, "adapter": item.adapter_key,
             "policyTypeId": item.policy_type_id, "axis": item.axis,
             "controlledUeId": item.request.controlled_ue_scope_id,
             "controlledAttribution": item.expected.to_record(),
             "candidateCaps": list(item.request.candidate_caps)}
            for item in composed_supplementary
        ]
        preflight["utterance"] = text
        cap_view = next(
            (item for item in composed_supplementary
             if item.action_id == CAP_ACTION_ID), None)
        session = KernelSubmissionSession(
            path=runtime.path, cell_id=runtime.cell_id,
            objective_registry=grammar, mode=mode, publish=publish,
            settle_ms=settle_ms,
            preconditions=lambda: scope_preconditions(
                deployment.producer_database,
                amf_ue_ngap_id=identity.amf_ue_ngap_id,
                policy_type_id=policy_type_id),
            supplementary=(None if cap_view is None else {
                "actionId": cap_view.action_id,
                "adapter": cap_view.adapter_key,
                "axis": cap_view.axis,
                "policyTypeId": cap_view.policy_type_id,
                "controlledUeId": cap_view.request.controlled_ue_scope_id,
                "maxDlPrbs": int(cap_view.request.candidate_caps[0]),
                "observe": cap_view.describe,
            }))
        return LiveCase(
            objective=name, case_id=case_id, runtime=runtime, grammar=grammar,
            utterance=text, generated=generated, identity=identity, target_nci=int(target),
            preflight=preflight, policy_port=port,
            scope_cleared=tuple(cleared), evidence_prefix=prefix,
            session=session, policy_builders=tuple(builders),
            supplementary=tuple(composed_supplementary))

    def case_preconditions(case: LiveCase) -> Sequence[str]:
        return scope_preconditions(
            deployment.producer_database,
            amf_ue_ngap_id=case.identity.amf_ue_ngap_id,
            policy_type_id=policy_type_id)

    def withdraw(case: LiveCase) -> Sequence[Mapping[str, Any]]:
        """Archive, then withdraw, the verified policy the operator confirmed."""
        archive_path = Path(f"{case.evidence_prefix}-precondition-archive.json")
        return clear(
            binding, state_database=deployment.producer_database,
            amf_ue_ngap_id=case.identity.amf_ue_ngap_id,
            policy_type_id=policy_type_id, withdraw_verified=True,
            archive=lambda records: _write_scope_archive(
                archive_path, records, at=clock.now(),
                amf_ue_ngap_id=case.identity.amf_ue_ngap_id,
                database=deployment.producer_database))

    sitting: Optional[LiveConsoleSession] = None
    sitting = LiveConsoleSession(
        open_case=open_case, mode=mode, families=live_capable_families(),
        scope_preconditions=case_preconditions, withdraw=withdraw,
        publish=publish)
    sitting.open(objective, utterance=utterance)
    return LiveSession(session=sitting, deployment=deployment, injected=injected)


def _write_scope_archive(path: Path, records: Sequence[Mapping[str, Any]], *,
                         at: str, amf_ue_ngap_id: int, database: Any) -> None:
    """Every occupant of this UE's scope, verbatim, before anything is withdrawn."""
    path.write_text(
        json.dumps({
            "schemaVersion": "liveconsole-scope-archive/1.0.0",
            "at": at,
            "amfUeNgapId": amf_ue_ngap_id,
            "producerDatabase": str(database),
            "note": (
                "The A1-P producer's own rows for every policy occupying this "
                "UE's scope, complete and verbatim, written before any of them "
                "was withdrawn. A withdrawal that freed the scope moved this "
                "record; it did not destroy it."),
            "records": [dict(record) for record in records],
        }, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")


def attach_live_session(console: Any, profile: Any, **kwargs: Any) -> LiveSession:
    """Build a live session and attach it to an ``OperatorConsole``.

    The one call ``main.py --live`` makes after building the console, so
    ``build_console`` (and its reachability test) still yields a Disconnected
    console with no session.

    ``select_mode("LIVE")`` is called here rather than left to the operator so
    the header badge and ``session_mode_evidence`` agree with what was actually
    attached from the command line.  It is not a way to claim Live: the console
    refuses the selection unless the attached session itself declares ``LIVE``
    (``gui/operator/app.py::select_mode``), which a ``MOCK`` session never does.
    """
    composed = build_live_session(profile, **kwargs)
    console.attach_kernel_session(composed.session)
    if composed.session.mode == MODE_LIVE:
        console.select_mode(MODE_LIVE)
    return composed


def _supplementary_record(live: LiveSession) -> List[Dict[str, Any]]:
    """Every SUPPLEMENTARY participant of this case, as the file records it.

    One entry per adapter, carrying what only that adapter knows: the A1
    policies it bound, the identity its configuration readback was entitled to
    match, every answer that readback gave, and the durable binding journal's
    last word -- ``RESTORED`` and the detail that says the baseline was read
    back, rather than the DELETE response that merely began it.
    """
    policy_ids = live.supplementary_policy_ids()
    readbacks = live.supplementary_readback_log()
    records: List[Dict[str, Any]] = []
    for participant in live.supplementary:
        described = dict(participant.describe())
        records.append({
            "actionId": participant.action_id,
            "adapterKey": participant.adapter_key,
            "policyTypeId": participant.policy_type_id,
            "axis": participant.axis,
            "controlledUe": dict(participant.request.controlled_ue),
            "candidateCaps": [int(value)
                              for value in participant.request.candidate_caps],
            # Two different facts, kept apart.  ``liveBindings`` is what the
            # adapter still holds when the run ends -- empty after a verified
            # restore, because the binding was released, and that *is* the
            # record of a completed unwind.  ``bindingPolicyId`` is the durable
            # journal's, which outlives the release and is the id an operator
            # chases in the producer.
            "liveBindings": list(policy_ids.get(participant.adapter_key, ())),
            "expectedAttribution": participant.expected.to_record(),
            "bindingState": described.get("bindingState", ""),
            "bindingPolicyId": described.get("policyId", ""),
            "readbackState": described.get("readbackState", ""),
            "rollbackDetail": described.get("detail", ""),
            "readbackLog": [dict(entry)
                            for entry in readbacks.get(participant.adapter_key, ())],
        })
    return records


def write_run_evidence(live: LiveSession, view: Any) -> Dict[str, str]:
    """Write this run's evidence beside its scope archive, and say where.

    A live episode cannot be re-run for free, so the one report an operator
    gets has to hold the preflight, the contract that was frozen, the Kernel's
    own event stream and the producer's rows for whatever policy was created.
    Nothing here decides anything: every field is copied from the Kernel, the
    adapter or the producer.

    That includes the **supplementary** half.  A composed cap is verified in
    memory -- the binding journal knows the policy it created and whether the
    baseline came back, and the configuration reader knows every answer it gave
    and the UE/cell/epoch it was entitled to read -- and until ``1.1.0`` none of
    it reached the file.  A run document that recorded only the primary
    adapter's policy could not answer "was the cap applied, to which UE, and
    was it restored", which is the question the acceptance matrix's R2 rows
    exist to ask.  Each participant is written out under its own adapter key:
    two adapters wrote, so two records are kept, and neither is merged into the
    other.
    """
    directory = live.deployment.evidence_dir
    directory.mkdir(parents=True, exist_ok=True)
    run_path = Path(f"{live.evidence_prefix}-run.json")
    events_path = Path(f"{live.evidence_prefix}-events.jsonl")
    settlement = view.settlement
    document = {
        # 1.1.0 adds the supplementary half.  Bumped rather than extended in
        # place because a reader that finds no ``supplementary`` key in a
        # 1.0.0 document is reading a run that had none *recorded*, which is
        # not the same fact as a run that had none.  The six committed 1.0.0
        # files stay readable and stay 1.0.0.
        "schemaVersion": "liveconsole-run/1.1.0",
        "sessionMode": live.mode,
        "objective": live.objective,
        "caseId": live.case_id,
        "utterance": live.utterance,
        "preflight": dict(live.preflight),
        "contract": {
            "epochHash": live.runtime.path.epoch_hash(),
            "candidateId": live.runtime.candidate_id(),
            "terminalStateHash": live.runtime.path.terminal_state_hash(),
        },
        "axes": {
            "executionValidity": view.axes.execution_validity,
            "measurementSufficiency": view.axes.measurement_sufficiency,
            "predicateVerdicts": dict(view.axes.predicate_verdicts),
            "trialOutcome": view.axes.trial_outcome,
            "holdComplete": view.axes.hold_complete,
        },
        "settlement": None if settlement is None else {
            "trialState": settlement.trial_state,
            "outcome": settlement.outcome,
            "stopReason": settlement.stop_reason,
            "evidenceStatus": settlement.evidence_status,
            "caseTermination": settlement.case_termination,
            "harmCharges": list(settlement.harm_charges),
            "gatewayOperations": [list(item)
                                  for item in settlement.gateway_operations],
        },
        "refusal": view.refusal,
        "refusalDetail": view.refusal_detail,
        "policyIds": list(live.policy_ids()),
        "policyBodies": live.policy_bodies(),
        "policyStatus": live.policy_status(),
        "transportCalls": [dict(call) for call in live.transport_calls()],
        "supplementary": _supplementary_record(live),
        "afterEvidence": {
            "kpmSlots": kpm_slot_occupancy(live.deployment.kpm_jsonl_path),
            "readerRejectedRecords": live.runtime.reader.rejected_records,
            # 사유 없이 총계만 남기면 조치를 못 고른다 (agent.py 의 같은 자리와 맞춘다).
            "readerRejectedByReason": dict(
                getattr(live.runtime.reader, "rejected_by_reason", {}) or {}),
        },
    }
    run_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    events_path.write_text(
        "".join(
            json.dumps(envelope.to_canonical_dict(), sort_keys=True,
                       separators=(",", ":")) + "\n"
            for envelope in live.runtime.event_store.iterate()),
        encoding="utf-8")
    return {"run": str(run_path), "events": str(events_path),
            "scopeArchive": live.preflight.get("scopeArchive")}
