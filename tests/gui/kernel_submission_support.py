"""Fixtures for the Cockpit's Kernel submission path.

Not a test module -- the same convention as ``tests/assurance/kgw_support.py``
and ``vertical_support.py``: the name does not match ``test*.py``, so discovery
does not count what is in here as tests.

What it provides is the deployment half of a Kernel session: the objective
grammar a deployment offering PIN_TO_CELL would register with the Intent Agent,
and a builder that hands the Cockpit an already-wired
:class:`~assurance.vertical.VerticalPath` over the hardware-free mock actuation
adapter.  The contract family itself is
``tests/assurance/pin_to_cell_support.py`` -- the Gate 3 preparation expressed
PIN_TO_CELL in the new contract families there, and re-expressing it here would
be a second opinion about what the case is.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from assurance.advisors.grammar import IntentGrammarEntry
from assurance.contracts.target import ComparisonOperator
from assurance.gateway.mock_adapter import FaultInjection

from gui.operator.sources.kernel_live import MODE_MOCK, KernelSubmissionSession
from tests.assurance.pin_to_cell_support import (
    CELL_ID,
    HOME_NCI,
    TARGET_NCI,
    PinToCellFixture,
)

#: The grammar a deployment that offers UE cell steering registers.  Keywords
#: only -- the Intent Agent selects an objective from the registry and never
#: invents one, so this is the complete set of sentences it can recognise.
PIN_TO_CELL_GRAMMAR: Mapping[str, IntentGrammarEntry] = {
    "UeCellSteeringPinToCell": IntentGrammarEntry(
        objective_family="UeCellSteeringPinToCell",
        keywords=("pin", "serving cell"),
        measurement_ref="measurement/serving-cell-min",
        # A cell identity, so the drafted comparison is equality rather than a
        # direction; ``nci`` is the unit token the frozen counter carries.
        default_operator=ComparisonOperator.EQUAL,
        default_unit="nci",
    ),
}

#: The sentence an Operator types.  The cell identity precedes the scope token
#: because the grammar's number regex takes the first number in the sentence,
#: and ``ue-1`` contains one.
PIN_UTTERANCE: str = f"pin serving cell {TARGET_NCI} nci for ueId=ue-1"

#: The same sentence naming the cell the UE is already on -- a different
#: reading, therefore a different content hash, therefore a new confirmation.
HOME_UTTERANCE: str = f"pin serving cell {HOME_NCI} nci for ueId=ue-1"

#: A sentence no registry entry recognises.
UNSERVABLE_UTTERANCE: str = "raise the downlink throughput to 40 Mbps"


class KernelSubmissionFixture(PinToCellFixture):
    """A PIN_TO_CELL vertical path with a Cockpit session in front of it."""

    def session(
        self,
        *,
        observed: Sequence[int] = (TARGET_NCI,) * 5,
        pinned_nci: int = TARGET_NCI,
        faults: Optional[FaultInjection] = None,
        config: Optional[Mapping[str, Any]] = None,
        publish: Optional[Any] = None,
        mode: str = MODE_MOCK,
    ) -> KernelSubmissionSession:
        path = self.build(observed=observed, pinned_nci=pinned_nci,
                          faults=faults, config=config)
        self.path = path
        return KernelSubmissionSession(
            path=path,
            cell_id=CELL_ID,
            objective_registry=PIN_TO_CELL_GRAMMAR,
            mode=mode,
            publish=publish,
        )


__all__ = [
    "HOME_UTTERANCE", "KernelSubmissionFixture", "PIN_TO_CELL_GRAMMAR",
    "PIN_UTTERANCE", "UNSERVABLE_UTTERANCE",
]
