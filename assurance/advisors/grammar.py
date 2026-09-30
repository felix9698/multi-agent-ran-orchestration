"""A deterministic, LLM-free natural-language grammar for the Intent Agent.

Owner lane: **KAGT**.  New file, added under
``docs/architecture/SEAMS-GATE2.md`` section 3.

The brief for this gate is explicit: "Gate 2에서는 LLM 없이 결정론 파서(간단
문법)로 -- LLM 백엔드는 같은 인터페이스 뒤에 나중에."  This module is that
simple grammar: fixed keyword matching against a caller-supplied registry, a
number-plus-unit regex for the bound, and ``key=value`` tokens for scope.
Nothing here calls a model, reads a clock or draws randomness -- the same
utterance and registry always produce the same reading.

It never invents an objective.  :class:`IntentParseError` is raised when no
registry entry's keywords appear in the utterance at all, because at that
point there is nothing in the frozen registry to select -- design section 4.2
requires an unservable intent to come back honestly rather than as a
fabricated contract, and there is no honest field to put "the whole intent"
in when not even the objective could be identified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Optional, Tuple

from assurance.contracts.target import ComparisonOperator, TypedConstraint
from assurance.core.provenance import DocumentStatus, Provenance, TypedQuantity

__all__ = ["IntentGrammarEntry", "IntentParseError", "parse_utterance",
           "scope_selector"]


class IntentParseError(ValueError):
    """No grammar entry's keywords appear anywhere in the utterance."""


#: Comparison-direction keywords, checked case-insensitively.  Order matters:
#: the first matching group wins, so a caller wanting a different precedence
#: composes their own list rather than relying on this default.
_OPERATOR_KEYWORDS: Tuple[Tuple[Tuple[str, ...], ComparisonOperator], ...] = (
    (("at least", "이상", ">="), ComparisonOperator.GREATER_OR_EQUAL),
    (("at most", "이하", "<="), ComparisonOperator.LESS_OR_EQUAL),
)

_NUMBER_PATTERN = re.compile(r"(-?\d+(?:\.\d+)?)\s*([A-Za-z%]+)?")
_SCOPE_TOKEN_PATTERN = re.compile(r"([A-Za-z][A-Za-z0-9_]*)\s*[:=]\s*([A-Za-z0-9_.\-]+)")


@dataclass(frozen=True)
class IntentGrammarEntry:
    """One recognisable objective in the deterministic grammar.

    Attributes
    ----------
    objective_family:
        The registry key this entry drafts a constraint for -- a name the
        agent *selects*, never invents.
    keywords:
        Case-insensitive substrings whose presence in the utterance selects
        this entry.
    measurement_ref:
        The measurement contract id a drafted constraint would bind to.
    default_operator / default_unit:
        Used when the utterance names a numeric bound but not an explicit
        comparison direction or unit token.
    """

    objective_family: str
    keywords: Tuple[str, ...]
    measurement_ref: str
    default_operator: ComparisonOperator = ComparisonOperator.GREATER_OR_EQUAL
    default_unit: str = "1"


def _select_operator(utterance: str, default: ComparisonOperator) -> ComparisonOperator:
    lowered = utterance.lower()
    for keywords, operator in _OPERATOR_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return operator
    return default


def _select_bound(utterance: str, default_unit: str) -> Optional[Tuple[float, str]]:
    match = _NUMBER_PATTERN.search(utterance)
    if match is None:
        return None
    return float(match.group(1)), (match.group(2) or default_unit)


def scope_selector(utterance: str) -> Mapping[str, str]:
    """Pull ``key=value`` / ``key:value`` tokens out of *utterance*.

    Public because a composition root has to read the scope *before* it can
    build the grammar this module would otherwise parse against: which UE a
    sentence names decides which UE is observed, and the observed UE decides
    which contract bundle exists to parse the sentence with.  Exporting the one
    rule breaks that circle without giving anyone a second opinion about what
    ``ueId=`` means -- :func:`parse_utterance` reads the scope through this
    same function, so a root that selected one UE and a draft that scoped to
    another is not expressible.
    """
    return dict(_SCOPE_TOKEN_PATTERN.findall(utterance))


def parse_utterance(
    utterance: str,
    *,
    objective_registry: Mapping[str, IntentGrammarEntry],
    source_record: str,
) -> Tuple[str, Mapping[str, str], Tuple[TypedConstraint, ...], Tuple[str, ...]]:
    """Deterministically parse *utterance* against *objective_registry*.

    Returns ``(objective_family, scope_selector, proposed_constraints,
    unsupported_requests)`` -- exactly the fields
    :class:`~assurance.advisors.messages.IntentDraft` needs.  Every bound in
    ``proposed_constraints`` carries
    :attr:`~assurance.core.provenance.DocumentStatus.DRAFT`, so none of them
    is admissible until the Operator confirms it.

    Raises :class:`IntentParseError` when no entry matches at all.  When an
    entry matches but no numeric bound can be found, that half of the intent
    is reported through ``unsupported_requests`` instead -- a well-formed
    message with an honest gap, not a failure.
    """
    if not isinstance(utterance, str) or not utterance.strip():
        raise IntentParseError("utterance must be a non-empty string")

    lowered = utterance.lower()
    matched: Optional[IntentGrammarEntry] = None
    for entry in objective_registry.values():
        if any(keyword.lower() in lowered for keyword in entry.keywords):
            matched = entry
            break
    if matched is None:
        raise IntentParseError(f"no objective in the registry recognises: {utterance!r}")

    selected_scope = scope_selector(utterance)
    bound = _select_bound(utterance, matched.default_unit)
    if bound is None:
        return matched.objective_family, selected_scope, (), (
            f"no numeric bound found in: {utterance!r}",
        )

    value, unit = bound
    operator = _select_operator(utterance, matched.default_operator)
    constraint = TypedConstraint(
        measurement_ref=matched.measurement_ref,
        operator=operator,
        bound=TypedQuantity(
            value, unit, Provenance.EXPERIMENT_CONFIG, source_record, DocumentStatus.DRAFT,
        ),
    )
    return matched.objective_family, selected_scope, (constraint,), ()
