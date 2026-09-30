"""Shared §14 assertions for the state a fault is required to preserve.

The fault tests exercise different entry points, but task section 14 requires
the same eight facts to survive every one: actual outcome, harm settlement,
reserve disposition, evidence inclusion, configuration, resource lock,
recovery and finite case terminal.  This module only normalizes the real
Kernel, gateway and adapter projections so each test can compare one literal
snapshot instead of scattering those assertions across the method.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Tuple

from assurance.core.axes import CaseTermination


EvidenceFact = Tuple[str, str, str, bool]
EvidenceCellFact = Tuple[str, str, Tuple[EvidenceFact, ...]]
HarmFact = Tuple[str, float, bool]


@dataclass(frozen=True)
class PreservationSnapshot:
    """The eight section-14 facts, in a directly comparable form."""

    actual_outcome: Optional[str]
    harm_settlement: Tuple[HarmFact, ...]
    reserve_outstanding: float
    evidence: Tuple[EvidenceCellFact, ...]
    configuration: Tuple[Tuple[str, Any], ...]
    locks: Tuple[Tuple[str, str], ...]
    recovery: Tuple[str, ...]
    trial_terminal: Optional[str]
    case_terminal: Optional[str]


def snapshot(fixture: Any, path: Any, *, trial_id: Optional[str]) -> PreservationSnapshot:
    """Read a preservation snapshot from the real reduced/event state."""

    state = fixture.kernel.reduced_state()
    case_id = path.case_id
    trial = state["trials"].get(trial_id) if trial_id is not None else None
    harm_entries = tuple(
        (
            str(entry["movementKind"]),
            float(entry["amount"]["value"]),
            bool(entry.get("chargedForMissingInterval")),
        )
        for entry in state["harmLedger"]
        if entry.get("caseId") == case_id
        and (trial_id is None or entry.get("trialId") == trial_id)
    )
    reserved = sum(amount for kind, amount, _ in harm_entries if kind == "RESERVE")
    returned = sum(amount for kind, amount, _ in harm_entries if kind == "RETURN")
    charged = sum(amount for kind, amount, _ in harm_entries if kind == "CHARGE")
    evidence = tuple(
        (
            str(cell_id),
            str(cell["status"]),
            tuple(
                (
                    str(item["executionValidity"]),
                    str(item["measurementSufficiency"]),
                    str(item["predicateVerdict"]),
                    bool(item.get("isPostClosureWitness")),
                )
                for item in cell.get("contributions", [])
            ),
        )
        for cell_id, cell in sorted(state["evidenceCells"].items())
        if cell.get("caseId") == case_id
    )
    recovery = tuple(
        str(envelope.payload["resolution"])
        for envelope in fixture.store.iterate()
        if envelope.event_kind == "TransactionResolved"
        and (trial_id is None or envelope.payload.get("trialId") == trial_id)
    )
    case = state["cases"][case_id]
    return PreservationSnapshot(
        actual_outcome=None if trial is None else str(trial["outcome"]),
        harm_settlement=harm_entries,
        reserve_outstanding=reserved - returned - charged,
        evidence=evidence,
        configuration=tuple(sorted(fixture.adapter.snapshot().items())),
        locks=tuple(sorted(state["resourceLocks"].items())),
        recovery=recovery,
        trial_terminal=None if trial is None else str(trial["state"]),
        case_terminal=case.get("terminal"),
    )


def assert_finite_terminal(test: Any, path: Any, expected: CaseTermination) -> None:
    """Assert termination is in the closed six-state set and cannot oscillate."""

    first = path.terminate()
    first_hash = path.terminal_state_hash()
    second = path.terminate()
    second_hash = path.terminal_state_hash()

    test.assertIs(first, expected)
    test.assertIs(second, expected)
    test.assertIn(first, tuple(CaseTermination))
    test.assertEqual(second_hash, first_hash)


__all__ = ["PreservationSnapshot", "assert_finite_terminal", "snapshot"]
