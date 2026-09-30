"""JSON schemas, and the semantic checks a schema cannot express, for model
output on its way into a typed advisory.

Owner lane: **KAGT**.  New file (Gate 6, task section 11).

A model's answer arrives as text.  Between that text and an
:class:`~assurance.advisors.messages.AdvisoryMessage` sit four gates, and all
four have to hold for the same reason: the constraint list forbids using "LLM
free text, confidence or predicted effect as an admission bound, verdict,
closure or harm charge", and the only way to keep that true is for nothing
untyped to get past this module.

1. **Parse.** The text must contain one JSON object.  Prose and code fences
   around it are tolerated -- models emit them, and refusing on that would
   make the comparison a prompt-engineering result rather than a strategy
   result -- but the object itself is what is validated, in full.
2. **Schema.** Draft 2020-12, ``additionalProperties: false`` at every level,
   through the project's existing ``jsonschema`` dependency.  A field the
   schema has no room for is a refusal, not a field the caller must remember
   to ignore.
3. **Forbidden fields.** A recursive scan for every name in
   :data:`~assurance.advisors.messages.FORBIDDEN_ADVISORY_FIELDS`, matched
   past spelling: ``confidence``, ``harmCharge`` and ``harm_charge`` are the
   same attempt.  ``additionalProperties: false`` already rejects them, and
   this scan restates the invariant directly so it survives a schema edit.
4. **Catalog and evidence membership.** The check a schema *cannot* make: a
   candidate id that is not in the frozen catalog, or an evidence cell that is
   not in the case's view, is a hallucination.  Design section 6.3 --
   "agents cannot add, remove, or mutate candidates during an epoch" -- means
   naming one that does not exist has to fail here, not be silently created
   downstream.

Free text survives exactly one route: :func:`untrusted_text`, which caps it
and marks it.  Design section 11 keeps LIVE, REPLAY, DERIVED, UNKNOWN and
UNSUPPORTED visibly distinct on the Cockpit, and model prose belongs on the
same honest footing -- so it is prefixed rather than laundered into an
explanation that looks like the system's own.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, FrozenSet, Iterable, Mapping, Sequence, Set

from jsonschema import Draft202012Validator

from assurance.advisors.messages import FORBIDDEN_ADVISORY_FIELDS, MAX_EXPLANATION_CHARS

__all__ = [
    "COORDINATOR_CHOICE_SCHEMA",
    "INTENT_READING_SCHEMA",
    "MONOLITHIC_CHOICE_SCHEMA",
    "UNTRUSTED_TEXT_MARKER",
    "XAPP_ASSESSMENT_SCHEMA",
    "AdvisorySchemaError",
    "parse_role_output",
    "reject_forbidden_fields",
    "require_known_candidate",
    "require_known_cells",
    "untrusted_text",
]

#: Prefix stamped on every piece of model prose that reaches an Operator.
UNTRUSTED_TEXT_MARKER = "[untrusted-model-text]"

#: Model prose is capped well below the advisory's own
#: :data:`~assurance.advisors.messages.MAX_EXPLANATION_CHARS` ceiling.  The
#: advisory cap is a hard structural limit; this is the strategy's own budget
#: for prose, and keeping it smaller means a model cannot spend its whole
#: output allowance on an explanation nobody reads.
MAX_MODEL_PROSE_CHARS = 600


class AdvisorySchemaError(ValueError):
    """Model output could not be turned into a typed advisory.

    :attr:`reason` is a short stable code -- ``not-json``, ``schema``,
    ``forbidden-field``, ``unknown-candidate``, ``unknown-cell``,
    ``contradiction``, ``no-applicable-candidate`` -- so the deterministic
    fallback that follows records
    *which* gate refused, and task section 12's fallback-usage statistic can
    be broken down by cause instead of being one undifferentiated count.
    """

    def __init__(self, reason: str, detail: str, *, agent_role: str = "") -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail
        self.agent_role = agent_role


# --------------------------------------------------------------------------- #
# The three role schemas, plus the monolithic one-shot schema
# --------------------------------------------------------------------------- #

#: Role 1 -- the Intent Agent's reading of what the case still owes.  It names
#: obligations; it does not set them, and it carries no bound, because a bound
#: from a model would be a threshold (design section 4.2 forbids exactly that).
INTENT_READING_SCHEMA: Mapping[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["outstandingCells"],
    "properties": {
        "outstandingCells": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "maxItems": 64,
        },
        "note": {"type": "string"},
    },
}

#: Role 2 -- the xApp Agent's per-candidate assessment.  ``applicable`` is a
#: boolean and nothing softer: design section 4.2 gives the xApp role
#: "applicability, expected effect, risk, evidence needs", and a probability
#: here would be the confidence float GAP-02 removed, wearing another name.
XAPP_ASSESSMENT_SCHEMA: Mapping[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["assessments"],
    "properties": {
        "assessments": {
            "type": "array",
            "minItems": 1,
            "maxItems": 64,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["candidateId", "applicable"],
                "properties": {
                    "candidateId": {"type": "string", "minLength": 1},
                    "applicable": {"type": "boolean"},
                    "evidenceNeeds": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "maxItems": 64,
                    },
                    "riskNote": {"type": "string"},
                },
            },
        },
    },
}

#: Role 3 -- the Evidence Coordinator's choice.  One candidate, the cells it
#: would serve, and prose.  No schedule, no charge, no closure: the Kernel
#: decides whether a trial opens at all (design section 8).
COORDINATOR_CHOICE_SCHEMA: Mapping[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["candidateId"],
    "properties": {
        "candidateId": {"type": "string", "minLength": 1},
        "evidenceCellRefs": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "maxItems": 64,
        },
        "rationale": {"type": "string"},
    },
}

#: The monolithic strategy answers in exactly role 3's shape from one call.
#: Identical on purpose: the two LLM strategies must be indistinguishable to
#: everything downstream, or the comparison would be measuring the output
#: format rather than the coordination method.
MONOLITHIC_CHOICE_SCHEMA: Mapping[str, Any] = COORDINATOR_CHOICE_SCHEMA


#: One compiled validator per schema, built once at import.  Looked up by
#: object identity rather than by a name, so a caller cannot get the wrong
#: validator by passing a dict that merely resembles one of these.
_COMPILED = (
    (INTENT_READING_SCHEMA, Draft202012Validator(INTENT_READING_SCHEMA)),
    (XAPP_ASSESSMENT_SCHEMA, Draft202012Validator(XAPP_ASSESSMENT_SCHEMA)),
    (COORDINATOR_CHOICE_SCHEMA, Draft202012Validator(COORDINATOR_CHOICE_SCHEMA)),
)


def _validator_for(schema: Mapping[str, Any]) -> Draft202012Validator:
    for known, validator in _COMPILED:
        if schema is known:
            return validator
    return Draft202012Validator(schema)


# --------------------------------------------------------------------------- #
# Parsing and validation
# --------------------------------------------------------------------------- #

_FENCE = re.compile(r"```[a-zA-Z]*\s*(.*?)```", re.DOTALL)


def _normalise_field(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


#: Every forbidden field name reduced to letters and digits, so ``confidence``,
#: ``harmCharge`` and ``harm_charge`` all collapse onto the same token.
_FORBIDDEN_NORMALISED: FrozenSet[str] = frozenset(
    _normalise_field(name) for name in FORBIDDEN_ADVISORY_FIELDS
)


def _extract_object(text: str) -> Dict[str, Any]:
    """Pull the one JSON object out of a model's answer."""
    if not isinstance(text, str) or not text.strip():
        raise AdvisorySchemaError("not-json", "empty completion")
    fenced = _FENCE.search(text)
    body = fenced.group(1) if fenced else text
    start = body.find("{")
    end = body.rfind("}")
    if start < 0 or end <= start:
        raise AdvisorySchemaError("not-json", "no JSON object in the completion")
    try:
        parsed = json.loads(body[start : end + 1])
    except json.JSONDecodeError as exc:
        raise AdvisorySchemaError("not-json", f"{exc.msg} at position {exc.pos}") from exc
    if not isinstance(parsed, dict):
        raise AdvisorySchemaError("not-json", f"top level is {type(parsed).__name__}, not an object")
    return parsed


def reject_forbidden_fields(payload: Any, *, where: str) -> None:
    """Refuse any key naming something design section 4.2 reserves to the Kernel.

    Recursive, and matched past spelling.  A model that answers with a
    ``confidence`` is not making a formatting mistake -- it is offering a
    number that the legacy path compared against ``theta_star`` to decide
    admission (GAP-02).  There is no field for it to land in, and there is no
    path where ignoring it is safer than refusing.
    """
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if _normalise_field(key) in _FORBIDDEN_NORMALISED:
                raise AdvisorySchemaError(
                    "forbidden-field",
                    f"{where}: {key!r} is reserved to the Kernel and may not come from a model",
                )
            reject_forbidden_fields(value, where=where)
        return
    if isinstance(payload, (list, tuple)):
        for item in payload:
            reject_forbidden_fields(item, where=where)


def parse_role_output(
    text: str, *, schema: Mapping[str, Any], agent_role: str
) -> Dict[str, Any]:
    """Text in, validated plain dict out.

    Runs gates 1 to 3 (parse, schema, forbidden fields).  Membership checks
    need the catalog and the evidence view and so are separate calls the
    strategy makes with what it was handed.
    """
    payload = _extract_object(text)
    validator = _validator_for(schema)
    errors = sorted(validator.iter_errors(payload), key=lambda err: list(err.absolute_path))
    if errors:
        first = errors[0]
        location = "/".join(str(part) for part in first.absolute_path) or "<root>"
        raise AdvisorySchemaError("schema", f"{agent_role} at {location}: {first.message}")
    reject_forbidden_fields(payload, where=agent_role)
    return payload


# --------------------------------------------------------------------------- #
# Membership -- the checks a schema cannot make
# --------------------------------------------------------------------------- #


def require_known_candidate(candidate_id: str, available_ids: Iterable[str], *, agent_role: str) -> str:
    """Refuse a candidate that is not available in the frozen catalog.

    This is the hallucination gate.  The catalog is frozen per epoch and an
    advisory can only ever *name* something already in it; a model naming
    ``candidate/does-not-exist`` -- or naming a real candidate the Kernel has
    marked unavailable -- is refused here, before an advisory is built, so
    nothing downstream has to be able to tell the difference.
    """
    known = set(available_ids)
    if candidate_id not in known:
        raise AdvisorySchemaError(
            "unknown-candidate",
            f"{agent_role} named {candidate_id!r}, which is not an available catalog candidate",
            agent_role=agent_role,
        )
    return candidate_id


def require_known_cells(
    cell_refs: Sequence[str], known_cells: Iterable[str], *, agent_role: str
) -> tuple:
    """Refuse evidence cell ids the case's view does not contain.

    Returns the refs sorted, so two runs that named the same cells in a
    different order build the identical advisory.
    """
    known: Set[str] = set(known_cells)
    unknown = [ref for ref in cell_refs if ref not in known]
    if unknown:
        raise AdvisorySchemaError(
            "unknown-cell",
            f"{agent_role} named evidence cells outside the case view: {sorted(unknown)}",
            agent_role=agent_role,
        )
    return tuple(sorted(set(cell_refs)))


# --------------------------------------------------------------------------- #
# Free text
# --------------------------------------------------------------------------- #


def untrusted_text(text: Any, *, fallback: str = "") -> str:
    """Mark and cap model prose for the one place it is allowed to go.

    The result is display data: never parsed, never compared, never an input
    to a verdict or a candidate choice.  The marker is part of the string
    rather than a sibling flag, because a flag can be dropped by the next
    layer that copies the text and a prefix cannot.
    """
    if not isinstance(text, str) or not text.strip():
        return fallback[:MAX_EXPLANATION_CHARS]
    trimmed = " ".join(text.split())[:MAX_MODEL_PROSE_CHARS]
    return f"{UNTRUSTED_TEXT_MARKER} {trimmed}"[:MAX_EXPLANATION_CHARS]
