"""The staged actuation plan and the configuration surface it moves.

Owner lane: **KGW**.

A plan is what the Kernel hands to
:meth:`~assurance.gateway.write_gateway.WriteGateway.prepare`, and it is the
*only* payload the gateway ever accepts from outside.  Every later operation --
commit, stop, reverse rollback, finalize -- takes a
:class:`~assurance.gateway.token.KernelToken` and nothing else, so once a plan
is staged under a transaction id there is no second door through which a
different payload can arrive.  That is the structural half of GATE1-MAP's
GAP-01: an advisory proposal cannot become an actuator binding, because the
binding is derived from the staged plan the Kernel authorised and addressed by
the transaction id printed on the permit.

The **configuration surface** is the other half.  A deployment has one
contracted set of axis names (design section 6.2's capability constraints), and
both the plan's baseline and the deployment's safe state must describe exactly
that set.  Two consequences follow, and both are load-bearing:

* the gateway can compute the digest of every intermediate configuration --
  baseline, after step 1, after step 2, ... -- which is what turns "some of the
  change is live" from a guess into an observation (see
  :meth:`ActuationPlan.prefix_hashes`);
* a plan that names an axis outside the surface is refused before anything is
  staged, rather than applying an axis nobody contracted a readback for.

Values are JSON-representable because they are hashed with the project's RFC
8785 canonicaliser; a value that cannot be canonicalised cannot be evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Dict, FrozenSet, Mapping, Optional, Sequence, Tuple

from assurance.core.addressing import content_hash

__all__ = [
    "ActuationPlan",
    "PlanError",
    "PlanStep",
    "config_hash",
    "frozen_config",
]


class PlanError(ValueError):
    """A plan or configuration is not admissible.

    Raised at parse time, before anything is staged, so a malformed plan can
    never become a half-staged transaction.
    """


def frozen_config(config: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return a read-only copy of *config* with validated axis names."""
    if not isinstance(config, Mapping) or not config:
        raise PlanError(f"a configuration is a non-empty mapping, got {config!r}")
    copied: Dict[str, Any] = {}
    for axis, value in config.items():
        if not isinstance(axis, str) or not axis.strip():
            raise PlanError(f"configuration axis names must be non-empty strings: {axis!r}")
        copied[axis] = value
    return MappingProxyType(copied)


def config_hash(config: Mapping[str, Any]) -> str:
    """Canonical digest of a configuration.

    The same function the gateway uses for the baseline, for every prefix and
    for the safe state, so "observed equals expected" is a comparison of two
    values produced the same way rather than of two conventions.
    """
    return content_hash({str(axis): value for axis, value in dict(config).items()})


@dataclass(frozen=True)
class PlanStep:
    """One axis write, in the order it will be applied.

    Order is part of the contract, not an implementation detail: reverse
    rollback undoes the steps backwards precisely because undoing them forwards
    can pass through a configuration that was never valid (design section 7).

    ``adapter`` is the *immutable per-step adapter key*.  A composition can
    reach the deployment over more than one registered client -- the PRIMARY
    steering policy over ``r1`` and a SUPPLEMENTARY UE cap over ``r1-cap`` --
    and which client carries which write is fixed when the plan is staged, not
    chosen at dispatch.  ``None`` means "the plan's own adapter", which is what
    a single-participant plan has always meant; a name here must be one the
    gateway has registered, and nothing in a command or a proposal can change
    it afterwards.
    """

    axis: str
    value: Any
    adapter: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.axis, str) or not self.axis.strip():
            raise PlanError(f"step axis must be a non-empty string, got {self.axis!r}")
        if self.adapter is not None and (
            not isinstance(self.adapter, str) or not self.adapter.strip()
        ):
            raise PlanError(
                f"step adapter must be a non-empty name or absent, got {self.adapter!r}"
            )

    def to_canonical_dict(self) -> Dict[str, Any]:
        # The key is present only when the step names an adapter of its own, so
        # a single-participant plan canonicalises exactly as it always did and
        # its recorded digest does not move under this change.
        record: Dict[str, Any] = {"axis": self.axis, "value": self.value}
        if self.adapter is not None:
            record["adapter"] = self.adapter
        return record


@dataclass(frozen=True)
class ActuationPlan:
    """A staged, side-effect-free description of one equipment change.

    Attributes
    ----------
    adapter:
        Name of the registered adapter that will carry every command for this
        transaction.  Resolved through the gateway's private registry, so the
        name is a lookup key and never an endpoint.
    scope:
        The contracted scope the change applies to, e.g.
        ``{"guAmfUeNgapId": ...}``.  Carried on every command so a downstream
        component can refuse a command aimed at a scope it does not serve.
    baseline_config:
        The full contracted configuration surface as the Kernel believes it is
        *before* the change.  Its digest must equal the permit's
        ``expected_config_hash``; that equality is what makes a blind overwrite
        impossible.
    steps:
        The axis writes, in apply order.  Non-empty, no repeated axis -- a plan
        that writes the same axis twice has no single reversal.
    watchdogs:
        Identifiers of the contract watchdogs that must be armed at ``ready``
        and are reported back as ``watchdog:<id>:armed`` evidence.  The
        Kernel refuses a commit whose arming it did not *observe* (task
        section 6.3), and the gateway is the only component that reaches the
        deployment, so the ids travel with the plan the Kernel authorised
        rather than being asserted by either side alone.  Empty is legitimate:
        a deployment with no contracted watchdog arms none.
    """

    adapter: str
    scope: Mapping[str, Any]
    baseline_config: Mapping[str, Any]
    steps: Tuple[PlanStep, ...]
    watchdogs: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.adapter, str) or not self.adapter.strip():
            raise PlanError(f"plan adapter must be a non-empty name, got {self.adapter!r}")
        if not isinstance(self.scope, Mapping) or not self.scope:
            raise PlanError("plan scope must be a non-empty mapping")
        object.__setattr__(self, "scope", MappingProxyType(dict(self.scope)))
        object.__setattr__(self, "baseline_config", frozen_config(self.baseline_config))
        steps = tuple(self.steps)
        if not steps:
            raise PlanError("a plan with no steps is not a change; refuse it here")
        for step in steps:
            if not isinstance(step, PlanStep):
                raise PlanError(f"plan steps must be PlanStep, got {type(step).__name__}")
        axes = [step.axis for step in steps]
        if len(set(axes)) != len(axes):
            raise PlanError(f"a plan writes each axis at most once, got {axes}")
        unknown = [axis for axis in axes if axis not in self.baseline_config]
        if unknown:
            raise PlanError(
                f"steps name axes outside the contracted configuration surface: {unknown}"
            )
        object.__setattr__(self, "steps", steps)
        watchdogs = tuple(self.watchdogs)
        for identifier in watchdogs:
            if not isinstance(identifier, str) or not identifier.strip():
                raise PlanError(
                    f"plan watchdog ids must be non-empty strings: {identifier!r}"
                )
        if len(set(watchdogs)) != len(watchdogs):
            raise PlanError(f"a plan arms each watchdog at most once, got {watchdogs}")
        object.__setattr__(self, "watchdogs", watchdogs)
        # Canonicalisability is checked here, not at commit: a value that
        # cannot be hashed cannot be verified, and finding that out with a
        # policy already created is the expensive way to learn it.
        config_hash(self.applied_config(len(steps)))

    # -- surfaces ----------------------------------------------------------

    @property
    def surface(self) -> FrozenSet[str]:
        """The contracted axis names this plan describes."""
        return frozenset(self.baseline_config)

    @property
    def axes(self) -> Tuple[str, ...]:
        """Axis names in apply order."""
        return tuple(step.axis for step in self.steps)

    @property
    def participants(self) -> Tuple[str, ...]:
        """Every adapter this plan writes through, in first-write order.

        The plan's own adapter leads, because it carries the PRIMARY change and
        a commit applies the steps in plan order.  Reverse rollback walks the
        steps backwards, so it necessarily unwinds the participants in the
        opposite order without a second list to keep consistent.
        """
        ordered = [self.adapter]
        for step in self.steps:
            name = step.adapter or self.adapter
            if name not in ordered:
                ordered.append(name)
        return tuple(ordered)

    def adapter_for(self, axis: str) -> str:
        """The adapter that writes *axis*, or the plan's own for an axis it does
        not move."""
        for step in self.steps:
            if step.axis == axis:
                return step.adapter or self.adapter
        return self.adapter

    def applied_config(self, count: int) -> Mapping[str, Any]:
        """The configuration after the first *count* steps have landed."""
        if not 0 <= count <= len(self.steps):
            raise PlanError(f"step count {count} is outside this plan")
        merged = dict(self.baseline_config)
        for step in self.steps[:count]:
            merged[step.axis] = step.value
        return MappingProxyType(merged)

    def prefix_hashes(self) -> Tuple[str, ...]:
        """Digest of the configuration after 0, 1, ... n steps.

        Index ``0`` is the baseline and index ``n`` is the fully applied
        change.  A configuration reread that matches index ``k`` for
        ``0 < k < n`` is a *positively observed* partial apply, which is the
        difference between reporting ``PARTIAL_APPLY`` and guessing.
        """
        return tuple(config_hash(self.applied_config(i)) for i in range(len(self.steps) + 1))

    def subset_hashes(self) -> frozenset:
        """Digest of every configuration in which each staged axis is at its
        baseline **or** at its planned value -- in any combination.

        2026-09-23: prefixes alone assume the steps land in staged order.  A1
        effects land asynchronously -- a scheduler weight takes effect at once, a
        steering handover seconds later -- so "the second step is live and the
        first not yet" is our own partial apply, not a foreign configuration.
        Live board ``formal38guarded-20260923T083611`` trial 3 (steer ue1 +
        pfWeight ue2): the commit read matched no prefix, the Kernel classed it
        as unknown and locked down **without reversing**, leaving both policies
        at the RIC.  ``2 ** steps`` digests; a 9-step joint plan is 512.
        """
        steps = list(self.steps)
        found = set()
        for mask in range(1 << len(steps)):
            merged = dict(self.baseline_config)
            for index, step in enumerate(steps):
                if mask >> index & 1:
                    merged[step.axis] = step.value
            found.add(config_hash(merged))
        return frozenset(found)

    @property
    def baseline_hash(self) -> str:
        return config_hash(self.baseline_config)

    @property
    def applied_hash(self) -> str:
        return config_hash(self.applied_config(len(self.steps)))

    # -- content addressing ------------------------------------------------

    def to_canonical_dict(self) -> Dict[str, Any]:
        return {
            "adapter": self.adapter,
            "scope": dict(self.scope),
            "baselineConfig": dict(self.baseline_config),
            "steps": [step.to_canonical_dict() for step in self.steps],
            "watchdogs": list(self.watchdogs),
        }

    def content_hash(self) -> str:
        """Digest of the staged plan, recorded in the durable transaction."""
        return content_hash(self.to_canonical_dict())

    @classmethod
    def from_mapping(cls, plan: Mapping[str, Any]) -> "ActuationPlan":
        """Parse the mapping form the frozen ``prepare`` signature accepts.

        Fail-closed on an unknown key: a plan carrying a field this gateway
        does not understand is a plan written against a different contract, and
        silently ignoring it is how an unapplied constraint becomes an applied
        change.
        """
        if not isinstance(plan, Mapping):
            raise PlanError(f"plan must be a mapping, got {type(plan).__name__}")
        allowed = {"adapter", "scope", "baselineConfig", "steps", "watchdogs"}
        required = {"adapter", "scope", "baselineConfig", "steps"}
        unknown = sorted(set(plan) - allowed)
        if unknown:
            raise PlanError(f"plan carries fields this gateway does not implement: {unknown}")
        missing = sorted(required - set(plan))
        if missing:
            raise PlanError(f"plan is missing required fields: {missing}")
        raw_steps = plan["steps"]
        if not isinstance(raw_steps, Sequence) or isinstance(raw_steps, (str, bytes)):
            raise PlanError("plan steps must be a sequence")
        steps = []
        for entry in raw_steps:
            if isinstance(entry, PlanStep):
                steps.append(entry)
                continue
            if not isinstance(entry, Mapping) or not {"axis", "value"} <= set(entry) \
                    or not set(entry) <= {"axis", "value", "adapter"}:
                raise PlanError(
                    f"a plan step is axis and value, optionally adapter, got {entry!r}"
                )
            steps.append(PlanStep(
                axis=entry["axis"], value=entry["value"], adapter=entry.get("adapter")
            ))
        watchdogs = plan.get("watchdogs", ())
        if isinstance(watchdogs, (str, bytes)) or not isinstance(watchdogs, Sequence):
            raise PlanError("plan watchdogs must be a sequence of identifiers")
        return cls(
            adapter=plan["adapter"],
            scope=plan["scope"],
            baseline_config=plan["baselineConfig"],
            steps=tuple(steps),
            watchdogs=tuple(watchdogs),
        )
