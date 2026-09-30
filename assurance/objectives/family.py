"""The seat one objective family fills, and the data it hands to everyone else.

Owner: none for the data types below (they are complete); lane **OBJ1/OBJ2/OBJ3**
for the family modules that subclass :class:`ObjectiveFamilyModule`.  See
``docs/architecture/SEAMS-GATE4.md``.

Task section 8 lists a twelve-item chain every objective family must complete.
Six of those items are code a family lane writes, and they are the six seats on
:class:`ObjectiveFamilyModule`:

===========================================  =======================================
task section 8 chain item                     seat
===========================================  =======================================
3 Target/Harm/Measurement contract            :meth:`~ObjectiveFamilyModule.contract_bundle`
4 finite candidate catalog                    :meth:`~ObjectiveFamilyModule.candidate_parameters`
5 R1 request/response/status lifecycle        :meth:`~ObjectiveFamilyModule.policy_lifecycle`
6 A1-P policy type/lifecycle and idempotency  :meth:`~ObjectiveFamilyModule.policy_lifecycle`
8 decision KPI and assurance KPI              :meth:`~ObjectiveFamilyModule.kpi_declaration`
9 apply/hold/success/…/terminal oracle        :meth:`~ObjectiveFamilyModule.terminal_oracle`
11 hardware-free scenario matrix              :meth:`~ObjectiveFamilyModule.hardware_free_expectations`
===========================================  =======================================

Items 1, 2 and 12 are not code: item 1 (project contract identifier and
standard mapping) is a declarative record in
:mod:`assurance.objectives.registry`, item 2 (intent semantics and the
clarification/unsupported answers) is the Intent Agent's grammar plus that same
record, and item 12 (OTA raw evidence) is a Gate 5 artefact.  Item 7 is the
E2 service-model mapping, which is *declared* in the registry and *actuated*
through the actuator binding in :meth:`~ObjectiveFamilyModule.contract_bundle`;
no module here reaches E2 itself.  Item 10 is the Cockpit's, which reads the
same correlation ids the Kernel already records.

Nothing in this module reaches an endpoint, and nothing here decides anything:
a bundle is contract *content*, and the only component that turns content into
an effect is the Write Gateway holding a Kernel permit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar, Mapping, Optional, Tuple

from assurance.contracts.capability import (
    ActuatorBinding,
    CapabilityManifest,
    CompositionManifest,
    DeploymentBinding,
)
from assurance.contracts.catalog import CoordinationCasePolicy
from assurance.contracts.harm import HarmContract, WatchdogContract
from assurance.contracts.measurement import CounterBinding, MeasurementContract
from assurance.contracts.target import (
    TargetContract,
    TargetReleasePolicy,
    TargetVector,
)
from assurance.core.axes import TrialOutcome
from assurance.core.states import StopReason, TrialState

__all__ = [
    "ExpectedConfiguration",
    "ExpectedOutcome",
    "KpiDeclaration",
    "KpiUse",
    "ObjectiveContractBundle",
    "ObjectiveFamilyModule",
    "PolicyLifecycle",
    "ScenarioName",
]


class ScenarioName(str, Enum):
    """The hardware-free scenario matrix of task section 8, chain item 11.

    Ten names, in the order the task lists them.  A ``str`` enum so a family
    module can key a plain mapping by the scenario without importing the test
    harness, which lives under ``tests/`` and must not be a runtime dependency.
    """

    POSITIVE = "positive"
    NEGATIVE = "negative"
    MALFORMED = "malformed"
    CONFLICT = "conflict"
    STALE = "stale"
    MISSING = "missing"
    TIMEOUT = "timeout"
    PARTIAL_EFFECT = "partial-effect"
    DUPLICATE = "duplicate"
    FAULT = "fault"


#: The six scenarios whose answer is a trial verdict, and which therefore need
#: a family-supplied expectation.  The other four -- malformed, conflict,
#: timeout, duplicate -- are refusals the architecture owns identically for
#: every family, so the shared harness asserts them itself rather than letting
#: a family declare what "refused" means.
VERDICT_SCENARIOS: Tuple[ScenarioName, ...] = (
    ScenarioName.POSITIVE,
    ScenarioName.NEGATIVE,
    ScenarioName.STALE,
    ScenarioName.MISSING,
    ScenarioName.PARTIAL_EFFECT,
    ScenarioName.FAULT,
)


class ExpectedConfiguration(Enum):
    """Where the deployment's configuration must end up after a scenario."""

    #: Back at the configuration the trial started from.
    BASELINE = "BASELINE"
    #: The candidate's configuration, durably finalized.
    APPLIED = "APPLIED"
    #: The contracted safe state.
    SAFE_STATE = "SAFE_STATE"
    #: Deliberately unconstrained: an incident lockdown leaves the equipment
    #: wherever the failed reversal left it, and asserting otherwise would be
    #: asserting that a failed rollback succeeded.
    UNCONSTRAINED = "UNCONSTRAINED"


@dataclass(frozen=True)
class ExpectedOutcome:
    """What one family expects one scenario to end as.

    Every field is a Kernel-owned value read back out of the trial record, not
    a summary the harness computes: the point of stating them here is that a
    family lane commits to a terminal state *before* running the matrix, so a
    scenario that quietly ends somewhere else fails instead of being described.
    """

    terminal_state: TrialState
    outcome: TrialOutcome
    stop_reason: Optional[StopReason] = None
    #: ``EvidenceCellStatus`` value, or ``None`` when the branch closes no cell.
    evidence_status: Optional[str] = None
    configuration: ExpectedConfiguration = ExpectedConfiguration.BASELINE
    #: Why this family expects that, in one line.  Not decoration: a lane that
    #: cannot write the sentence has not decided what the scenario means.
    rationale: str = ""


class KpiUse(Enum):
    """Which of the two KPI populations a measurement belongs to.

    Task section 8, chain item 8 keeps them apart deliberately.  A decision KPI
    steers candidate selection; an assurance KPI decides the verdict.  The same
    counter may appear in both, but never with the same cadence or freshness
    bound by accident -- stating the use is what makes that visible.
    """

    DECISION = "DECISION"
    ASSURANCE = "ASSURANCE"


@dataclass(frozen=True)
class KpiDeclaration:
    """One KPI's scope, unit, cadence, freshness and provenance.

    Plain declaration data for the Cockpit and the acceptance report.  The
    numbers that *judge* a trial live in the measurement contract; this record
    says which measurement plays which part, so a reader can tell a steering
    input from an evidence input without reading the evaluator.
    """

    measurement_ref: str
    use: KpiUse
    scope_level: str
    unit: str
    cadence_ms: int
    freshness_bound_ms: int
    source_interface: str
    provenance_note: str = ""


@dataclass(frozen=True)
class PolicyLifecycle:
    """The R1 and A1-P lifecycle one family's submission travels through.

    Task section 8, chain items 5 and 6.  Declaration only: no module in this
    package performs an R1 call.  ``policy_type_id`` is the identifier the
    *deployment* exposes; ``None`` means the current frozen deployment has no
    policy type for this family, which is a refusal to submit rather than a
    reason to invent one (design section 10, fail closed).
    """

    policy_type_id: Optional[str]
    #: Ordered R1/A1-P states this family's request passes through.
    states: Tuple[str, ...]
    #: What makes a re-submission of the same content a no-op rather than a
    #: second effect.
    idempotency_basis: str
    #: What the status source must report before the Kernel may treat the
    #: policy as enforced.  An A1 create is not that (task section 7.6).
    enforcement_evidence: str
    notes: str = ""


@dataclass(frozen=True)
class ObjectiveContractBundle:
    """One family's complete, cross-consistent contract set.

    The same shape ``tests/assurance/vertical_support.py`` and
    ``tests/assurance/pin_to_cell_support.py`` build by hand, named once so the
    shared scenario harness can drive any family without knowing which one it
    has.  It is data: it carries no behaviour beyond the admission order, which
    is a property of the Kernel's admission rules and not of any one family.
    """

    family: str
    counters: Tuple[CounterBinding, ...]
    measurements: Tuple[MeasurementContract, ...]
    target: TargetContract
    vector: TargetVector
    release: TargetReleasePolicy
    case_policy: CoordinationCasePolicy
    watchdogs: Tuple[WatchdogContract, ...]
    harm: HarmContract
    deployment: DeploymentBinding
    actuators: Tuple[ActuatorBinding, ...]
    capabilities: Tuple[CapabilityManifest, ...]
    composition: CompositionManifest
    #: The deployment's configuration surface at rest.
    baseline_config: Mapping[str, Any]
    #: The contracted safe configuration.
    safe_state: Mapping[str, Any]
    #: The entities the trial acts on.
    scope: Mapping[str, Any]
    #: Scope keys carried on a raw sample.
    sample_scope: Mapping[str, str]
    adapter_name: str = "mock"
    #: For a composite family: component family name -> its mandatory
    #: predicate ids.  Task section 8 forbids composing a combined objective
    #: out of separate passes, so the harness uses this to prove every
    #: component was judged inside one trial.
    component_predicates: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)

    def unconfirmed_contracts(self) -> Tuple[Any, ...]:
        """Contracts admitted without an Operator confirmation, in order.

        Order is not cosmetic: the Kernel resolves references as it admits, so
        a measurement admitted before its counter binding, or an actuator
        before its capability, is refused.
        """
        return (
            *self.counters,
            *self.measurements,
            self.target,
            self.release,
            *self.watchdogs,
            self.harm,
            *self.actuators,
            *self.capabilities,
            self.composition,
        )

    def confirmed_contracts(self) -> Tuple[Any, ...]:
        """The two objects the Operator confirms: the vector, then the policy.

        Design section 6.4 confirms the complete ordered target-vector list
        before execution; the case policy is confirmed with it because it is
        what bounds the run the Operator is agreeing to.
        """
        return (self.vector, self.case_policy)

    def all_contracts(self) -> Tuple[Any, ...]:
        return self.unconfirmed_contracts() + self.confirmed_contracts()

    def configuration_axes(self) -> Tuple[str, ...]:
        """Configuration keys any option in the target may change."""
        axes: list = []
        for option in self.target.options:
            for key in option.parameter_space:
                if key not in axes:
                    axes.append(key)
        return tuple(axes)


class ObjectiveFamilyModule:
    """The six frozen seats one objective family lane fills.

    Signatures frozen by ``docs/architecture/SEAMS-GATE4.md``; bodies owned by
    the lane named in :attr:`lane`.  Every seat raises rather than returning a
    placeholder, because a seat that returned an empty bundle would let the
    shared harness report a matrix pass over nothing at all -- which is exactly
    the "supported on name and schema alone" that Gate 4 forbids.

    A family module is a description, not an actor.  It builds contract
    content, declares which published policy/interface/service-model versions
    its content applies, and states what it expects each hardware-free scenario
    to end as.  It never opens a trial, never writes and never judges: the
    Kernel does all three.
    """

    #: Project contract identifier of the family, e.g.
    #: ``"TrafficSteeringPreference"``.  A project name, never presented as an
    #: ETSI or O-RAN objective name (task section 7.7).
    family: ClassVar[str] = ""
    #: Owning lane from ``docs/architecture/SEAMS-GATE4.md`` section 3.
    lane: ClassVar[str] = ""

    def contract_bundle(
        self, *, scope: Mapping[str, Any], deployment_binding: DeploymentBinding
    ) -> ObjectiveContractBundle:
        """Build this family's complete contract set for one scope.

        Signature frozen by ``docs/architecture/SEAMS-GATE4.md``; body owned by
        the lane in :attr:`lane`.

        The returned bundle must pass
        :func:`assurance.contracts.validation.validate_family_set` unchanged --
        every ``*_ref`` resolving inside the set, an actuator on the
        ``OFFICIAL_ORAN_DYNAMIC`` path naming a readback measurement and a
        rollback, and an objective-bearing contract carrying its standard
        mapping.  A family whose deployment cannot supply one of those raises
        instead of shipping a bundle with the field left blank.
        """
        raise NotImplementedError(
            f"objective family {self.family!r}: contract_bundle is owned by lane "
            f"{self.lane} (docs/architecture/SEAMS-GATE4.md)"
        )

    def candidate_parameters(self) -> Mapping[str, Tuple[str, ...]]:
        """The finite parameter space the candidate catalog is generated over.

        Signature frozen by ``docs/architecture/SEAMS-GATE4.md``; body owned by
        the lane in :attr:`lane`.

        Finite by construction: the epoch freezes cardinality, membership and
        catalog hash (design section 6.3), so an unbounded range would leave
        the frozen catalog undefined.  Keys are configuration axes, values are
        the admissible settings for each.
        """
        raise NotImplementedError(
            f"objective family {self.family!r}: candidate_parameters is owned by lane "
            f"{self.lane} (docs/architecture/SEAMS-GATE4.md)"
        )

    def policy_lifecycle(self) -> PolicyLifecycle:
        """Declare the R1/A1-P lifecycle a submission for this family uses.

        Signature frozen by ``docs/architecture/SEAMS-GATE4.md``; body owned by
        the lane in :attr:`lane`.

        Declaration only -- nothing here calls R1.  A family with no policy
        type in the current frozen deployment returns one whose
        ``policy_type_id`` is ``None`` and says so in ``notes``; it does not
        borrow another family's type.
        """
        raise NotImplementedError(
            f"objective family {self.family!r}: policy_lifecycle is owned by lane "
            f"{self.lane} (docs/architecture/SEAMS-GATE4.md)"
        )

    def kpi_declaration(self) -> Tuple[KpiDeclaration, ...]:
        """Declare each measurement's part, scope, unit, cadence and freshness.

        Signature frozen by ``docs/architecture/SEAMS-GATE4.md``; body owned by
        the lane in :attr:`lane`.

        Task section 8, chain item 8.  Every measurement referenced by a target
        predicate must appear here with :attr:`KpiUse.ASSURANCE`; a KPI used
        only to choose among candidates appears with :attr:`KpiUse.DECISION`.
        """
        raise NotImplementedError(
            f"objective family {self.family!r}: kpi_declaration is owned by lane "
            f"{self.lane} (docs/architecture/SEAMS-GATE4.md)"
        )

    def terminal_oracle(self, evaluation: Mapping[str, Any]) -> TrialOutcome:
        """Read the Kernel's evaluation record and name the terminal outcome.

        Signature frozen by ``docs/architecture/SEAMS-GATE4.md``; body owned by
        the lane in :attr:`lane`.

        Not a second opinion: ``evaluation`` is the trial record's own
        ``evaluation`` mapping -- execution validity, measurement sufficiency
        and the per-predicate verdicts the Kernel already produced -- and the
        oracle's job is to state, for this family, which of those combinations
        is a success, which is invalid, which is incomplete and which is a
        rollback.  Returning ``SUCCESS`` where the Kernel recorded anything
        other than a passing mandatory set is a defect, not a family choice.
        """
        raise NotImplementedError(
            f"objective family {self.family!r}: terminal_oracle is owned by lane "
            f"{self.lane} (docs/architecture/SEAMS-GATE4.md)"
        )

    def hardware_free_expectations(self) -> Mapping[ScenarioName, ExpectedOutcome]:
        """State what each verdict-bearing scenario must end as.

        Signature frozen by ``docs/architecture/SEAMS-GATE4.md``; body owned by
        the lane in :attr:`lane`.

        Must cover every member of :data:`VERDICT_SCENARIOS`.  The remaining
        four scenarios -- malformed, conflict, timeout, duplicate -- are
        architecture-wide refusals the shared harness asserts for every family
        identically, so a family that declared its own answer for them would be
        declaring the architecture, not its objective.
        """
        raise NotImplementedError(
            f"objective family {self.family!r}: hardware_free_expectations is owned by "
            f"lane {self.lane} (docs/architecture/SEAMS-GATE4.md)"
        )
