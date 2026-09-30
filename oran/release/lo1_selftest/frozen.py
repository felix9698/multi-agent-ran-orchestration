"""Frozen-byte reader.  Every value the self-test uses is READ, never typed.

Two properties matter more than anything else in this module:

1. **No oracle literal.**  ``/scenarios/83/expected``, ``/scenarios/83/rules``
   and ``/assignments/65`` are reached through pointers that are themselves
   read from the frozen assignment, so mutating a scratch copy of the bundle
   changes what the self-test plans and reports (``LO1-ST-O01``).  Nothing in
   this file writes an expected scalar down.
2. **A bundle path is a parameter.**  Every reader takes the bundle root, so a
   drift check can point it at a scratch copy in a temporary directory and the
   repository bundle is never written (``G-CONTRACT-1``).

The scenario is located by asking the execution-profile assignment which
catalog pointer carries it.  A mutated pointer, a mutated ``id`` or a mutated
``executionProfile`` therefore changes behaviour instead of being absorbed.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from xml.etree import ElementTree

#: The one scenario this release is scoped to.  This is an IDENTITY, not an
#: oracle value: the expectations attached to it are always read from the
#: frozen catalog and never restated here.
SCENARIO_ID = "SC-084"

BUNDLE_RELATIVE = Path("contracts") / "oran-aic" / "1.0.1" / "shared-contract-bundle"

CATALOG = "scenario-catalog.1.0.1.json"
RUNNER_CONTRACT = "scenario-runner-contract.1.0.1.json"
ASSIGNMENT = "execution-profile-assignment.1.0.1.json"
VECTOR_SCHEMA = "deployment-test-vector.1.0.0.schema.json"
NETCONF_PROFILE = "o1-netconf-yang-profile.1.0.0.json"
PA_FILE_PROFILE = "oran-aic-o1-pa-file.1.0.0.json"
BUNDLE_MANIFEST = "bundle-manifest.1.0.1.json"
POLICY_EVIDENCE_SCHEMA = "aic.policy-evidence.1.0.0.schema.json"

GOLDEN_O1 = "golden/o1"
GOLDEN_NETCONF = "golden/o1/netconf"

#: Exactly the inputs ``emulator-boundary.1.0.0.json#/emulatorConstruction/permittedInputs``
#: admits.  Anything else is a prohibited input and the emulator refuses it.
PERMITTED_EMULATOR_INPUTS: tuple[str, ...] = (
    NETCONF_PROFILE,
    PA_FILE_PROFILE,
    f"{GOLDEN_O1}/notify-file-ready.json",
    f"{GOLDEN_O1}/valid-prb.xml",
    f"{GOLDEN_O1}/null-prb.xml",
    f"{GOLDEN_O1}/suspect-prb.xml",
    f"{GOLDEN_O1}/overlap-context.json",
)


class FrozenByteError(RuntimeError):
    """A frozen input is absent, unreadable or does not resolve as declared."""


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def json_pointer(document: Any, pointer: str) -> Any:
    """RFC 6901 resolution.  A pointer that does not resolve is an error."""
    if pointer in ("", "/"):
        return document
    if not pointer.startswith("/"):
        raise FrozenByteError(f"not a JSON pointer: {pointer!r}")
    current = document
    for token in pointer.split("/")[1:]:
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                raise FrozenByteError(f"pointer {pointer!r} does not resolve at {token!r}")
            current = current[token]
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes)):
            if not token.lstrip("-").isdigit():
                raise FrozenByteError(f"pointer {pointer!r} indexes an array with {token!r}")
            index = int(token)
            if index < 0 or index >= len(current):
                raise FrozenByteError(f"pointer {pointer!r} is out of range at {token!r}")
            current = current[index]
        else:
            raise FrozenByteError(f"pointer {pointer!r} descends into a scalar at {token!r}")
    return current


@dataclass(frozen=True)
class GoldenFixture:
    """One registered golden artefact, kept as raw bytes plus its digest."""

    relative_path: str
    raw: bytes
    sha256: str

    @property
    def name(self) -> str:
        return self.relative_path.rsplit("/", 1)[-1]


class FrozenBundle:
    """Reader over one copy of the frozen shared contract bundle.

    ``root`` may be the repository copy or a scratch copy in a temporary
    directory; the class never writes.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        if not (self.root / CATALOG).is_file():
            raise FrozenByteError(f"no frozen bundle at {self.root}")
        self._cache: dict[str, Any] = {}
        self._raw: dict[str, bytes] = {}

    # ------------------------------------------------------------------ bytes

    def raw(self, name: str) -> bytes:
        if name not in self._raw:
            path = self.root / name
            if not path.is_file():
                raise FrozenByteError(f"frozen input missing: {name}")
            self._raw[name] = path.read_bytes()
        return self._raw[name]

    def digest(self, name: str) -> str:
        return sha256_bytes(self.raw(name))

    def document(self, name: str) -> Any:
        if name not in self._cache:
            self._cache[name] = json.loads(self.raw(name).decode("utf-8"))
        return self._cache[name]

    # -------------------------------------------------------------- documents

    @property
    def catalog(self) -> Mapping[str, Any]:
        return self.document(CATALOG)

    @property
    def runner_contract(self) -> Mapping[str, Any]:
        return self.document(RUNNER_CONTRACT)

    @property
    def assignment(self) -> Mapping[str, Any]:
        return self.document(ASSIGNMENT)

    @property
    def vector_schema(self) -> Mapping[str, Any]:
        return self.document(VECTOR_SCHEMA)

    @property
    def netconf_profile(self) -> Mapping[str, Any]:
        return self.document(NETCONF_PROFILE)

    @property
    def pa_file_profile(self) -> Mapping[str, Any]:
        return self.document(PA_FILE_PROFILE)

    # ------------------------------------------------------- scenario lookup

    def assignment_pointer(self, scenario_id: str = SCENARIO_ID) -> str:
        entries = self.assignment.get("assignments")
        if not isinstance(entries, list):
            raise FrozenByteError("assignment document has no /assignments array")
        for index, entry in enumerate(entries):
            if isinstance(entry, Mapping) and entry.get("scenarioId") == scenario_id:
                return f"/assignments/{index}"
        raise FrozenByteError(f"no assignment for {scenario_id}")

    def scenario_assignment(self, scenario_id: str = SCENARIO_ID) -> Mapping[str, Any]:
        return json_pointer(self.assignment, self.assignment_pointer(scenario_id))

    def catalog_pointer(self, scenario_id: str = SCENARIO_ID) -> str:
        pointer = self.scenario_assignment(scenario_id).get("catalogPointer")
        if not isinstance(pointer, str) or not pointer.startswith("/scenarios/"):
            raise FrozenByteError(f"assignment for {scenario_id} carries no catalogPointer")
        return pointer

    def scenario(self, scenario_id: str = SCENARIO_ID) -> Mapping[str, Any]:
        pointer = self.catalog_pointer(scenario_id)
        found = json_pointer(self.catalog, pointer)
        if not isinstance(found, Mapping) or found.get("id") != scenario_id:
            raise FrozenByteError(
                f"catalogPointer {pointer} does not resolve to {scenario_id}")
        return found

    def expected_pointer(self, scenario_id: str = SCENARIO_ID) -> str:
        return f"{self.catalog_pointer(scenario_id)}/expected"

    def rules_pointer(self, scenario_id: str = SCENARIO_ID) -> str:
        return f"{self.catalog_pointer(scenario_id)}/rules"

    def expected(self, scenario_id: str = SCENARIO_ID) -> Mapping[str, Any]:
        """The oracle object, READ.  Callers measure against it; nothing copies it."""
        return json_pointer(self.catalog, self.expected_pointer(scenario_id))

    def declared_rules(self, scenario_id: str = SCENARIO_ID) -> tuple[str, ...]:
        return tuple(json_pointer(self.catalog, self.rules_pointer(scenario_id)))

    def rule_semantics(self, rule_id: str) -> Mapping[str, Any]:
        rules = self.catalog.get("assertionRules")
        if not isinstance(rules, Mapping) or rule_id not in rules:
            raise FrozenByteError(f"assertionRules has no {rule_id}")
        return rules[rule_id]

    def steps(self, scenario_id: str = SCENARIO_ID) -> tuple[Mapping[str, Any], ...]:
        return tuple(self.scenario(scenario_id)["materialization"]["steps"])

    def initial_states(self, scenario_id: str = SCENARIO_ID) -> tuple[str, ...]:
        return tuple(self.scenario(scenario_id)["materialization"]["initialState"])

    def step(self, step_id: str, scenario_id: str = SCENARIO_ID) -> Mapping[str, Any]:
        for entry in self.steps(scenario_id):
            if entry.get("id") == step_id:
                return entry
        raise FrozenByteError(f"{scenario_id} declares no step {step_id!r}")

    def live_capture_lower_bound_ms(self, scenario_id: str = SCENARIO_ID) -> int:
        """``collectionDurationMs + afterActionDelayMs``, read from the catalog.

        The release stores neither number; ``G-ID-08`` computes the bound and
        refuses a shorter ``timeouts.liveCaptureMs``.
        """
        notify = self.step("o1-notify", scenario_id)
        return int(notify.get("collectionDurationMs", 0)) + int(notify.get("afterActionDelayMs", 0))

    def profile(self, name: str) -> Mapping[str, Any]:
        profiles = self.assignment.get("profiles")
        if not isinstance(profiles, Mapping) or name not in profiles:
            raise FrozenByteError(f"assignment declares no profile {name!r}")
        return profiles[name]

    def execution_profile(self, scenario_id: str = SCENARIO_ID) -> str:
        return str(self.scenario_assignment(scenario_id)["executionProfile"])

    # --------------------------------------------------------------- fixtures

    def golden_netconf_fixtures(self) -> tuple[GoldenFixture, ...]:
        directory = self.root / GOLDEN_NETCONF
        if not directory.is_dir():
            raise FrozenByteError("golden/o1/netconf is absent from the bundle")
        found = []
        for path in sorted(directory.glob("*.xml")):
            raw = path.read_bytes()
            found.append(GoldenFixture(
                relative_path=f"{GOLDEN_NETCONF}/{path.name}",
                raw=raw,
                sha256=sha256_bytes(raw),
            ))
        if not found:
            raise FrozenByteError("golden/o1/netconf carries no fixture")
        return tuple(found)

    def golden_pm_documents(self) -> tuple[GoldenFixture, ...]:
        names = ("valid-prb.xml", "null-prb.xml", "suspect-prb.xml")
        found = []
        for name in names:
            relative = f"{GOLDEN_O1}/{name}"
            raw = self.raw(relative)
            found.append(GoldenFixture(relative, raw, sha256_bytes(raw)))
        return tuple(found)

    def golden_notification(self) -> Mapping[str, Any]:
        return self.document(f"{GOLDEN_O1}/notify-file-ready.json")

    def golden_overlap_context(self) -> Mapping[str, Any]:
        return self.document(f"{GOLDEN_O1}/overlap-context.json")

    # ------------------------------------------------------- derived readings

    def pm_namespace(self) -> str:
        return str(self.pa_file_profile["delivery"]["xmlNamespace"])

    def pm_root_element(self) -> str:
        return str(self.pa_file_profile["delivery"]["xmlRoot"])

    def pm_stylesheet_pi(self) -> str:
        return str(self.pa_file_profile["delivery"]["xmlStylesheetProcessingInstruction"])

    def pm_file_format(self) -> str:
        return str(self.pa_file_profile["delivery"]["fileFormat"])

    def measurement_profile(self, name: str) -> Mapping[str, Any]:
        for entry in self.pa_file_profile["measurements"]:
            if entry.get("name") == name:
                return entry
        raise FrozenByteError(f"the PM file profile declares no measurement {name!r}")

    def measurement_names(self) -> tuple[str, ...]:
        return tuple(str(entry["name"]) for entry in self.pa_file_profile["measurements"])

    def required_netconf_capabilities(self) -> tuple[str, ...]:
        return tuple(self.netconf_profile["transport"]["requiredCapabilities"])

    def netconf_lifecycle(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(self.netconf_profile["lifecycle"])

    def netconf_teardown(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(self.netconf_profile["teardown"])

    def golden_sample_values(self) -> frozenset[str]:
        """Every ``<r>`` text present in any golden PM document.

        ``RULE-O1-LIVE-VALUE-INVARIANTS`` forbids a live measurement value from
        being equal to any of them, so the emulator generates around this set
        rather than replaying it.
        """
        values: set[str] = set()
        for fixture in self.golden_pm_documents():
            root = ElementTree.fromstring(fixture.raw.decode("utf-8"))
            for element in root.iter():
                if element.tag.rsplit("}", 1)[-1] == "r" and element.text is not None:
                    values.add(element.text.strip())
        return frozenset(values)

    def golden_window_instants(self) -> frozenset[str]:
        """Every timestamp attribute value present in the golden PM documents."""
        instants: set[str] = set()
        pattern = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
        for fixture in self.golden_pm_documents():
            instants.update(pattern.findall(fixture.raw.decode("utf-8")))
        notification = json.dumps(self.golden_notification())
        instants.update(pattern.findall(notification))
        return frozenset(instants)

    # ----------------------------------------------------------- self-checks

    def permitted_input_digests(self) -> dict[str, str]:
        """Digest of every input the emulator boundary permits, plus fixtures."""
        digests = {name: self.digest(name) for name in PERMITTED_EMULATOR_INPUTS}
        for fixture in self.golden_netconf_fixtures():
            digests[fixture.relative_path] = fixture.sha256
        return digests

    def contract_digests(self) -> dict[str, str]:
        return {
            "bundleManifestSha256": self.digest(BUNDLE_MANIFEST),
            "catalogSha256": self.digest(CATALOG),
            "runnerContractSha256": self.digest(RUNNER_CONTRACT),
            "profileAssignmentSha256": self.digest(ASSIGNMENT),
            "deploymentVectorSchemaSha256": self.digest(VECTOR_SCHEMA),
            "o1NetconfYangProfileSha256": self.digest(NETCONF_PROFILE),
            "o1PaFileProfileSha256": self.digest(PA_FILE_PROFILE),
            "policyEvidenceSchemaSha256": self.digest(POLICY_EVIDENCE_SCHEMA),
        }


def repository_bundle(repo_root: Path) -> FrozenBundle:
    return FrozenBundle(Path(repo_root) / BUNDLE_RELATIVE)


def iter_bundle_files(root: Path) -> Iterable[Path]:
    for path in sorted(Path(root).rglob("*")):
        if path.is_file():
            yield path
