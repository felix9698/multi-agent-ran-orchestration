"""XApp Execution Assignment: the one runtime input a specialist xApp takes.

An assignment tells one xApp to execute one already-decided Action.  It is
not a request for an opinion: the xApp validates preconditions, executes,
reads back, and reports status.  Its report type has no field for a new
Action Proposal, and :data:`FORBIDDEN_REPORT_FIELDS` keeps that testable the
same way ``FORBIDDEN_ADVISORY_FIELDS`` does for advisories.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum
from typing import Any, Dict, FrozenSet, Mapping, Tuple

from assurance.core.timebase import is_utc_timestamp
from assurance.gateway.token import KernelToken
from assurance.xapps.execution import XAppExecutionPlan, XAppExecutionStep

__all__ = [
    "FORBIDDEN_REPORT_FIELDS",
    "XAppExecutionAssignment",
    "XAppExecutionReport",
    "XAppExecutionStatus",
    "assignment_for_step",
]


class XAppExecutionStatus(Enum):
    """The closed set of execution states an xApp may report."""

    ACCEPTED = "ACCEPTED"
    REJECTED_CAPABILITY = "REJECTED_CAPABILITY"
    REJECTED_STALE_STATE = "REJECTED_STALE_STATE"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ROLLBACK_SUCCEEDED = "ROLLBACK_SUCCEEDED"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"


#: Field names an execution report must never carry: each would turn the
#: executor back into a proposer, which is the structure this layer removes.
FORBIDDEN_REPORT_FIELDS: FrozenSet[str] = frozenset({
    "action_proposal",
    "candidate",
    "next_action",
    "proposed_parameters",
    "recommended_action",
    "recommendation",
})


@dataclass(frozen=True)
class XAppExecutionAssignment:
    """One Action assigned to one xApp, with everything execution needs."""

    assignment_id: str
    plan_id: str
    step_id: str
    xapp_id: str
    action_id: str
    parameters: Mapping[str, Any]
    target_selector: Mapping[str, Any]
    snapshot_id: str
    snapshot_hash: str
    preconditions: Tuple[str, ...]
    deadline: str
    permit_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", dict(self.parameters))
        object.__setattr__(self, "target_selector", dict(self.target_selector))
        object.__setattr__(self, "preconditions", tuple(self.preconditions))
        for name in ("assignment_id", "plan_id", "step_id", "xapp_id",
                     "action_id", "snapshot_id", "permit_ref"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not is_utc_timestamp(self.deadline):
            raise ValueError(f"deadline is not canonical UTC: {self.deadline!r}")


@dataclass(frozen=True)
class XAppExecutionReport:
    """Execution state and readback -- never a new Action Proposal."""

    assignment_id: str
    xapp_id: str
    action_id: str
    status: XAppExecutionStatus
    readback: Mapping[str, Any]
    completed_at: str
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.status, XAppExecutionStatus):
            raise ValueError("status must be an XAppExecutionStatus member")
        object.__setattr__(self, "readback", dict(self.readback))
        if not is_utc_timestamp(self.completed_at):
            raise ValueError(
                f"completed_at is not canonical UTC: {self.completed_at!r}")

    def to_canonical_dict(self) -> Dict[str, Any]:
        return {
            "assignmentId": self.assignment_id,
            "xappId": self.xapp_id,
            "actionId": self.action_id,
            "status": self.status.value,
            "readback": dict(self.readback),
            "completedAt": self.completed_at,
            "detail": self.detail,
        }


assert not (FORBIDDEN_REPORT_FIELDS
            & {f.name for f in fields(XAppExecutionReport)}), \
    "an execution report field collides with the forbidden proposer surface"


def assignment_for_step(
    plan: XAppExecutionPlan,
    step: XAppExecutionStep,
    *,
    permit: KernelToken,
    snapshot_hash: str,
) -> XAppExecutionAssignment:
    """Build the assignment for one plan step under one issued permit.

    The permit is referenced by content hash, so an executor can verify the
    exact token it was handed is the one the assignment was built for.
    ``snapshot_hash`` is the content hash of the common KPI snapshot the plan
    was made from; the executor re-checks it before touching anything.
    """
    if not isinstance(permit, KernelToken):
        raise TypeError(
            "an assignment requires a Kernel-issued Write Gateway permit "
            "(KernelToken); nothing else authorises an equipment write")
    return XAppExecutionAssignment(
        assignment_id=f"assignment/{plan.plan_id.split('/')[-1]}/{step.step_id}",
        plan_id=plan.plan_id,
        step_id=step.step_id,
        xapp_id=step.xapp_id,
        action_id=step.action_id,
        parameters=step.parameters,
        target_selector=step.target_selector,
        snapshot_id=plan.snapshot_ids[0],
        snapshot_hash=snapshot_hash,
        preconditions=tuple(
            f"{p.kind.value}:{p.subject}" for p in step.preconditions),
        deadline=plan.deadline,
        permit_ref=permit.content_hash(),
    )
