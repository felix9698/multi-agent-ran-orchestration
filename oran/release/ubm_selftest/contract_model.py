"""Executable model of the frozen contract bytes.

Everything in this module is derived from exactly two frozen documents plus the
fixture files they reference:

* ``contracts/oran-aic/1.0.1/shared-contract-bundle/scenario-catalog.1.0.1.json``
* ``contracts/oran-aic/1.0.1/shared-contract-bundle/scenario-runner-contract.1.0.1.json``

No route, operation, initial state, transform or predicate is invented here.  If
the bytes do not declare it, the model raises rather than guessing (UBM-ST-C01).

stdlib only: the lower double must not depend on upper runtime modules.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

CATALOG_FILENAME = "scenario-catalog.1.0.1.json"
RUNNER_CONTRACT_FILENAME = "scenario-runner-contract.1.0.1.json"


class ContractFaithfulnessError(RuntimeError):
    """The model was asked for something the frozen bytes do not declare."""


class UnknownRouteError(ContractFaithfulnessError):
    """A request path is not derivable from ``#/endpointTemplates``."""


class UnknownOperationError(ContractFaithfulnessError):
    """A step/initial-state operation is not declared by the runner contract."""


class UnresolvedBindingError(ContractFaithfulnessError):
    """An expression or endpoint placeholder could not be resolved."""


# --------------------------------------------------------------------------
# digests
# --------------------------------------------------------------------------

def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def jcs(value: Any) -> bytes:
    """RFC 8785 (JCS) canonical serialization for the JSON subset in use."""
    return _jcs_render(value).encode("utf-8")


def jcs_sha256(value: Any) -> str:
    return sha256_bytes(jcs(value))


def _jcs_number(value: Any) -> str:
    if isinstance(value, bool):  # pragma: no cover - guarded by caller
        raise TypeError("bool is not a number")
    if isinstance(value, int):
        return str(value)
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError("non-finite numbers are not representable in JCS")
    if value == int(value) and abs(value) < 1e21:
        return str(int(value))
    return repr(value)


def _jcs_render(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return _jcs_number(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_jcs_render(item) for item in value) + "]"
    if isinstance(value, Mapping):
        members = sorted(value.items(), key=lambda kv: kv[0].encode("utf-16-be"))
        return "{" + ",".join(
            json.dumps(key, ensure_ascii=False) + ":" + _jcs_render(item)
            for key, item in members
        ) + "}"
    raise TypeError("value is not JSON: %r" % (value,))


# --------------------------------------------------------------------------
# expression language  (#/expressionLanguage)
# --------------------------------------------------------------------------

_INDEX_RE = re.compile(r"^([A-Za-z0-9_-]+)(?:\[([0-9]+)\])?$")


def _traverse(root: Any, dotted: str, label: str) -> Any:
    current = root
    for segment in dotted.split("."):
        match = _INDEX_RE.match(segment)
        if match is None:
            raise UnresolvedBindingError("bad expression path %r in %s" % (dotted, label))
        name, index = match.group(1), match.group(2)
        if not isinstance(current, Mapping) or name not in current:
            raise UnresolvedBindingError("AIC_RUNNER_UNRESOLVED_EXPRESSION: %s (%s)" % (dotted, label))
        current = current[name]
        if index is not None:
            if not isinstance(current, Sequence) or isinstance(current, (str, bytes)):
                raise UnresolvedBindingError("not an array: %s (%s)" % (dotted, label))
            position = int(index)
            if position >= len(current):
                raise UnresolvedBindingError("index out of range: %s (%s)" % (dotted, label))
            current = current[position]
    return current


@dataclass(frozen=True)
class ExpressionScope:
    """The three namespaces the frozen contract declares, and nothing else."""

    deployment: Mapping[str, Any]
    constants: Mapping[str, Any]
    steps: Mapping[str, Any]

    def lookup(self, expression: str) -> Any:
        body = expression[2:-1]
        namespace, _, rest = body.partition(".")
        if namespace == "deployment":
            return _traverse(self.deployment, rest, expression)
        if namespace == "constants":
            return _traverse(self.constants, rest, expression)
        if namespace == "steps":
            match = _STEP_OUTPUT_RE.fullmatch(expression)
            if match is None:
                raise UnresolvedBindingError("AIC_RUNNER_UNDEFINED_OUTPUT: %s" % expression)
            step_id, output, index = match.group(1), match.group(2), match.group(3)
            if step_id not in self.steps:
                raise UnresolvedBindingError("AIC_RUNNER_FORWARD_OUTPUT_REFERENCE: %s" % expression)
            outputs = self.steps[step_id]
            if output not in outputs:
                raise UnresolvedBindingError("AIC_RUNNER_UNDEFINED_OUTPUT: %s" % expression)
            value = outputs[output]
            if index is not None:
                value = value[int(index)]
            return value
        raise UnresolvedBindingError("undeclared namespace in %s" % expression)


_STATIC_RE = re.compile(r"\$\{(?:deployment|constants)\.[^}]+\}")
_STEP_OUTPUT_RE = re.compile(r"\$\{steps\.([A-Za-z0-9_-]+)\.outputs\.([A-Za-z0-9_-]+)(?:\[([0-9]+)\])?\}")
_ANY_EXPR_RE = re.compile(r"\$\{[^}]+\}")


def _lexical(value: Any, expression: str) -> str:
    if isinstance(value, str):
        return value
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return _jcs_number(value)
    raise UnresolvedBindingError(
        "embedded substitution requires a string, number or boolean: %s" % expression)


def resolve_expressions(value: Any, scope: ExpressionScope) -> Any:
    """Apply ``wholeValueSubstitution`` then ``embeddedSubstitution``."""
    if isinstance(value, Mapping):
        return {key: resolve_expressions(item, scope) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_expressions(item, scope) for item in value]
    if not isinstance(value, str):
        return value
    whole = _ANY_EXPR_RE.fullmatch(value)
    if whole is not None:
        return json.loads(json.dumps(scope.lookup(value)))
    def _replace(match: "re.Match[str]") -> str:
        return _lexical(scope.lookup(match.group(0)), match.group(0))
    return _ANY_EXPR_RE.sub(_replace, value)


def assert_no_unresolved(value: Any, label: str) -> None:
    """``VERIFY_NO_UNRESOLVED_STATIC_EXPRESSION_REMAINS``."""
    rendered = json.dumps(value, ensure_ascii=False)
    if _ANY_EXPR_RE.search(rendered):
        raise UnresolvedBindingError("AIC_RUNNER_UNRESOLVED_EXPRESSION remains in %s" % label)


# --------------------------------------------------------------------------
# RFC 6901 / RFC 6902 (only what the four scenarios declare)
# --------------------------------------------------------------------------

def json_pointer(document: Any, pointer: str) -> Any:
    if pointer in ("", "/"):
        return document
    current = document
    for raw in pointer.lstrip("/").split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                raise UnresolvedBindingError("pointer %s missing at %r" % (pointer, token))
            current = current[token]
        elif isinstance(current, list):
            current = current[int(token)]
        else:
            raise UnresolvedBindingError("pointer %s does not resolve" % pointer)
    return current


def apply_json_patch(document: Any, patch: Sequence[Mapping[str, Any]]) -> Any:
    result = json.loads(json.dumps(document))
    for operation in patch:
        op = operation["op"]
        if op not in {"replace", "add", "remove"}:
            raise ContractFaithfulnessError("unsupported RFC 6902 op: %s" % op)
        pointer = operation["path"]
        tokens = [t.replace("~1", "/").replace("~0", "~") for t in pointer.lstrip("/").split("/")]
        parent = result
        for token in tokens[:-1]:
            parent = parent[int(token)] if isinstance(parent, list) else parent[token]
        last = tokens[-1]
        if op == "remove":
            if isinstance(parent, list):
                del parent[int(last)]
            else:
                parent.pop(last)
        elif isinstance(parent, list):
            index = len(parent) if last == "-" else int(last)
            if op == "add":
                parent.insert(index, operation["value"])
            else:
                parent[index] = operation["value"]
        else:
            if op == "replace" and last not in parent:
                raise ContractFaithfulnessError("replace target missing: %s" % pointer)
            parent[last] = operation["value"]
    return result


# --------------------------------------------------------------------------
# endpoint templates  (#/endpointTemplates + #/expressionLanguage/endpointTemplateResolution)
# --------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")


def expand_endpoint(template: str, *, roots: Mapping[str, Any],
                    bindings: Mapping[str, Any], label: str) -> str:
    """Expand ``{name}`` strictly per ``endpointTemplateResolution``."""
    def _replace(match: "re.Match[str]") -> str:
        name = match.group(1)
        if name in roots:
            return _lexical(roots[name], "{%s}" % name)
        if name not in bindings:
            raise UnresolvedBindingError(
                "endpoint placeholder {%s} has no same-named scalar step binding (%s)"
                % (name, label))
        value = bindings[name]
        if isinstance(value, (Mapping, list)) or value is None:
            raise UnresolvedBindingError(
                "endpoint placeholder {%s} must bind a scalar (%s)" % (name, label))
        return _lexical(value, "{%s}" % name)
    expanded = _PLACEHOLDER_RE.sub(_replace, template)
    if _PLACEHOLDER_RE.search(expanded):
        raise UnresolvedBindingError("unexpanded endpoint placeholder in %s" % label)
    return expanded


@dataclass(frozen=True)
class RouteMatch:
    template_name: str
    template: str
    bindings: Mapping[str, str]


class RouteTable:
    """Path recogniser built from ``#/endpointTemplates`` for one root name."""

    def __init__(self, templates: Mapping[str, str], root_name: str, root_uri: str):
        self.root_name = root_name
        self.root_uri = root_uri.rstrip("/")
        root_path = urlsplit(self.root_uri).path.rstrip("/")
        self._routes: list[tuple[str, str, re.Pattern[str], tuple[str, ...]]] = []
        prefix = "{%s}" % root_name
        for name, template in templates.items():
            if not template.startswith(prefix):
                continue
            tail = template[len(prefix):]
            names: list[str] = []
            pattern = ["^", re.escape(root_path)]
            index = 0
            for match in _PLACEHOLDER_RE.finditer(tail):
                pattern.append(re.escape(tail[index:match.start()]))
                names.append(match.group(1))
                pattern.append(r"([^/]+)")
                index = match.end()
            pattern.append(re.escape(tail[index:]))
            pattern.append("$")
            self._routes.append((name, template, re.compile("".join(pattern)), tuple(names)))
        # Longest literal prefix first so ``/policies/{id}/status`` wins over
        # ``/policies/{id}``.
        self._routes.sort(key=lambda item: -len(item[1]))

    def match(self, path: str) -> RouteMatch:
        for name, template, pattern, names in self._routes:
            found = pattern.match(path)
            if found is not None:
                return RouteMatch(name, template, dict(zip(names, found.groups())))
        raise UnknownRouteError("path %r is not derivable from #/endpointTemplates" % path)

    def template_names(self) -> tuple[str, ...]:
        return tuple(name for name, _, _, _ in self._routes)


# --------------------------------------------------------------------------
# scenarios
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ScenarioModel:
    scenario_id: str
    catalog_index: int
    suite: str
    fixture_mode: str
    initial_state: tuple[str, ...]
    steps: tuple[Mapping[str, Any], ...]
    faults: tuple[Any, ...]
    expected: Mapping[str, Any]
    rules: tuple[str, ...]
    time: Mapping[str, Any]

    @property
    def catalog_pointer(self) -> str:
        return "/scenarios/%d" % self.catalog_index

    @property
    def expected_pointer(self) -> str:
        return "/scenarios/%d/expected" % self.catalog_index

    @property
    def rules_pointer(self) -> str:
        return "/scenarios/%d/rules" % self.catalog_index


class FrozenContract:
    """Read-only view over the frozen catalog and runner contract bytes."""

    def __init__(self, bundle_dir: Path):
        self.bundle_dir = Path(bundle_dir)
        self.catalog_path = self.bundle_dir / CATALOG_FILENAME
        self.runner_contract_path = self.bundle_dir / RUNNER_CONTRACT_FILENAME
        self.catalog_sha256 = sha256_file(self.catalog_path)
        self.runner_contract_sha256 = sha256_file(self.runner_contract_path)
        self.catalog: Mapping[str, Any] = json.loads(self.catalog_path.read_text("utf-8"))
        self.runner: Mapping[str, Any] = json.loads(self.runner_contract_path.read_text("utf-8"))
        self._scenario_index = {
            entry.get("id") or entry.get("scenarioId"): position
            for position, entry in enumerate(self.catalog["scenarios"])
        }
        self._fixture_cache: dict[str, Any] = {}

    # -- declarations -------------------------------------------------
    @property
    def constants(self) -> Mapping[str, Any]:
        return self.catalog["constants"]

    @property
    def endpoint_templates(self) -> Mapping[str, str]:
        return self.catalog["endpointTemplates"]

    @property
    def root_mappings(self) -> Mapping[str, str]:
        return self.runner["expressionLanguage"]["endpointTemplateResolution"]["rootMappings"]

    @property
    def operations(self) -> Mapping[str, Any]:
        return self.runner["operations"]

    @property
    def initial_states(self) -> Mapping[str, Any]:
        return self.runner["initialStates"]

    def operation(self, name: str) -> Mapping[str, Any]:
        if name not in self.operations:
            raise UnknownOperationError("operation %r is not declared by the runner contract" % name)
        return self.operations[name]

    def adapter_action(self, initial_state_token: str) -> str:
        if initial_state_token not in self.initial_states:
            raise UnknownOperationError(
                "initial state %r is not declared by the runner contract" % initial_state_token)
        return self.initial_states[initial_state_token]["adapterAction"]

    def endpoint_template(self, ref: str) -> str:
        if not ref.startswith("#/endpointTemplates/"):
            raise UnknownRouteError("endpointRef %r is not a catalog endpoint template" % ref)
        name = ref.rsplit("/", 1)[-1]
        if name not in self.endpoint_templates:
            raise UnknownRouteError("endpoint template %r is not declared" % name)
        return self.endpoint_templates[name]

    # -- resolution ---------------------------------------------------
    def resolve_roots(self, deployment: Mapping[str, Any]) -> dict[str, Any]:
        scope = ExpressionScope(deployment=deployment, constants=self.constants, steps={})
        return {name: scope.lookup(expression) for name, expression in self.root_mappings.items()}

    def fixture(self, reference: str) -> Any:
        """Resolve ``fixture://name`` or ``fixture://name#/json/pointer``."""
        if not reference.startswith("fixture://"):
            raise ContractFaithfulnessError("not a fixture reference: %r" % reference)
        body = reference[len("fixture://"):]
        name, _, pointer = body.partition("#")
        registry = self.catalog["fixtureRegistry"]
        if name not in registry:
            raise ContractFaithfulnessError("fixture %r is not in #/fixtureRegistry" % name)
        target = registry[name]
        relative, _, registry_pointer = target.partition("#")
        document = self._load_fixture_file(relative)
        if registry_pointer:
            document = json_pointer(document, registry_pointer)
        if pointer:
            document = json_pointer(document, pointer)
        return json.loads(json.dumps(document)) if isinstance(document, (dict, list)) else document

    def fixture_bytes(self, reference: str) -> bytes:
        body = reference[len("fixture://"):]
        name, _, pointer = body.partition("#")
        if pointer:
            raise ContractFaithfulnessError("raw fixture bytes cannot carry a pointer: %r" % reference)
        relative = self.catalog["fixtureRegistry"][name].partition("#")[0]
        return (self.bundle_dir / relative).read_bytes()

    def _load_fixture_file(self, relative: str) -> Any:
        if relative not in self._fixture_cache:
            path = self.bundle_dir / relative
            raw = path.read_bytes()
            if path.suffix == ".json":
                self._fixture_cache[relative] = json.loads(raw.decode("utf-8"))
            else:
                self._fixture_cache[relative] = raw.decode("utf-8")
        return self._fixture_cache[relative]

    def resolve_fixture_refs(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            if set(value) == {"$fixtureRef"}:
                return self.fixture(value["$fixtureRef"])
            return {key: self.resolve_fixture_refs(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.resolve_fixture_refs(item) for item in value]
        return value

    # -- scenarios ----------------------------------------------------
    def scenario(self, scenario_id: str) -> ScenarioModel:
        if scenario_id not in self._scenario_index:
            raise ContractFaithfulnessError("scenario %r is not in the frozen catalog" % scenario_id)
        index = self._scenario_index[scenario_id]
        entry = self.catalog["scenarios"][index]
        materialization = entry["materialization"]
        ordered = sorted(
            enumerate(materialization["steps"]),
            key=lambda item: (item[1]["atMs"], item[0]),
        )
        for _, step in ordered:
            self.operation(step["op"])
        for token in materialization["initialState"]:
            self.adapter_action(token)
        return ScenarioModel(
            scenario_id=scenario_id,
            catalog_index=index,
            suite=entry["suite"],
            fixture_mode=entry["fixtureMode"],
            initial_state=tuple(materialization["initialState"]),
            steps=tuple(step for _, step in ordered),
            faults=tuple(materialization.get("faults", ())),
            expected=entry["expected"],
            rules=tuple(entry.get("rules", ())),
            time=materialization["time"],
        )

    def profile_scenarios(self, assignment_path: Path, profile: str) -> tuple[str, ...]:
        assignment = json.loads(Path(assignment_path).read_text("utf-8"))
        selected = [
            entry["scenarioId"]
            for entry in assignment["assignments"]
            if entry.get("profile") == profile
        ]
        if not selected:
            raise ContractFaithfulnessError("profile %r assigns no scenario" % profile)
        return tuple(sorted(set(selected)))


def scenario_origin(scenario: ScenarioModel, contract: FrozenContract) -> str:
    """``materialization.time.origin`` or its declared fixture reference."""
    time = scenario.time
    if "origin" in time:
        return time["origin"]
    if "originRef" in time:
        return contract.fixture(time["originRef"])
    raise ContractFaithfulnessError("scenario %s declares no time origin" % scenario.scenario_id)


def declared_expected_status(step: Mapping[str, Any]) -> int | None:
    for key in ("expectedHttpStatus", "callbackExpectedHttpStatus"):
        if key in step:
            return step[key]
    return None
