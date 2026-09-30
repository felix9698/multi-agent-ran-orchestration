#!/usr/bin/env python3
"""
Strict schema + finite-number validation for the LLM decision boundaries
(CLI handoff Batch C, P0-5).

Every model-facing boundary of the coordination loop - intent parse,
feasibility, alternatives, action proposal, and the negotiation-policy
callback - must reject malformed input BEFORE it can influence routing,
clipping, or actuation.  The pre-Batch-C parser (``content.find('{')`` +
``json.loads``) was fail-OPEN in four concrete ways:

  * the string ``"false"`` is truthy, so ``"feasible":"false"`` routed to a
    trial;
  * Python's ``json.loads`` accepts the non-standard constants ``NaN`` /
    ``Infinity`` / ``-Infinity``, and a ``NaN`` action clipped to a boundary;
  * ``json.loads`` silently keeps the LAST value of a DUPLICATE key, so a
    conflicting proposal parsed cleanly;
  * an unknown action key was silently dropped instead of rejecting the
    proposal.

This module is the single, model-agnostic source of that validation (section
3.6: schema validation is a SYSTEM responsibility, identical for every
backend).  It is stdlib-only, raises :class:`SchemaError` with a precise
reject reason on any violation, and never coerces.

Nothing here reaches the RAN, opens a socket, or touches hardware.
"""

from __future__ import annotations

import json
import math
from typing import Any, Dict, Iterable, List, Optional, Tuple


class SchemaError(ValueError):
    """A strict-schema / finite-number rejection at a decision boundary.

    Carries the human-readable reject reason as ``str(err)``; the coordinator
    stores it verbatim in the evidence record's schema verdict (P0-5) so an
    audit can see WHICH boundary and WHICH field failed."""


# ---------------------------------------------------------------------------
# Strict JSON parsing (reject NaN/Infinity/-Infinity + duplicate keys)
# ---------------------------------------------------------------------------

def _reject_constant(token: str) -> Any:
    """``parse_constant`` hook: Python's json accepts the non-standard
    constants ``NaN`` / ``Infinity`` / ``-Infinity`` by default.  A finite
    contract cannot allow any of them, so reject at parse time (before a
    ``NaN`` can be float()'d and clipped to an axis boundary)."""
    raise SchemaError(f"non-standard JSON constant {token!r} is rejected "
                      f"(NaN/Infinity are not finite)")


def _reject_duplicate_keys(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    """``object_pairs_hook``: ``json.loads`` keeps only the LAST value of a
    duplicated key, hiding a conflicting/ambiguous object.  Reject any object
    that carries the same key twice."""
    seen: Dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise SchemaError(f"duplicate JSON key {key!r} is rejected "
                              f"(ambiguous object)")
        seen[key] = value
    return seen


def strict_loads(text: str) -> Any:
    """``json.loads`` with the finite-constant and duplicate-key rejections
    engaged.  Raises :class:`SchemaError` for malformed JSON, a non-standard
    constant, or a duplicate key."""
    try:
        return json.loads(text, parse_constant=_reject_constant,
                          object_pairs_hook=_reject_duplicate_keys)
    except SchemaError:
        raise
    except (ValueError, TypeError) as e:
        raise SchemaError(f"malformed JSON: {e}")


def strict_extract_object(content: str) -> Dict[str, Any]:
    """Extract the outermost ``{...}`` object from raw model content and parse
    it strictly.

    Mirrors the historical extraction (a real backend wraps its JSON in prose)
    but parses with :func:`strict_loads`, so a duplicate key or a ``NaN``
    ANYWHERE inside the object is rejected rather than silently accepted.  The
    result must be a JSON object (dict)."""
    if not isinstance(content, str):
        raise SchemaError(f"model content is not text ({type(content).__name__})")
    start = content.find("{")
    end = content.rfind("}")
    if start < 0 or end <= start:
        raise SchemaError("no JSON object found in model content")
    obj = strict_loads(content[start:end + 1])
    if not isinstance(obj, dict):
        raise SchemaError("model content is not a JSON object")
    return obj


# ---------------------------------------------------------------------------
# Typed field validators (JSON booleans only; finite numbers only)
# ---------------------------------------------------------------------------

def require_json_bool(value: Any, field: str) -> bool:
    """A boolean field MUST be a JSON boolean (Python ``bool``).  The strings
    ``"true"``/``"false"`` and the ints ``0``/``1`` are rejected - the pre-fix
    truthiness of ``"false"`` is exactly the counterexample (P0-5)."""
    if isinstance(value, bool):
        return value
    raise SchemaError(f"field {field!r} must be a JSON boolean, got "
                      f"{value!r} ({type(value).__name__})")


def require_finite_number(value: Any, field: str) -> float:
    """A numeric field MUST be a finite JSON number.  Rejects bools (JSON
    ``true`` is not a number), strings, and non-finite values (defence in
    depth even though :func:`strict_loads` already rejects the NaN/Infinity
    *constants* - a value can still arrive non-finite via arithmetic upstream
    or a hand-built dict)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchemaError(f"field {field!r} must be a finite number, got "
                          f"{value!r} ({type(value).__name__})")
    fv = float(value)
    if not math.isfinite(fv):
        raise SchemaError(f"field {field!r} must be finite, got {value!r}")
    return fv


def require_unit_interval(value: Any, field: str) -> float:
    """A confidence/probability field MUST be a finite number in ``[0, 1]``."""
    fv = require_finite_number(value, field)
    if fv < 0.0 or fv > 1.0:
        raise SchemaError(f"field {field!r} must be in [0,1], got {fv!r}")
    return fv


def reject_unknown_keys(data: Dict[str, Any], allowed: Iterable[str],
                        where: str) -> None:
    """Reject an object carrying any key outside ``allowed`` (P0-5: an unknown
    field is a whole-object rejection, never silently ignored)."""
    extra = set(data) - set(allowed)
    if extra:
        raise SchemaError(f"unknown field(s) {sorted(extra)} in {where}")


def require_keys(data: Dict[str, Any], required: Iterable[str],
                 where: str) -> None:
    """Reject an object missing any required key (P0-5)."""
    missing = [k for k in required if k not in data]
    if missing:
        raise SchemaError(f"missing required field(s) {missing} in {where}")


# ---------------------------------------------------------------------------
# Boundary schemas
# ---------------------------------------------------------------------------

# Allowed top-level keys of a feasibility object (the system-prompt contract).
_FEASIBILITY_KEYS = frozenset({
    "feasible", "confidence", "reasoning", "proposed_config", "expected_kpi",
    "alternatives",
})
_FEASIBILITY_REQUIRED = ("feasible", "confidence")


def validate_feasibility(content: str) -> Dict[str, Any]:
    """Strictly validate the S2 feasibility object from raw model content.

    Enforces (P0-5): well-formed JSON with no NaN/Infinity/duplicate key;
    ``feasible`` is a JSON boolean; ``confidence`` is a finite number in
    ``[0, 1]``; no unknown top-level field.  ``proposed_config`` values are
    validated separately at the ACTION boundary (:func:`validate_action_proposal`)
    because the canonical key set depends on the installed topology.  Returns
    the validated object (booleans/floats normalised); raises
    :class:`SchemaError` otherwise."""
    data = strict_extract_object(content)
    reject_unknown_keys(data, _FEASIBILITY_KEYS, "feasibility")
    require_keys(data, _FEASIBILITY_REQUIRED, "feasibility")
    feasible = require_json_bool(data["feasible"], "feasible")
    confidence = require_unit_interval(data["confidence"], "confidence")
    out = dict(data)
    out["feasible"] = feasible
    out["confidence"] = confidence
    # Optional fields, WHEN PRESENT, cannot be null and must be the exact type
    # (P0-5, coordinator review): no null/coercion fail-open.
    if "reasoning" in data:
        _require_str(data["reasoning"], "reasoning")
    if "proposed_config" in data:
        if not isinstance(data["proposed_config"], dict):
            raise SchemaError("field 'proposed_config' must be an object")
    if "expected_kpi" in data:
        _validate_kpi_object(data["expected_kpi"], "expected_kpi")
    if "alternatives" in data:
        if not isinstance(data["alternatives"], list):
            raise SchemaError("field 'alternatives' must be an array")
        _validate_alternative_list(data["alternatives"], "alternatives")
    return out


def _validate_kpi_object(obj: Any, field: str) -> None:
    """A KPI object (e.g. ``expected_kpi``) must be an object whose values are
    all finite numbers (P0-5)."""
    if not isinstance(obj, dict):
        raise SchemaError(f"field {field!r} must be an object")
    for k, v in obj.items():
        require_finite_number(v, f"{field}[{k!r}]")


def validate_action_proposal(proposed: Dict[str, Any],
                             canonical_keys: Iterable[str]
                             ) -> Dict[str, float]:
    """Strictly validate a proposed action vector against the EXACT canonical
    action key set (P0-5).

    ``canonical_keys`` is the closed set of action keys the installed topology
    can address (per-cell axes x configured gNBs + per-UE axes x configured
    UEs).  Every proposal key MUST be in that set (an unknown action key
    rejects the whole proposal - never silently dropped) and every value MUST
    be a finite number (a ``NaN``/``Infinity`` action is rejected before it can
    reach the executor).  Returns a canonical ``{key: float}`` mapping; raises
    :class:`SchemaError` on the first violation."""
    if proposed is None:
        return {}
    if not isinstance(proposed, dict):
        raise SchemaError("proposed_config must be an object")
    allowed = set(canonical_keys)
    out: Dict[str, float] = {}
    for key, value in proposed.items():
        if key not in allowed:
            raise SchemaError(
                f"unknown action key {key!r} rejects the whole proposal "
                f"(not in the canonical action key set)")
        out[key] = require_finite_number(value, f"proposed_config[{key!r}]")
    return out


# --- intent parse schema (P0-5, coordinator review) -----------------------

_INTENT_KEYS = frozenset({"type", "constraint", "value", "unit", "scope",
                         "description"})
# The FULL required intent envelope (coordinator review): EVERY key is required
# via require_keys - including ``value`` (a missing value is a require_keys
# rejection, and a present value is additionally validated as a finite JSON
# number below).
_INTENT_REQUIRED = ("type", "constraint", "value", "unit", "scope",
                    "description")
_SCOPE_KEYS = frozenset({"ue_ids", "bs_ids"})
_SCOPE_REQUIRED = ("ue_ids", "bs_ids")


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or isinstance(value, bool):
        raise SchemaError(f"field {field!r} must be a string, got "
                          f"{value!r} ({type(value).__name__})")
    return value


def _require_str_array(value: Any, field: str) -> None:
    if not isinstance(value, list):
        raise SchemaError(f"field {field!r} must be an array, got "
                          f"{type(value).__name__}")
    for i, item in enumerate(value):
        if not isinstance(item, str) or isinstance(item, bool):
            raise SchemaError(
                f"{field}[{i}] must be a string, got {item!r}")


def validate_intent_parse(data: Dict[str, Any]) -> Dict[str, Any]:
    """Strictly validate a parsed-intent object (P0-5, coordinator review).

    Requires the full required key set with exact JSON types: ``value`` a JSON
    NUMBER (not bool/string); ``scope`` an OBJECT carrying only ``ue_ids`` /
    ``bs_ids``, each an array of strings; ``type``/``constraint`` strings.
    Rejects any nested unknown / missing / type-invalid field.  Returns the
    object; raises :class:`SchemaError` (the caller maps it to
    InputSchemaRejected)."""
    if not isinstance(data, dict):
        raise SchemaError("intent parse output is not a JSON object")
    reject_unknown_keys(data, _INTENT_KEYS, "intent parse")
    require_keys(data, _INTENT_REQUIRED, "intent parse")
    _require_str(data["type"], "type")
    _require_str(data["constraint"], "constraint")
    _require_str(data["unit"], "unit")
    _require_str(data["description"], "description")
    # ``value`` is REQUIRED (enforced above via require_keys) and additionally
    # validated HERE as a finite JSON number - not delegated outside the
    # validator (coordinator review).  Messages keep the substrings the Batch-A
    # tests pin ("non-numeric" / "non-finite").
    _v = data["value"]
    if isinstance(_v, bool) or not isinstance(_v, (int, float)):
        raise SchemaError(f"non-numeric target value {_v!r}")
    if not math.isfinite(float(_v)):
        raise SchemaError(f"non-finite target value {_v!r}")
    scope = data["scope"]
    if not isinstance(scope, dict):
        raise SchemaError("field 'scope' must be an object")
    reject_unknown_keys(scope, _SCOPE_KEYS, "intent scope")
    require_keys(scope, _SCOPE_REQUIRED, "intent scope")
    _require_str_array(scope["ue_ids"], "scope.ue_ids")
    _require_str_array(scope["bs_ids"], "scope.bs_ids")
    return data


# Allowed keys of one alternative object + the REQUIRED prompt fields.
_ALT_KEYS = frozenset({"id", "description", "target_value", "value",
                       "actions", "confidence"})
_ALT_REQUIRED = ("id", "description", "target_value", "confidence")
# The dynamic-alternatives response top level must be EXACTLY this object.
_ALT_RESPONSE_KEYS = frozenset({"alternatives"})


def _validate_alternative_list(alts: Any, where: str) -> List[Dict[str, Any]]:
    """Strictly validate an alternatives ARRAY (shared by the feasibility and
    dynamic-S5 boundaries).

    Each entry must be an object carrying the prompt-required fields with exact
    types: ``id`` string, ``description`` string, ``target_value`` finite
    number, ``confidence`` in ``[0, 1]``.  No str()/float() coercion.  A value
    that ALSO carries the ambiguous ``value`` alias is rejected.  Optional
    ``actions`` must be an array.  Unknown keys are rejected."""
    if not isinstance(alts, list):
        raise SchemaError(f"field {where!r} must be an array")
    for i, alt in enumerate(alts):
        if not isinstance(alt, dict):
            raise SchemaError(f"{where}[{i}] must be an object")
        reject_unknown_keys(alt, _ALT_KEYS, f"{where}[{i}]")
        require_keys(alt, _ALT_REQUIRED, f"{where}[{i}]")
        _require_str(alt["id"], f"{where}[{i}].id")
        _require_str(alt["description"], f"{where}[{i}].description")
        # ambiguous target_value + value both present is rejected (no guessing)
        if "value" in alt:
            raise SchemaError(
                f"{where}[{i}] carries both target_value and the ambiguous "
                f"'value' alias")
        require_finite_number(alt["target_value"], f"{where}[{i}].target_value")
        require_unit_interval(alt["confidence"], f"{where}[{i}].confidence")
        if "actions" in alt and not isinstance(alt["actions"], list):
            raise SchemaError(f"{where}[{i}].actions must be an array")
    return alts


def validate_alternatives(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Strictly validate a DYNAMIC S5 alternatives-generation RESPONSE object.

    The top level must be EXACTLY the required ``{"alternatives": [...]}``
    object: a missing/null ``alternatives`` field, an unknown top-level key, or
    a non-array ``alternatives`` all reject (coordinator review).  Each entry is
    validated by :func:`_validate_alternative_list`.  Returns the list; raises
    :class:`SchemaError` otherwise."""
    if not isinstance(data, dict):
        raise SchemaError("alternatives response is not a JSON object")
    reject_unknown_keys(data, _ALT_RESPONSE_KEYS, "alternatives response")
    if "alternatives" not in data:
        raise SchemaError("alternatives response missing 'alternatives'")
    alts = data["alternatives"]
    if not isinstance(alts, list):
        raise SchemaError("field 'alternatives' must be an array (not null)")
    return _validate_alternative_list(alts, "alternatives")
