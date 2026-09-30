"""Correlate observed exchanges with the frozen catalog steps.

The capture schema requires ``stepId``, ``stepIndex``, ``logicalTimeMs``,
``endpointRef`` and ``declaredExpectedHttpStatus`` for every exchange, so the
upper *must* know which catalog step an exchange belongs to.  That knowledge is
derived here from the catalog and runner-contract bytes only - never from a new
wire field, a header or a guess about the lower runner's behaviour.

Matching is order preserving: the n-th observed exchange that matches a plan
entry's method and resolved-URI shape takes that entry.  An exchange that
matches nothing is still captured, with ``stepId`` ``UNDECLARED_EXCHANGE`` and a
null ``declaredExpectedHttpStatus`` - the upper never suppresses an observation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

BILATERAL_SCENARIOS = ("SC-062", "SC-083", "SC-091", "SC-092")

UNDECLARED_STEP_ID = "UNDECLARED_EXCHANGE"

#: Catalog operations whose step is an HTTP boundary the upper can observe.
_HTTP_OPERATIONS = {
    "HTTP": None,
    "R1_DME_QUERY": "GET",
    "R1_DME_DATA_JOB": "POST",
    "R1_DME_PUBLISH": "POST",
}
#: Non-HTTP operations that still reach an upper listener as a callback.
_CALLBACK_OPERATIONS = {
    "A1_EMIT_STATUS": ("POST", "a1StatusCallbackRoot", "callbackExpectedHttpStatus"),
    "O1_NOTIFY": ("POST", "#/endpointTemplates/o1NotificationRecipient", None),
}


class PlanError(RuntimeError):
    """The catalog could not be reduced to an upper-observable plan."""


@dataclass(frozen=True)
class PlannedExchange:
    step_id: str
    step_index: int
    at_ms: int
    method: str
    endpoint_ref: str | None
    path_pattern: "re.Pattern[str]"
    expected_status: int | None
    bindings: Mapping[str, str]


@dataclass(frozen=True)
class ScenarioPlan:
    scenario_id: str
    catalog_index: int
    time_origin: str
    evaluation_now: str
    initial_state: tuple[str, ...]
    rules: tuple[str, ...]
    exchanges: tuple[PlannedExchange, ...]
    step_times: Mapping[str, int]

    @property
    def catalog_pointer(self) -> str:
        return "/scenarios/%d" % self.catalog_index

    @property
    def expected_pointer(self) -> str:
        return self.catalog_pointer + "/expected"

    @property
    def rules_pointer(self) -> str:
        return self.catalog_pointer + "/rules"


def scenario_entry(catalog: Mapping[str, Any], scenario_id: str
                   ) -> tuple[int, Mapping[str, Any]]:
    for index, scenario in enumerate(catalog.get("scenarios", [])):
        if scenario.get("id") == scenario_id:
            return index, scenario
    raise PlanError("scenario %s is not in the frozen catalog" % scenario_id)


def build_plan(catalog: Mapping[str, Any], scenario_id: str,
               roots: Mapping[str, str],
               fixture: Any = None) -> ScenarioPlan:
    index, scenario = scenario_entry(catalog, scenario_id)
    materialization = scenario["materialization"]
    templates = catalog["endpointTemplates"]
    planned: list[PlannedExchange] = []
    step_times: dict[str, int] = {}
    for step_index, step in enumerate(materialization.get("steps", [])):
        step_id = str(step["id"])
        at_ms = int(step.get("atMs", 0))
        step_times[step_id] = at_ms
        operation = str(step.get("op", ""))
        endpoint_ref = step.get("endpointRef")
        if operation in _HTTP_OPERATIONS and endpoint_ref:
            method = str(step.get("method") or _HTTP_OPERATIONS[operation] or "GET")
            pattern = _pattern_for(templates, str(endpoint_ref), roots)
            planned.append(PlannedExchange(
                step_id=step_id, step_index=step_index, at_ms=at_ms,
                method=method.upper(), endpoint_ref=str(endpoint_ref),
                path_pattern=pattern,
                expected_status=_optional_int(step.get("expectedHttpStatus")),
                bindings=_scalar_bindings(step.get("bindings"))))
            continue
        if operation in _CALLBACK_OPERATIONS:
            method, target, status_key = _CALLBACK_OPERATIONS[operation]
            declared = _optional_int(step.get(status_key)) if status_key else 204
            if target.startswith("#/endpointTemplates/"):
                pattern = _pattern_for(templates, target, roots)
                reference: str | None = target
            else:
                pattern = _pattern_for_root(roots[target])
                reference = None
            planned.append(PlannedExchange(
                step_id=step_id, step_index=step_index, at_ms=at_ms,
                method=method, endpoint_ref=reference, path_pattern=pattern,
                expected_status=declared,
                bindings=_scalar_bindings(step.get("bindings"))))
    time_block = materialization.get("time", {})
    origin = _resolve_instant(time_block, "origin", fixture)
    evaluation = _resolve_instant(time_block, "evaluationNow", fixture) or origin
    if origin is None:
        raise PlanError("scenario %s declares no logical time origin" % scenario_id)
    return ScenarioPlan(
        scenario_id=scenario_id,
        catalog_index=index,
        time_origin=origin,
        evaluation_now=evaluation,
        initial_state=tuple(str(item) for item in materialization.get(
            "initialState", [])),
        rules=tuple(str(item) for item in scenario.get("rules", [])),
        exchanges=tuple(planned),
        step_times=step_times,
    )


class TemplateIndex:
    """Resolve any observed path back to its catalog endpoint template.

    Undeclared exchanges (the upper's own reconciliation GETs, for instance)
    still carry a useful ``endpointRef`` this way, and the mapping comes from
    the catalog's ``endpointTemplates`` bytes rather than from a guess.
    """

    def __init__(self, templates: Mapping[str, str], roots: Mapping[str, str]):
        entries: list[tuple[int, str, "re.Pattern[str]"]] = []
        for name, template in templates.items():
            expanded = template
            for placeholder, value in roots.items():
                expanded = expanded.replace("{%s}" % placeholder, str(value))
            path = urlsplit(expanded).path if "://" in expanded else expanded
            literal = len(re.sub(r"\{[^}]+\}", "", path))
            entries.append((literal, "#/endpointTemplates/" + name,
                            _compile_path(path)))
        self._entries = sorted(entries, key=lambda item: -item[0])

    def resolve(self, path: str) -> str | None:
        for _, reference, pattern in self._entries:
            if pattern.fullmatch(path) is not None:
                return reference
        return None


class PlanMatcher:
    """Order-preserving assignment of observed exchanges to planned steps."""

    def __init__(self, plan: ScenarioPlan) -> None:
        self.plan = plan
        self._remaining = list(plan.exchanges)

    def match(self, method: str, path: str) -> PlannedExchange | None:
        for position, candidate in enumerate(self._remaining):
            if candidate.method != method.upper():
                continue
            if candidate.path_pattern.fullmatch(path) is None:
                continue
            return self._remaining.pop(position)
        return None

    @property
    def unmatched(self) -> tuple[PlannedExchange, ...]:
        return tuple(self._remaining)


def _resolve_instant(time_block: Mapping[str, Any], name: str,
                     fixture: Any) -> str | None:
    """``origin``/``evaluationNow`` may be literal or a bundle fixture ref."""
    literal = time_block.get(name)
    if isinstance(literal, str):
        return literal
    reference = time_block.get(name + "Ref")
    if isinstance(reference, str):
        if fixture is None:
            raise PlanError("resolving %sRef needs the contract bundle" % name)
        resolved = fixture(reference)
        if not isinstance(resolved, str):
            raise PlanError("%sRef did not resolve to an instant" % name)
        return resolved
    return None


def _pattern_for(templates: Mapping[str, str], endpoint_ref: str,
                 roots: Mapping[str, str]) -> "re.Pattern[str]":
    name = endpoint_ref.rsplit("/", 1)[-1]
    try:
        template = templates[name]
    except KeyError as exc:
        raise PlanError("unknown endpoint template: %s" % endpoint_ref) from exc
    expanded = template
    for placeholder, value in roots.items():
        expanded = expanded.replace("{%s}" % placeholder, str(value))
    path = urlsplit(expanded).path if "://" in expanded else expanded
    return _compile_path(path)


def _compile_path(path: str) -> "re.Pattern[str]":
    parts: list[str] = []
    for token in re.split(r"(\{[^}]+\})", path):
        if token.startswith("{") and token.endswith("}"):
            parts.append(r"[^/]+")
        else:
            parts.append(re.escape(token))
    return re.compile("".join(parts).rstrip("/") + "/?")


def _pattern_for_root(uri: str) -> "re.Pattern[str]":
    path = urlsplit(str(uri)).path.rstrip("/") or "/"
    return re.compile(re.escape(path) + "/?")


def _scalar_bindings(bindings: Any) -> dict[str, str]:
    if not isinstance(bindings, Mapping):
        return {}
    return {str(key): str(value) for key, value in bindings.items()
            if isinstance(value, (str, int, float, bool))}


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return int(value)


def declared_rules(catalog: Mapping[str, Any], scenario_ids: Iterable[str]
                   ) -> tuple[str, ...]:
    collected: list[str] = []
    for scenario_id in scenario_ids:
        _, scenario = scenario_entry(catalog, scenario_id)
        for rule in scenario.get("rules", []):
            if rule not in collected:
                collected.append(str(rule))
    return tuple(sorted(collected))


def roots_from_vector(vector: Mapping[str, Any]) -> dict[str, str]:
    """The frozen ``rootMappings`` applied to a validated deployment vector."""
    file_data = vector["o1"]["fileDataReporting"]
    return {
        "r1ApiRoot": vector["r1"]["apiRoot"],
        "a1ApiRoot": vector["a1"]["apiRoot"],
        "a1StatusCallbackRoot": vector["a1"]["statusCallbackRoot"],
        "rAppCallbackRoot": vector["r1"]["callbackApi"]["rootUri"],
        "MnSRoot": file_data["mnsRoot"],
        "MnSVersion": file_data["mnsVersion"],
        "o1ConsumerRoot": file_data["consumerReference"],
        "configured.r1.dme.policyEvidencePushBaseUri":
            vector["r1"]["dme"]["policyEvidencePushBaseUri"],
    }


def assert_root_mappings(runner_contract: Mapping[str, Any],
                         roots: Sequence[str]) -> None:
    declared = runner_contract["expressionLanguage"][
        "endpointTemplateResolution"]["rootMappings"]
    missing = sorted(set(declared) - set(roots))
    if missing:
        raise PlanError("deployment roots are missing %s" % ", ".join(missing))
