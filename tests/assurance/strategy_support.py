"""The one views-and-transports fixture every ``test_strat_*.py`` acts on.

Not a test module (``discover`` collects ``test*.py`` only): it holds the
catalog view, evidence view and budget view design section 12's six strategies
are compared over, plus the scripted model answers that make the two LLM arms
hermetic.

The whole point of task section 11 is that the six strategies see *the same
thing*.  So there is one ``CATALOG``, one ``EVIDENCE``, one ``BUDGET`` here,
and a strategy that behaved differently because a test handed it a different
view would be an artefact of the test rather than a property of the strategy.

No network, no key, no model.  Every LLM arm in this suite runs behind
:class:`~assurance.advisors.strategies.transport.ScriptedTransport`; the one
test that would touch a real provider is skipped unless a run explicitly opts
in (see ``test_strat_boundary.py``).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Sequence

from assurance.advisors.catalog_view import CatalogEntry
from assurance.advisors.strategies import ScriptedTransport
from assurance.contracts.catalog import Candidate
from assurance.core.addressing import content_hash
from assurance.core.axes import CandidateAvailability

STAMP = "2026-08-25T09:00:00.000000Z"
EPOCH = content_hash({"epoch": "strategy-matrix"})
FOREIGN_EPOCH = content_hash({"epoch": "strategy-matrix-previous"})
CORRELATION = "case/strategy-matrix"

TARGET = "target/steer"
OTHER_TARGET = "target/other"


def candidate(
    candidate_id: str, *, target_ref: str = TARGET, capability_ref: str = "capability/steer"
) -> Candidate:
    return Candidate(
        candidate_id=candidate_id,
        target_ref=target_ref,
        option_ref="option/steer",
        parameters={"servingCell": "cell-2"},
        semantic_hash=content_hash({"candidate": candidate_id}),
        capability_ref=capability_ref,
    )


def entry(
    candidate_id: str,
    *,
    availability: CandidateAvailability = CandidateAvailability.AVAILABLE,
    target_ref: str = TARGET,
) -> CatalogEntry:
    return CatalogEntry(
        candidate=candidate(candidate_id, target_ref=target_ref), availability=availability
    )


#: Deliberately out of id order, with one locked entry and one candidate
#: bound to a different target.  Catalog order and lexicographic order differ,
#: so a strategy that claims a total order over ids has to prove it rather
#: than accidentally agreeing with the sequence it was handed.
CATALOG: Sequence[CatalogEntry] = (
    entry("candidate/000001"),
    entry("candidate/000000"),
    entry("candidate/000003", target_ref=OTHER_TARGET),
    entry("candidate/000002", availability=CandidateAvailability.LOCKED),
)

#: Ids a proposal may legitimately name.
AVAILABLE_IDS = ("candidate/000000", "candidate/000001", "candidate/000003")
LOCKED_ID = "candidate/000002"
UNKNOWN_ID = "candidate/999999"

#: One sealed cell, so a test can check no strategy plans against dormant
#: evidence (design section 8), and two open ones on the shared target.
EVIDENCE: Mapping[str, Any] = {
    "cell/open-a": {"status": "OPEN", "targetRef": TARGET, "sealed": False},
    "cell/partial-b": {"status": "PARTIAL", "targetRef": TARGET, "sealed": False},
    "cell/sealed-c": {"status": "OPEN", "targetRef": OTHER_TARGET, "sealed": True},
    "cell/closed-d": {"status": "CLOSED_PASS", "targetRef": TARGET, "sealed": False},
}
KNOWN_CELLS = tuple(sorted(EVIDENCE))
UNKNOWN_CELL = "cell/does-not-exist"

BUDGET: Mapping[str, Any] = {
    "trials_used": 0,
    "max_trials": 3,
    "proposals_used": 0,
    "max_proposals": 6,
    "deadline_at": "2026-08-25T10:00:00.000000Z",
    "active_vector": TARGET,
}

EXHAUSTED_BUDGET: Mapping[str, Any] = {
    "trials_used": 3,
    "max_trials": 3,
    "proposals_used": 6,
    "max_proposals": 6,
}


def views(**overrides: Any) -> Dict[str, Any]:
    """The identical keyword arguments every strategy's ``propose`` takes."""
    arguments: Dict[str, Any] = {
        "catalog_view": CATALOG,
        "evidence_view": EVIDENCE,
        "budget_view": BUDGET,
        "correlation_id": CORRELATION,
        "epoch_hash": EPOCH,
        "now": STAMP,
    }
    arguments.update(overrides)
    return arguments


# --------------------------------------------------------------------------- #
# Scripted model answers
# --------------------------------------------------------------------------- #


def intent_reply(cells: Sequence[str] = ("cell/open-a", "cell/partial-b"), note: str = "two open") -> str:
    return json.dumps({"outstandingCells": list(cells), "note": note})


def xapp_reply(
    applicable: Sequence[str] = ("candidate/000000",),
    inapplicable: Sequence[str] = ("candidate/000001",),
    needs: Sequence[str] = ("cell/open-a",),
) -> str:
    assessments: List[Dict[str, Any]] = []
    for candidate_id in applicable:
        assessments.append(
            {
                "candidateId": candidate_id,
                "applicable": True,
                "evidenceNeeds": list(needs),
                "riskNote": "steers one UE",
            }
        )
    for candidate_id in inapplicable:
        assessments.append(
            {"candidateId": candidate_id, "applicable": False, "riskNote": "not applicable"}
        )
    return json.dumps({"assessments": assessments})


def choice_reply(
    candidate_id: str = "candidate/000000",
    cells: Sequence[str] = ("cell/open-a",),
    rationale: str = "closes the open cell fastest",
    **extra: Any,
) -> str:
    body: Dict[str, Any] = {
        "candidateId": candidate_id,
        "evidenceCellRefs": list(cells),
        "rationale": rationale,
    }
    body.update(extra)
    return json.dumps(body)


def all_inapplicable_reply(candidates: Sequence[str] = AVAILABLE_IDS) -> str:
    """An xApp arm that rates every available candidate inapplicable.

    The degenerate verdict: with nothing applicable there is nothing for a
    later choice to contradict, so this is the shape that makes a
    consistency check skippable if the empty set is allowed through.
    """
    return json.dumps(
        {
            "assessments": [
                {"candidateId": candidate_id, "applicable": False, "riskNote": "no"}
                for candidate_id in candidates
            ]
        }
    )


def role_separated_script(
    *,
    candidate_id: str = "candidate/000000",
    applicable: Sequence[str] = ("candidate/000000",),
) -> List[str]:
    """One healthy three-call conversation for the proposed method."""
    return [
        intent_reply(),
        xapp_reply(applicable=applicable),
        choice_reply(candidate_id=candidate_id),
    ]


def scripted(replies: Sequence[Any], **kwargs: Any) -> ScriptedTransport:
    return ScriptedTransport(replies, **kwargs)
