"""Reduce the frozen SC-084 catalog bytes to an upper-observable plan.

The capture schema requires ``stepId``, ``stepIndex``, ``endpointRef`` and
``declaredExpectedHttpStatus`` on every exchange, so the upper *must* know which
catalog step an exchange belongs to.  That knowledge is derived here from the
catalog and runner-contract bytes only.

Two properties matter more here than in the bilateral release:

* **The oracle is READ, never hardcoded** (G-ORACLE-2 / LO1-ST-O01).  Step ids,
  expected HTTP statuses, the declared rule set and the declared shape counts all
  come from the catalog at run time, so mutating a scratch copy of
  ``/scenarios/83`` changes what this plan contains.  :func:`oracle_reference`
  additionally digests ``/scenarios/83/expected`` so the runtime can *notice* a
  mutation without ever copying a value into the capture document.
* **``LIVE_OBSERVED`` ordering.**  ``atMs`` is an earliest-start offset and
  ``atMs: null`` means "immediately after the previous step's outputs are
  captured", so the plan carries the declared offset but never a schedule.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from oran.contract.jcs import jcs_sha256

LIVE_O1_SCENARIO = "SC-084"
CATALOG_POINTER = "/scenarios/83"
ASSIGNMENT_POINTER = "/assignments/65"

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
    at_ms: int | None
    method: str
    endpoint_ref: str | None
    path_pattern: "re.Pattern[str]"
    expected_status: int | None
    bindings: Mapping[str, str]


@dataclass(frozen=True)
class ScenarioPlan:
    scenario_id: str
    catalog_index: int
    fixture_mode: str
    time_mode: str
    initial_state: tuple[str, ...]
    rules: tuple[str, ...]
    step_ids: tuple[str, ...]
    step_ops: tuple[str, ...]
    exchanges: tuple[PlannedExchange, ...]
    declared_fault_count: int

    @property
    def catalog_pointer(self) -> str:
        return "/scenarios/%d" % self.catalog_index

    @property
    def expected_pointer(self) -> str:
        return self.catalog_pointer + "/expected"

    @property
    def rules_pointer(self) -> str:
        return self.catalog_pointer + "/rules"

    def scenario_shape(self) -> dict[str, Any]:
        """The capture's ``/scenario`` member: identity and MEASURED shape only."""
        return {
            "scenarioId": self.scenario_id,
            "catalogPointer": self.catalog_pointer,
            "expectedPointer": self.expected_pointer,
            "rulesPointer": self.rules_pointer,
            "fixtureMode": self.fixture_mode,
            "executionProfile": "live-O1",
            "counterpartProvisioning": "LIVE_O1_AUTHORITY_APPROVED_HARNESS",
            "declaredFaultCount": self.declared_fault_count,
            "declaredStepCount": len(self.step_ids),
            "declaredRuleCount": len(self.rules),
        }


def scenario_entry(catalog: Mapping[str, Any], scenario_id: str
                   ) -> tuple[int, Mapping[str, Any]]:
    for index, scenario in enumerate(catalog.get("scenarios", [])):
        if scenario.get("id") == scenario_id:
            return index, scenario
    raise PlanError("scenario %s is not in the frozen catalog" % scenario_id)


def build_plan(catalog: Mapping[str, Any], scenario_id: str,
               roots: Mapping[str, str]) -> ScenarioPlan:
    index, scenario = scenario_entry(catalog, scenario_id)
    materialization = scenario["materialization"]
    templates = catalog["endpointTemplates"]
    planned: list[PlannedExchange] = []
    step_ids: list[str] = []
    step_ops: list[str] = []
    for step_index, step in enumerate(materialization.get("steps", [])):
        step_id = str(step["id"])
        operation = str(step.get("op", ""))
        step_ids.append(step_id)
        step_ops.append(operation)
        at_ms = step.get("atMs")
        at_ms = int(at_ms) if isinstance(at_ms, int) and not isinstance(at_ms, bool) \
            else None
        endpoint_ref = step.get("endpointRef")
        if operation in _HTTP_OPERATIONS and endpoint_ref:
            method = str(step.get("method") or _HTTP_OPERATIONS[operation] or "GET")
            planned.append(PlannedExchange(
                step_id=step_id, step_index=step_index, at_ms=at_ms,
                method=method.upper(), endpoint_ref=str(endpoint_ref),
                path_pattern=_pattern_for(templates, str(endpoint_ref), roots),
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
    return ScenarioPlan(
        scenario_id=scenario_id,
        catalog_index=index,
        fixture_mode=str(scenario.get("fixtureMode", "")),
        time_mode=str(time_block.get("mode", "")),
        initial_state=tuple(str(item) for item in materialization.get(
            "initialState", [])),
        rules=tuple(str(item) for item in scenario.get("rules", [])),
        step_ids=tuple(step_ids),
        step_ops=tuple(step_ops),
        exchanges=tuple(planned),
        declared_fault_count=len(scenario.get("faults") or []),
    )


def oracle_reference(catalog: Mapping[str, Any], scenario_id: str) -> dict[str, Any]:
    """Pointers plus a DIGEST of the oracle -- never a copy of its values.

    ``expectedJcsSha256`` is what lets the runtime notice that a scratch copy of
    the bundle was mutated (LO1-ST-O01) without a single expected scalar ever
    entering this release's own bytes or its capture document.
    """
    index, scenario = scenario_entry(catalog, scenario_id)
    expected = scenario.get("expected")
    if not isinstance(expected, Mapping):
        raise PlanError("scenario %s declares no expected object" % scenario_id)
    return {
        "origin": "SCENARIO_CATALOG_1_0_1",
        "catalogPointer": "/scenarios/%d" % index,
        "expectedPointer": "/scenarios/%d/expected" % index,
        "rulesPointer": "/scenarios/%d/rules" % index,
        "assignmentPointer": ASSIGNMENT_POINTER,
        "expectedJcsSha256": jcs_sha256(dict(expected)),
        "expectedMemberCount": len(expected),
        "declaredHttpObservationCount": len(list(expected.get("httpSequence") or [])),
    }


def assert_plan_matches_oracle_shape(plan: ScenarioPlan,
                                     oracle: Mapping[str, Any]) -> None:
    """The planned observation count must equal the oracle's declared shape.

    This is the load-bearing consumer of the oracle READ: a mutated
    ``httpSequence`` changes ``declaredHttpObservationCount`` and this assertion
    fails closed, which is exactly what LO1-ST-O01 demands of a harness that is
    not carrying a second source of truth.
    """
    declared = int(oracle["declaredHttpObservationCount"])
    if declared != len(plan.exchanges):
        raise PlanError(
            "the frozen oracle declares %d HTTP observations but the catalog "
            "steps reduce to %d upper-observable exchanges"
            % (declared, len(plan.exchanges)))


class TemplateIndex:
    """Resolve any observed path back to its catalog endpoint template."""

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
        self._lock = threading.RLock()

    def match(self, method: str, path: str) -> PlannedExchange | None:
        with self._lock:
            for position, candidate in enumerate(self._remaining):
                if candidate.method != method.upper():
                    continue
                if candidate.path_pattern.fullmatch(path) is None:
                    continue
                return self._remaining.pop(position)
        return None

    @property
    def unmatched(self) -> tuple[PlannedExchange, ...]:
        with self._lock:
            return tuple(self._remaining)


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


def declared_rules(catalog: Mapping[str, Any], scenario_ids: Iterable[str]
                   ) -> tuple[str, ...]:
    collected: list[str] = []
    for scenario_id in scenario_ids:
        _, scenario = scenario_entry(catalog, scenario_id)
        for rule in scenario.get("rules", []):
            if rule not in collected:
                collected.append(str(rule))
    return tuple(collected)


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
