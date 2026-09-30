"""The deliberately small, fail-closed scenario expression language."""
from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any, Mapping

from .contracts import ContractError

EXPRESSION = re.compile(r"\$\{([^{}]+)\}")
WHOLE_EXPRESSION = re.compile(r"^\$\{([^{}]+)\}$")


class ExpressionError(ContractError):
    code = "AIC_RUNNER_UNRESOLVED_EXPRESSION"


class UndefinedOutputError(ExpressionError):
    code = "AIC_RUNNER_UNDEFINED_OUTPUT"


class ForwardOutputError(ExpressionError):
    code = "AIC_RUNNER_FORWARD_OUTPUT_REFERENCE"


def _parts(path: str) -> list[str | int]:
    result: list[str | int] = []
    for part in path.split("."):
        match = re.fullmatch(r"([A-Za-z0-9_-]+)(?:\[([0-9]+)\])?", part)
        if not match:
            raise ExpressionError("invalid expression path: %s" % path)
        result.append(match.group(1))
        if match.group(2) is not None:
            result.append(int(match.group(2)))
    return result


def lookup(expression: str, namespaces: Mapping[str, Any], known_steps: set[str] | None = None) -> Any:
    namespace, dot, remainder = expression.partition(".")
    if not dot or namespace not in {"deployment", "constants", "steps"}:
        raise ExpressionError("unsupported expression: ${%s}" % expression)
    if namespace == "steps":
        step_id, dot, output_path = remainder.partition(".outputs.")
        if not dot:
            raise ExpressionError("invalid step output expression: ${%s}" % expression)
        if known_steps is not None and step_id not in known_steps:
            raise ForwardOutputError("forward step output: %s" % step_id)
        # Runtime results are stored as ``steps[step_id][output]`` while some
        # callers use the contract-shaped ``steps[step_id]["outputs"]`` form.
        # The expression syntax is identical for both; accept either backing
        # representation without making the runner manufacture a fake layer.
        try:
            step_value = namespaces["steps"][step_id]
            value: Any = (step_value["outputs"]
                          if isinstance(step_value, Mapping) and isinstance(step_value.get("outputs"), Mapping)
                          else step_value)
            for part in _parts(output_path):
                value = value[part]
            return deepcopy(value)
        except (KeyError, IndexError, TypeError) as exc:
            raise UndefinedOutputError("undefined step output: ${%s}" % expression) from exc
    value = namespaces
    try:
        for part in _parts(expression):
            value = value[part]
    except (KeyError, IndexError, TypeError) as exc:
        if namespace == "steps":
            raise UndefinedOutputError("undefined step output: ${%s}" % expression) from exc
        raise ExpressionError("unresolved expression: ${%s}" % expression) from exc
    return deepcopy(value)


def resolve(value: Any, namespaces: Mapping[str, Any], known_steps: set[str] | None = None) -> Any:
    if isinstance(value, list):
        return [resolve(item, namespaces, known_steps) for item in value]
    if isinstance(value, dict):
        return {key: resolve(item, namespaces, known_steps) for key, item in value.items()}
    if not isinstance(value, str):
        return deepcopy(value)
    whole = WHOLE_EXPRESSION.fullmatch(value)
    if whole:
        return lookup(whole.group(1), namespaces, known_steps)

    def replacement(match: re.Match[str]) -> str:
        resolved = lookup(match.group(1), namespaces, known_steps)
        if not isinstance(resolved, (str, int, float, bool)) or isinstance(resolved, float) and not resolved == resolved:
            raise ExpressionError("embedded expression must be scalar: ${%s}" % match.group(1))
        return json.dumps(resolved, separators=(",", ":"))[1:-1] if isinstance(resolved, str) else json.dumps(resolved)

    return EXPRESSION.sub(replacement, value)
