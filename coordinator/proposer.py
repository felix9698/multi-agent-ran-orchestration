#!/usr/bin/env python3
"""Batch E (P0-16): the immutable ProposerContext + backend-switch queue.

A ProposerContext freezes, at a PROPOSAL BOUNDARY, the proposer identity AND
the ACTUAL backend object/handle that the whole cycle must use - so parsing,
feasibility, and revision/alternative generation for one cycle can never be
served by a different backend even if the manager's mutable active backend is
externally switched mid-cycle. A switch requested while a cycle is active is
QUEUED and applied atomically only at the next proposal boundary.

Nothing here reaches the RAN, opens a socket, or touches hardware.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


def new_request_id() -> str:
    return f"swreq-{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class ProposerContext:
    """Immutable snapshot of the pinned proposer for ONE proposal boundary.

    ``backend_object`` is the ACTUAL pinned handle (compared by identity, not
    re-resolved by name); it is excluded from equality/serialization. The
    identity primitives (proposer_id / backend_name / model_version) and the
    cycle/proposal provenance are what the evidence, calibration and history
    attribution bind to.
    """
    proposer_id: str
    backend_name: str
    model_version: str
    # the pinned handle - identity only, not part of equality or to_dict()
    backend_object: Any = field(default=None, compare=False, repr=False)
    # provenance of the boundary
    cycle_index: int = 0
    cycle_id: Optional[str] = None
    proposal_id: Optional[str] = None
    episode_id: Optional[str] = None
    # the switch (if any) that produced this pin
    applied_request_id: Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            "proposer_id": self.proposer_id,
            "backend_name": self.backend_name,
            "model_version": self.model_version,
            "cycle_index": self.cycle_index,
            "cycle_id": self.cycle_id,
            "proposal_id": self.proposal_id,
            "episode_id": self.episode_id,
            "applied_request_id": self.applied_request_id,
        }

    def with_proposal(self, proposal_id: str) -> "ProposerContext":
        """A refined copy carrying the proposal id minted later at S2 (immutable
        - a NEW object, the backend pin is unchanged)."""
        return ProposerContext(
            proposer_id=self.proposer_id, backend_name=self.backend_name,
            model_version=self.model_version,
            backend_object=self.backend_object,
            cycle_index=self.cycle_index, cycle_id=self.cycle_id,
            proposal_id=proposal_id, episode_id=self.episode_id,
            applied_request_id=self.applied_request_id)


# switch-request statuses
SWITCH_PENDING = "pending"
SWITCH_APPLIED = "applied"
SWITCH_FAILED = "failed"
SWITCH_COALESCED = "coalesced"      # superseded by a later request (audited)
