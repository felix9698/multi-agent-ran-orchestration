"""Runtime component identity.

Complete module: pure data, no owner.

Design section 4.8 of the task ("runtime component ID는 routing·correlation·
replay 용도로만 사용하고 승인 권한으로 사용하지 않는다") is the whole content
of this module.  A component id says *which process wrote this record* so an
event can be routed, correlated and replayed.  It never says *may this record
be trusted*: authority comes from the Kernel's deterministic rules and from a
Kernel-issued token, never from the sender's name.

The distinction matters because the advisory components are the ones most
likely to produce a well-formed message that should still be refused.  An
``INTENT_AGENT`` message is not admitted because it is from the Intent Agent;
it is admitted because it is a typed advisory of a kind the current epoch
accepts, and it is refused the moment it tries to be anything else.
"""

from __future__ import annotations

from enum import Enum
from typing import FrozenSet

__all__ = ["ADVISORY_COMPONENTS", "AUTHORITATIVE_COMPONENTS", "ComponentId"]


class ComponentId(Enum):
    """The components of the unified system (design section 4)."""

    #: The Research Operations Cockpit, acting for the one human Operator.
    OPERATOR_COCKPIT = "OPERATOR_COCKPIT"
    #: Natural language to typed contract draft (section 4.2).
    INTENT_AGENT = "INTENT_AGENT"
    #: Candidate applicability, expected effect, risk, evidence needs.
    XAPP_AGENT = "XAPP_AGENT"
    #: Proposes the next catalog candidate and evidence obligations.
    EVIDENCE_COORDINATOR = "EVIDENCE_COORDINATOR"
    #: The deterministic Assurance Kernel (section 4.3).
    ASSURANCE_KERNEL = "ASSURANCE_KERNEL"
    #: The only component that performs dynamic equipment changes (4.4).
    WRITE_GATEWAY = "WRITE_GATEWAY"
    #: Raw counters straight to the Kernel (section 4.5).
    MEASUREMENT_COLLECTOR = "MEASUREMENT_COLLECTOR"
    #: Preparation and teardown only; never a trial effect (section 4.6).
    LAB_SETUP_UTILITY = "LAB_SETUP_UTILITY"


#: The three advisory components.  Everything they emit is a proposal that the
#: Kernel may reject; none of them can add a candidate, assign a verdict,
#: charge harm, release a target vector or terminate a case (section 4.2).
ADVISORY_COMPONENTS: FrozenSet[ComponentId] = frozenset(
    {
        ComponentId.INTENT_AGENT,
        ComponentId.XAPP_AGENT,
        ComponentId.EVIDENCE_COORDINATOR,
    }
)

#: The only component whose events carry decisions.  Named for the boundary
#: tests: an event asserting a verdict, a ledger update or a token from any
#: other source is malformed, not merely unexpected.
AUTHORITATIVE_COMPONENTS: FrozenSet[ComponentId] = frozenset(
    {ComponentId.ASSURANCE_KERNEL}
)
