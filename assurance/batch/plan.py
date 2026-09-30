"""Content-addressed BatchPlan contract.

No batch plan is executable merely because it deserializes: the exact plan
content must be covered by a ``CONFIRM_BATCH_PLAN`` confirmation.  Consequently
every scope field participates in the canonical content hash.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import random
from typing import Any, Dict, Mapping, Tuple

from assurance.core.addressing import content_hash
from assurance.core.confirmation import ConfirmationAction, ConfirmationRecord


def _json_mapping(value: Mapping[str, Any], *, name: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    return dict(value)


@dataclass(frozen=True)
class IntentProfile:
    profile_id: str
    intent: Mapping[str, Any]
    scope: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or not self.profile_id.strip():
            raise ValueError("profile_id must be non-empty")
        object.__setattr__(self, "intent", _json_mapping(self.intent, name="intent"))
        object.__setattr__(self, "scope", _json_mapping(self.scope, name="scope"))

    def canonical_dict(self) -> Dict[str, Any]:
        return {"profileId": self.profile_id, "intent": dict(self.intent), "scope": dict(self.scope)}


@dataclass(frozen=True)
class Budget:
    cases: int = 100
    trials_per_case: int = 1
    harm: float = 100.0

    def __post_init__(self) -> None:
        if self.cases < 1 or self.trials_per_case < 1 or self.harm < 0:
            raise ValueError("budget values must be positive (harm may be zero)")

    def canonical_dict(self) -> Dict[str, Any]:
        return {"cases": self.cases, "trialsPerCase": self.trials_per_case, "harm": self.harm}


@dataclass(frozen=True)
class Windows:
    measurement_s: float = 1.0
    observation_s: float = 1.0
    hold_s: float = 1.0
    warmup_s: float = 0.0
    recovery_s: float = 0.0

    def __post_init__(self) -> None:
        if any(value < 0 for value in (self.measurement_s, self.observation_s, self.hold_s,
                                       self.warmup_s, self.recovery_s)):
            raise ValueError("windows must be non-negative")

    def canonical_dict(self) -> Dict[str, Any]:
        return {"measurementS": self.measurement_s, "observationS": self.observation_s,
                "holdS": self.hold_s, "warmupS": self.warmup_s, "recoveryS": self.recovery_s}


@dataclass(frozen=True)
class RetryPolicy:
    retries: int = 0
    abort_on_error: bool = True
    inclusion: str = "all_terminal"

    def __post_init__(self) -> None:
        if self.retries < 0:
            raise ValueError("retries must be non-negative")
        if self.inclusion not in {"all_terminal", "valid_only", "exclude_errors"}:
            raise ValueError("inclusion must be all_terminal, valid_only, or exclude_errors")

    def canonical_dict(self) -> Dict[str, Any]:
        return {"retries": self.retries, "abortOnError": self.abort_on_error,
                "inclusion": self.inclusion}


@dataclass(frozen=True)
class BatchCase:
    case_id: str
    objective: str
    profile: IntentProfile
    repeat_index: int
    seed: int
    order_index: int


@dataclass(frozen=True)
class BatchPlan:
    """A typed, fully execution-relevant plan.

    ``scope`` is a campaign-level restriction layered onto the profile scope.
    It is deliberately hashed alongside every schedule and budget value.
    """

    objectives: Tuple[str, ...]
    intent_profiles: Tuple[IntentProfile, ...]
    strategy: str
    repeats: int
    seed: int
    ordering: str = "randomized"
    budget: Budget = field(default_factory=Budget)
    windows: Windows = field(default_factory=Windows)
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    scope: Mapping[str, Any] = field(default_factory=dict)
    model: Mapping[str, Any] = field(default_factory=dict)
    topology: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "objectives", tuple(self.objectives))
        object.__setattr__(self, "intent_profiles", tuple(self.intent_profiles))
        if not self.objectives or any(not isinstance(value, str) or not value.strip() for value in self.objectives):
            raise ValueError("objectives must contain non-empty names")
        if not self.intent_profiles or not all(isinstance(p, IntentProfile) for p in self.intent_profiles):
            raise ValueError("intent_profiles must contain IntentProfile values")
        if not isinstance(self.strategy, str) or not self.strategy.strip():
            raise ValueError("strategy must be non-empty")
        if self.repeats < 1:
            raise ValueError("repeats must be at least one")
        if self.ordering not in {"randomized", "counterbalanced"}:
            raise ValueError("ordering must be randomized or counterbalanced")
        object.__setattr__(self, "scope", _json_mapping(self.scope, name="scope"))
        object.__setattr__(self, "model", _json_mapping(self.model, name="model"))
        object.__setattr__(self, "topology", _json_mapping(self.topology, name="topology"))
        if len(self.objectives) * len(self.intent_profiles) * self.repeats > self.budget.cases:
            raise ValueError("objective/profile/repeat matrix exceeds case budget")

    def canonical_dict(self) -> Dict[str, Any]:
        return {
            "objectives": list(self.objectives),
            "intentProfiles": [profile.canonical_dict() for profile in self.intent_profiles],
            "strategy": self.strategy, "repeats": self.repeats, "seed": self.seed,
            "ordering": self.ordering, "budget": self.budget.canonical_dict(),
            "windows": self.windows.canonical_dict(), "retry": self.retry.canonical_dict(),
            "scope": dict(self.scope), "model": dict(self.model), "topology": dict(self.topology),
        }

    @property
    def content_hash(self) -> str:
        return content_hash(self.canonical_dict())

    def confirm(self, *, event_id: str, timestamp: str | None = None) -> ConfirmationRecord:
        if timestamp is None:
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        return ConfirmationRecord(
            confirmed_object_type="BatchPlan", confirmed_content_hash=self.content_hash,
            event_id=event_id, timestamp=timestamp, action=ConfirmationAction.CONFIRM_BATCH_PLAN,
        )

    def is_confirmed_by(self, confirmation: ConfirmationRecord | None) -> bool:
        return bool(confirmation and confirmation.action is ConfirmationAction.CONFIRM_BATCH_PLAN
                    and confirmation.is_valid_for(self.content_hash))

    def require_confirmation(self, confirmation: ConfirmationRecord | None) -> None:
        if not self.is_confirmed_by(confirmation):
            raise ValueError("BatchPlan scope or content changed; CONFIRM_BATCH_PLAN is required")

    def with_scope(self, scope: Mapping[str, Any]) -> "BatchPlan":
        return replace(self, scope=dict(scope))

    def cases(self) -> Tuple[BatchCase, ...]:
        matrix = [(objective, profile, repeat)
                  for repeat in range(self.repeats)
                  for objective in self.objectives
                  for profile in self.intent_profiles]
        randomizer = random.Random(self.seed)
        if self.ordering == "randomized":
            randomizer.shuffle(matrix)
        else:
            width = len(self.objectives) * len(self.intent_profiles)
            ordered = []
            for repeat in range(self.repeats):
                block = matrix[repeat * width:(repeat + 1) * width]
                offset = repeat % width
                ordered.extend(block[offset:] + block[:offset])
            matrix = ordered
        return tuple(
            BatchCase(case_id=f"case-{index + 1:04d}", objective=objective, profile=profile,
                      repeat_index=repeat, seed=randomizer.randrange(0, 2**31), order_index=index)
            for index, (objective, profile, repeat) in enumerate(matrix)
        )
