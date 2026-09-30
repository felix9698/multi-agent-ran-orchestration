"""The SC-084 O1 readiness lifecycle: ``§10.2``'s fixed startup order, measured.

The withdrawn 1.0.1 treated the three Upper O1 initial states as an optional,
default-OFF module.  That reading is retracted.
``scenario-catalog.1.0.1.json#/scenarios/83/materialization/initialState`` lists
ten mandatory initial states; three of them are Upper readiness obligations --
``INSTALL_O1_PROFILE``, ``INSTALL_ACTIVE_O1_SUBSCRIPTION`` and
``SET_PERF_METRIC_JOB_UNLOCKED`` -- and a fourth,
``CONFIGURE_LIVE_O1_OK_RECORD_FOR_EACH_POLICY_CELL``, stays Lower-Provider owned
but must be *accepted* before the Upper may claim readiness.
``scenario-runner-contract.1.0.1.json#/initialStates`` binds each one to an
adapter action and a postcondition, and those postcondition strings are READ
from the frozen bytes here -- never restated.

``02-rapp-xapp-backend-mandatory-contract.1.0.1.md`` §10.2 fixes the order::

    PRECHECK -> TRUST_READY -> SUBSCRIBED -> JOB_ACTIVE -> ASSURANCE_READY

This module is the ledger that makes that order a measurement:

* a state cannot be confirmed before its predecessors are confirmed;
* a state is confirmed only when the *evidence* it is handed satisfies the
  frozen postcondition -- an assertion without evidence is refused;
* until ``PRECHECK..JOB_ACTIVE`` are all confirmed the measured SC-084 body may
  not start, and every entry point that would start it asks here first;
* until ``ASSURANCE_READY`` is confirmed too, no capture may be dispositioned
  ``COMPLETED``.

Nothing here issues a NETCONF RPC or names a lifecycle ordinal.  The readiness
RPCs belong to the frozen ``o1-netconf-yang-profile`` lifecycle list and stay
there, which is why the measured body's NETCONF RPC delta remains zero.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

RUNNER_CONTRACT_NAME = "scenario-runner-contract.1.0.1.json"

#: The three initial states the Upper O1 Consumer must establish itself.
UPPER_INITIAL_STATES = (
    "INSTALL_O1_PROFILE",
    "INSTALL_ACTIVE_O1_SUBSCRIPTION",
    "SET_PERF_METRIC_JOB_UNLOCKED",
)

#: Frozen ownership: the live PM source is the Provider's and the Upper must
#: never synthesise it.  It is still a readiness obligation of the *run*: without
#: the Provider's acceptance evidence the Upper cannot reach ASSURANCE_READY.
PROVIDER_INITIAL_STATE = "CONFIGURE_LIVE_O1_OK_RECORD_FOR_EACH_POLICY_CELL"

REQUIRED_INITIAL_STATES = UPPER_INITIAL_STATES + (PROVIDER_INITIAL_STATE,)

#: The adapter actions the frozen runner contract binds to the three Upper rows.
REQUIRED_ADAPTER_ACTIONS = (
    "LOAD_O1_PROFILE",
    "INSTALL_O1_SUBSCRIPTION",
    "SET_PERF_METRIC_JOB",
)


class ReadinessRefused(RuntimeError):
    """A readiness postcondition is unmet, or the fixed order was broken.

    Carries a machine-readable ``reason_code`` so a refusal is attributable
    rather than merely non-zero.
    """

    exit_code = 78

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__("%s: %s" % (reason_code, message))
        self.reason_code = str(reason_code)


@dataclass(frozen=True)
class ReadinessStateSpec:
    """One §10.2 state, and which frozen initial states it discharges."""

    state: str
    initial_states: tuple[str, ...]
    owner: str
    meaning: str
    #: Evidence keys that must be present and truthy before the state confirms.
    required_evidence: tuple[str, ...] = ()
    #: ``(key, value)`` pairs the evidence must equal exactly.
    required_values: tuple[tuple[str, Any], ...] = ()


#: The fixed order.  ``§10.2`` items 1..5, verbatim in sequence.
READINESS_SEQUENCE: tuple[ReadinessStateSpec, ...] = (
    ReadinessStateSpec(
        state="PRECHECK",
        initial_states=("INSTALL_O1_PROFILE",),
        owner="UPPER_HARNESS",
        meaning="the exact file-reporting and NETCONF/YANG profiles are active "
                "and every lifecycle requestFixture resolves to the registered "
                "immutable XML bytes",
        required_evidence=("profilesActive", "paFileProfileActive",
                           "lifecycleFixturesResolved", "yangClosureDigest"),
        required_values=(("profilesActive", True),
                         ("paFileProfileActive", True)),
    ),
    ReadinessStateSpec(
        state="TRUST_READY",
        initial_states=(),
        owner="UPPER_HARNESS",
        meaning="notification receiver bound, NETCONF host key pinned and "
                "public-key authenticated, required capabilities advertised and "
                "the schema mount verified before any mutation",
        required_evidence=("hostKeyVerified", "clientAuthMethod",
                           "notificationReceiverBound",
                           "requiredCapabilitiesSatisfied", "schemaMountVerified"),
        required_values=(("hostKeyVerified", True),
                         ("clientAuthMethod", "PUBLIC_KEY"),
                         ("notificationReceiverBound", True),
                         ("requiredCapabilitiesSatisfied", True),
                         ("schemaMountVerified", True)),
    ),
    ReadinessStateSpec(
        state="SUBSCRIBED",
        initial_states=("INSTALL_ACTIVE_O1_SUBSCRIPTION",),
        owner="UPPER_HARNESS",
        meaning="the FileDataReporting subscription returned 201 and its "
                "identifier is durably persisted",
        required_evidence=("subscriptionId", "durable", "createdStatus"),
        required_values=(("durable", True), ("createdStatus", 201)),
    ),
    ReadinessStateSpec(
        state="JOB_ACTIVE",
        initial_states=("SET_PERF_METRIC_JOB_UNLOCKED",),
        owner="UPPER_HARNESS",
        meaning="running datastore locked, PerfMetricJob created LOCKED and "
                "read back LOCKED, the subscription confirmed durable BEFORE "
                "the unlock, then UNLOCKED plus eventual ENABLED read back and "
                "the datastore unlocked",
        required_evidence=("administrativeState", "operationalState",
                           "datastoreLockedBeforeCreate",
                           "lockedReadbackConfirmed",
                           "durableSubscriptionConfirmedBeforeUnlock",
                           "datastoreUnlocked"),
        required_values=(("administrativeState", "UNLOCKED"),
                         ("operationalState", "ENABLED"),
                         ("datastoreLockedBeforeCreate", True),
                         ("lockedReadbackConfirmed", True),
                         ("durableSubscriptionConfirmedBeforeUnlock", True),
                         ("datastoreUnlocked", True)),
    ),
    ReadinessStateSpec(
        state="ASSURANCE_READY",
        initial_states=(PROVIDER_INITIAL_STATE,),
        owner="LOWER_LIVE_O1_PROVIDER",
        meaning="the Provider acceptance contract records the live PM initial "
                "state as established, and the first notification -> SFTP "
                "retrieval -> verification -> normalization cycle succeeded",
        required_evidence=("providerAcceptedInitialState", "notificationsAccepted",
                           "filesRetrieved", "commitEligibleRecords"),
        required_values=(("providerAcceptedInitialState", True),),
    ),
)

READINESS_ORDER: tuple[str, ...] = tuple(
    spec.state for spec in READINESS_SEQUENCE)

#: The states that must all be confirmed before the measured SC-084 body starts.
#: ``ASSURANCE_READY`` is deliberately NOT one of them: §10.2 item 5 defines it
#: as the state the first successful notification/retrieval/normalization cycle
#: *produces*, so requiring it up front would be circular.  It gates the
#: capture's ``COMPLETED`` disposition instead.
MEASURED_BODY_PRECONDITIONS: tuple[str, ...] = tuple(
    state for state in READINESS_ORDER if state != "ASSURANCE_READY")


class FrozenReadinessPostconditions:
    """Reader over ``scenario-runner-contract.1.0.1.json#/initialStates``.

    Every postcondition string and adapter action this release compares against
    is READ from the frozen bytes it is handed.  A bundle that does not carry
    the runner contract cannot establish readiness, and that is a refusal.
    """

    def __init__(self, profile_bytes: Mapping[str, bytes]) -> None:
        raw = {str(name).replace("\\", "/"): bytes(value)
               for name, value in dict(profile_bytes or {}).items()}
        document: dict[str, Any] | None = None
        for name in sorted(raw):
            if name.endswith(RUNNER_CONTRACT_NAME):
                try:
                    document = json.loads(raw[name].decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ReadinessRefused(
                        "READINESS_CONTRACT_UNREADABLE",
                        "frozen %s is not readable JSON" % RUNNER_CONTRACT_NAME
                    ) from exc
                break
        if document is None:
            raise ReadinessRefused(
                "READINESS_CONTRACT_ABSENT",
                "the frozen %s is not among the bundle bytes handed to the O1 "
                "consumer; the readiness postconditions cannot be derived"
                % RUNNER_CONTRACT_NAME)
        states = document.get("initialStates")
        if not isinstance(states, Mapping):
            raise ReadinessRefused(
                "READINESS_CONTRACT_MALFORMED",
                "the frozen runner contract declares no /initialStates object")
        missing = [name for name in REQUIRED_INITIAL_STATES if name not in states]
        if missing:
            raise ReadinessRefused(
                "READINESS_INITIAL_STATE_UNDECLARED",
                "the frozen runner contract does not declare %s"
                % ", ".join(sorted(missing)))
        self._states = {str(name): dict(value)
                        for name, value in states.items()
                        if isinstance(value, Mapping)}

    def postcondition(self, initial_state: str) -> str:
        return str(self._states[str(initial_state)]["postcondition"])

    def adapter_action(self, initial_state: str) -> str:
        return str(self._states[str(initial_state)]["adapterAction"])

    def postconditions_for(self, spec: ReadinessStateSpec) -> list[str]:
        return [self.postcondition(name) for name in spec.initial_states]


@dataclass
class _Confirmation:
    spec: ReadinessStateSpec
    confirmed: bool = False
    at: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


class O1ReadinessLedger:
    """The measured §10.2 order, and the only thing allowed to open the body.

    ``confirm`` refuses out-of-order transitions and refuses evidence that does
    not satisfy the state it claims.  ``require`` is what every measured-body
    entry point calls; it raises rather than returning a boolean so a caller
    cannot ignore it by accident.
    """

    def __init__(self, *, postconditions: FrozenReadinessPostconditions,
                 clock: Any = None) -> None:
        self.postconditions = postconditions
        self._clock = clock
        self._confirmations: dict[str, _Confirmation] = {
            spec.state: _Confirmation(spec=spec) for spec in READINESS_SEQUENCE}
        self._staged: dict[str, dict[str, Any]] = {
            spec.state: {} for spec in READINESS_SEQUENCE}
        self._teardown_order: list[str] = []
        self._lock = threading.RLock()
        self._drained = threading.Condition(self._lock)
        self._lifecycle_state = "ACTIVE"
        self._in_flight = 0
        # Historical evidence: after the body is closed for teardown the live
        # admission predicate is necessarily false, but the capture must still
        # say whether admission was ever legitimately opened for this run.
        self._measured_body_was_admitted = False

    # -- clock ------------------------------------------------------------
    def _now(self) -> str:
        clock = self._clock
        if clock is not None and hasattr(clock, "now"):
            return str(clock.now())
        from datetime import datetime, timezone

        moment = datetime.now(timezone.utc)
        return moment.strftime("%Y-%m-%dT%H:%M:%S.") + (
            "%03dZ" % (moment.microsecond // 1000))

    def bind_clock(self, clock: Any) -> None:
        with self._lock:
            self._clock = clock

    # -- transitions ------------------------------------------------------
    def stage(self, state: str, **evidence: Any) -> None:
        """Contribute evidence a different component measured.

        ``TRUST_READY`` spans the notification receiver (the consumer's) and the
        NETCONF session (the lifecycle's), and ``ASSURANCE_READY`` spans the
        Provider's acceptance record and the first retrieval cycle.  Staging
        keeps each fact with whoever actually observed it instead of letting one
        component assert another's measurement.
        """
        name = str(state)
        with self._lock:
            if name not in self._staged:
                raise ReadinessRefused(
                    "READINESS_STATE_UNKNOWN",
                    "%s is not a declared §10.2 readiness state" % name)
            self._staged[name].update(
                {str(key): value for key, value in evidence.items()})

    def confirm(self, state: str, *, evidence: Mapping[str, Any]) -> None:
        name = str(state)
        with self._lock:
            if self._lifecycle_state != "ACTIVE":
                raise ReadinessRefused(
                    "READINESS_%s" % self._lifecycle_state,
                    "%s cannot be confirmed after teardown admission closed"
                    % name)
            record = self._confirmations.get(name)
            if record is None:
                raise ReadinessRefused(
                    "READINESS_STATE_UNKNOWN",
                    "%s is not a declared §10.2 readiness state" % name)
            index = READINESS_ORDER.index(name)
            unmet = [earlier for earlier in READINESS_ORDER[:index]
                     if not self._confirmations[earlier].confirmed]
            if unmet:
                raise ReadinessRefused(
                    "READINESS_OUT_OF_ORDER",
                    "%s cannot be confirmed while %s is unconfirmed; §10.2 fixes "
                    "the order %s" % (name, unmet[0], " -> ".join(READINESS_ORDER)))
            payload = dict(self._staged[name])
            payload.update(
                {str(key): value for key, value in dict(evidence or {}).items()})
            spec = record.spec
            for key in spec.required_evidence:
                if key not in payload:
                    raise ReadinessRefused(
                        "READINESS_EVIDENCE_ABSENT",
                        "%s was claimed without the measured %s its postcondition "
                        "ranges over" % (name, key))
                if payload[key] in (None, "", [], {}):
                    raise ReadinessRefused(
                        "READINESS_EVIDENCE_ABSENT",
                        "%s was claimed with an empty %s" % (name, key))
            for key, wanted in spec.required_values:
                if payload.get(key) != wanted:
                    raise ReadinessRefused(
                        "READINESS_POSTCONDITION_UNMET",
                        "%s postcondition requires %s == %r; the run measured %r"
                        % (name, key, wanted, payload.get(key)))
            record.confirmed = True
            record.at = self._now()
            record.evidence = payload
            if not self.unmet(MEASURED_BODY_PRECONDITIONS):
                self._measured_body_was_admitted = True

    # -- queries ----------------------------------------------------------
    def confirmed(self, state: str) -> bool:
        with self._lock:
            record = self._confirmations.get(str(state))
            return bool(record is not None and record.confirmed)

    def confirmed_evidence(self, state: str) -> dict[str, Any]:
        """Return a copy of evidence only after the state was confirmed.

        Runtime correlation values must come from the lifecycle measurement
        that established readiness, not from a deployment fixture carrying a
        pre-create placeholder.  Refusing unconfirmed evidence keeps callers
        from observing staged or partially assembled state.
        """
        name = str(state)
        with self._lock:
            record = self._confirmations.get(name)
            if record is None:
                raise ReadinessRefused(
                    "READINESS_STATE_UNKNOWN",
                    "%s is not a declared §10.2 readiness state" % name)
            if not record.confirmed:
                raise ReadinessRefused(
                    "READINESS_EVIDENCE_UNCONFIRMED",
                    "%s evidence is not authoritative before confirmation" % name)
            return dict(record.evidence)

    def unmet(self, states: Sequence[str] = READINESS_ORDER) -> list[str]:
        with self._lock:
            return [name for name in states
                    if not self._confirmations[name].confirmed]

    @property
    def measured_body_admitted(self) -> bool:
        with self._lock:
            return self._lifecycle_state == "ACTIVE" and not self.unmet(
                MEASURED_BODY_PRECONDITIONS)

    @property
    def complete(self) -> bool:
        with self._lock:
            return not self.unmet(READINESS_ORDER)

    def require_measured_body(self, what: str) -> None:
        """Refuse to start the measured body until §10.2 3+4 are established."""
        with self._lock:
            if self._lifecycle_state != "ACTIVE":
                raise ReadinessRefused(
                    "READINESS_%s" % self._lifecycle_state,
                    "%s cannot start while the lifecycle is %s"
                    % (what, self._lifecycle_state))
            unmet = self.unmet(MEASURED_BODY_PRECONDITIONS)
            if unmet:
                raise ReadinessRefused(
                    "READINESS_INCOMPLETE",
                    "%s is part of the measured SC-084 body and cannot start while "
                    "%s remains unconfirmed" % (what, ", ".join(unmet)))

    def enter_measured_body(self, what: str) -> None:
        """Atomically admit one operation and include it in teardown drain."""
        with self._lock:
            self.require_measured_body(what)
            self._in_flight += 1

    def leave_measured_body(self) -> None:
        with self._lock:
            if self._in_flight <= 0:
                raise RuntimeError("measured-body in-flight accounting underflow")
            self._in_flight -= 1
            if self._in_flight == 0:
                self._drained.notify_all()

    @contextmanager
    def measured_body(self, what: str):
        self.enter_measured_body(what)
        try:
            yield
        finally:
            self.leave_measured_body()

    def begin_draining(self) -> None:
        """Close admission before the first cleanup transition."""
        with self._lock:
            if self._lifecycle_state == "STOPPED":
                return
            self._lifecycle_state = "DRAINING"

    def close_measured_admission(self, *, timeout_seconds: float) -> None:
        """Atomically close admission and wait for every admitted lease.

        The measured end boundary is valid only after no already-admitted
        operation can still append a row or increment a wire counter.  Closing
        and waiting share the same condition lock as admission accounting, so
        there is no gap in which a new lease can enter after the close.
        """
        import time

        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        with self._drained:
            if self._lifecycle_state == "STOPPED":
                return
            self._lifecycle_state = "DRAINING"
            while self._in_flight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ReadinessRefused(
                        "READINESS_DRAIN_TIMEOUT",
                        "%d measured operation(s) remained in flight after "
                        "admission closed" % self._in_flight)
                self._drained.wait(timeout=remaining)

    def mark_stopped(self) -> None:
        with self._lock:
            self._lifecycle_state = "STOPPED"

    @property
    def lifecycle_state(self) -> str:
        with self._lock:
            return self._lifecycle_state

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    def require_complete(self, what: str) -> None:
        with self._lock:
            unmet = self.unmet(READINESS_ORDER)
            if unmet:
                raise ReadinessRefused(
                    "READINESS_INCOMPLETE",
                    "%s requires every readiness postcondition; %s remains "
                    "unconfirmed" % (what, ", ".join(unmet)))

    # -- teardown ---------------------------------------------------------
    def note_teardown_step(self, step: str) -> None:
        """Record the drain order §10.2 item 9 fixes, in the order observed."""
        with self._lock:
            self._teardown_order.append(str(step))

    @property
    def teardown_order(self) -> list[str]:
        with self._lock:
            return list(self._teardown_order)

    # -- capture ----------------------------------------------------------
    def as_capture(self) -> dict[str, Any]:
        with self._lock:
            states = []
            for spec in READINESS_SEQUENCE:
                record = self._confirmations[spec.state]
                entry: dict[str, Any] = {
                    "state": spec.state,
                    "owner": spec.owner,
                    "initialStates": list(spec.initial_states),
                    "postconditions": self.postconditions.postconditions_for(spec),
                    "confirmed": bool(record.confirmed),
                    "evidence": _capture_safe(record.evidence),
                }
                if record.at is not None:
                    entry["at"] = record.at
                states.append(entry)
            return {
                "contractPointer": "/initialStates",
                "orderSource": "02-rapp-xapp-backend-mandatory-contract.1.0.1.md#10.2",
                "requiredInitialStates": list(REQUIRED_INITIAL_STATES),
                "states": states,
                "measuredBodyAdmitted": self._measured_body_was_admitted,
                "complete": self.complete,
                "unmet": self.unmet(READINESS_ORDER),
                "teardownOrder": list(self._teardown_order),
            }


def _capture_safe(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Evidence is scalars only: a capture member is not a place for objects."""
    safe: dict[str, Any] = {}
    for key, value in dict(evidence or {}).items():
        if isinstance(value, bool) or value is None:
            safe[str(key)] = value
        elif isinstance(value, (int, float)):
            safe[str(key)] = value
        else:
            safe[str(key)] = str(value)[:512]
    return safe


def readiness_is_complete(document: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Adjudicate a capture's readiness block from the document alone.

    Used by the capture validator, which must be able to judge a document it did
    not produce.  A missing block is not a pass: it is the 1.0.1 defect.
    """
    netconf = document.get("netconf")
    if not isinstance(netconf, Mapping):
        return False, ["READINESS_BLOCK_ABSENT"]
    readiness = netconf.get("readiness")
    if not isinstance(readiness, Mapping):
        return False, ["READINESS_BLOCK_ABSENT"]
    declared = [str(item) for item in readiness.get("requiredInitialStates", [])]
    if sorted(declared) != sorted(REQUIRED_INITIAL_STATES):
        return False, ["READINESS_REQUIRED_SET_MISMATCH"]
    states = readiness.get("states")
    if not isinstance(states, Sequence) or isinstance(states, (str, bytes)):
        return False, ["READINESS_STATES_MALFORMED"]
    observed = [str((entry or {}).get("state")) for entry in states
                if isinstance(entry, Mapping)]
    if observed != list(READINESS_ORDER):
        return False, ["READINESS_ORDER_MISMATCH"]
    unmet = [str((entry or {}).get("state")) for entry in states
             if isinstance(entry, Mapping) and not entry.get("confirmed")]
    if unmet:
        return False, ["READINESS_UNCONFIRMED:" + name for name in unmet]
    if readiness.get("complete") is not True:
        return False, ["READINESS_NOT_COMPLETE"]
    if readiness.get("measuredBodyAdmitted") is not True:
        return False, ["READINESS_BODY_NOT_ADMITTED"]
    return True, []


__all__ = [
    "FrozenReadinessPostconditions",
    "MEASURED_BODY_PRECONDITIONS",
    "O1ReadinessLedger",
    "PROVIDER_INITIAL_STATE",
    "READINESS_ORDER",
    "READINESS_SEQUENCE",
    "REQUIRED_ADAPTER_ACTIONS",
    "REQUIRED_INITIAL_STATES",
    "ReadinessRefused",
    "ReadinessStateSpec",
    "UPPER_INITIAL_STATES",
    "readiness_is_complete",
]
