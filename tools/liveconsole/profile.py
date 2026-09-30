"""Resolve one live deployment from a single authority: the live profile JSON.

Sol's F-5 is the reason this module exists.  The Gate 3 runner reaches the
laboratory through four independent CLI defaults, and three of them are
absolute paths into one host's 2026-08-19 deployment
(``tools/g3ota/run_ota.py`` ``DEFAULT_VALUES`` / ``DEFAULT_CAPABILITY`` /
``DEFAULT_PRODUCER_DB``).  Four defaults are four ways for a run to address a
deployment nobody named, so the live console takes **one** input -- the profile
the operator passes to ``--profile`` -- and resolves everything else from it:

===========================  ====================================================
binding                      ``liveConsole.assuranceBindingPath``
integration values           ``integrationValuesPath`` (the GUI profile's own key)
capability manifest          ``capabilityManifestPath``, pinned by the values'
                             ``backend.capabilityManifestSha256``
A1-P producer sqlite         ``liveConsole.producerDatabasePath``
KPM indication JSONL         the binding's ``kpm.jsonlPath``
R1 consumer state directory  ``liveConsole.r1StateDir``, else ``<runsRoot>/liveconsole/r1-state``
evidence directory           ``liveConsole.evidenceDir``, else ``<runsRoot>/liveconsole/evidence``
the UE this profile addresses ``liveConsole.amfUeNgapId`` (optional)
the controlled non-target UE ``liveConsole.controlledUe.amfUeNgapId`` (optional)
second A1-P endpoint         ``liveConsole.actionProducer`` (optional)
===========================  ====================================================

Relative paths resolve against **the profile document's own directory**, one
rule with no fallback, so a profile can be read without knowing where the
process was started.

Two documents, one file
-----------------------
``gui/operator/session/profile.py::ExperimentProfile`` owns the GUI half and is
unchanged: it reads the keys it already knows and ignores the rest, and its
refusal of literal secret material still runs over the whole document.  The
``liveConsole`` block is the composition root's half of the same file, read
here.  Keeping it in one file is the point -- an operator names one thing.

Every digest is checked and a moved one is refused **by name**:
:func:`~assurance.contracts.live_binding.load_assurance_live_binding` verifies
each identity source it lists, and the capability manifest is verified against
the digest the integration values pin.  A run that addressed a deployment whose
identity had moved would be evidence about something else.

The two documents are then **cross-bound** (:func:`_cross_bind`).  Resolving
them separately is what made the previous version unsafe: the R1 client comes
from the integration values and the frozen ``DeploymentBinding`` the Kernel
records comes from the binding, so two individually valid documents could send
the policy to one Near-RT RIC and attribute it to another.  The values this run
resolved must therefore be a document the binding was cut from -- by path and
by digest -- and the endpoints, cells and E2 nodes they state must agree.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urlsplit

from assurance.contracts.live_binding import (
    AssuranceLiveBinding,
    LiveBindingError,
    load_assurance_live_binding,
)

from gui.operator.session.profile import ExperimentProfile, ProfileError

# The strict loader itself, not the preserved headless coordinator's wrapper
# around it: ``oran.rapp.headless`` is on the default entry point's forbidden
# list and this root has no need of anything else in it.
from oran.contract.integration_values import (
    IntegrationValuesError,
    load_integration_values,
)

__all__ = [
    "ACTION_PRODUCER_KEY",
    "LIVE_CONSOLE_KEY",
    "ActionProducer",
    "ActionProducerType",
    "LiveConsoleError",
    "LiveDeployment",
    "load_live_deployment",
]

#: The object in the profile document that carries the composition root's half.
LIVE_CONSOLE_KEY = "liveConsole"

#: The optional second A1-P endpoint inside that block.  The stage producer
#: carries the PRIMARY steering type; this one carries the SUPPLEMENTARY
#: Style-2 action types.  Two endpoints, two fixed adapters, and the mapping
#: between them stated once here -- never chosen at runtime and never taken
#: from proposal text.
ACTION_PRODUCER_KEY = "actionProducer"

#: Keys with no honest default.  A missing one is refused by name rather than
#: guessed from a directory layout: a producer database resolved by convention
#: that happened not to exist would read as "this UE's A1 scope is empty" and
#: the next submission would be fenced HTTP 409 for a reason nothing recorded.
REQUIRED_LIVE_KEYS = ("assuranceBindingPath", "producerDatabasePath")

#: Endpoints both documents state, as ``(integration-values key, what the
#: binding calls it)``.  Compared by authority -- scheme, host and port -- not
#: by full URL: the binding carries the A1-P interface path (``/A1-P/v2``) and
#: the values carry the service root, which are two correct spellings of one
#: endpoint.  The authority is the part that decides *where the write goes*.
CROSS_BOUND_ENDPOINTS = (("r1.apiRoot", "r1.apiRoot"), ("a1.apiRoot", "a1p.apiRoot"))


class LiveConsoleError(RuntimeError):
    """The profile does not name a live deployment this console can address."""


def _pinned_json(path: Path, expected_sha256: Optional[str], what: str
                 ) -> Dict[str, Any]:
    """Read *path*, refusing by name when its digest is not the pinned one."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise LiveConsoleError(f"cannot read {what}: {path}: {exc}") from None
    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha256 and digest != expected_sha256:
        raise LiveConsoleError(
            f"{what} digest moved: {path} is {digest}, the deployment pins "
            f"{expected_sha256}. Re-cut the deployment identity, or point the "
            "profile at the manifest this deployment was published with; "
            "submitting against a manifest that moved would be evidence about "
            "a different deployment.")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LiveConsoleError(f"{what} is not JSON: {path}: {exc}") from None


def _authority(url: str) -> str:
    parts = urlsplit(str(url))
    return f"{parts.scheme}://{parts.netloc}".lower()


def _cross_bind(*, values: Mapping[str, Any], binding: AssuranceLiveBinding,
                capability: Mapping[str, Any], values_path: Path,
                binding_path: Path) -> None:
    """Require the two identity documents to describe one deployment.

    Sol's second blocker.  Before this, the two halves of a live run were
    resolved independently: the R1 client is built from the *integration
    values* and the frozen ``DeploymentBinding`` the Kernel records comes from
    the *binding*.  Both were individually valid and nothing compared them, so
    a profile naming a second, perfectly well-formed integration-values file
    could send every policy to a different Near-RT RIC while the epoch, the
    evidence and the terminal state hash all recorded the first deployment.
    That is not a wrong number in a report -- it is evidence attributed to
    equipment that was never touched.

    Four checks, each refusing by name:

    1. the values document this run resolved must be one the binding was cut
       from -- same path **and** same digest as an entry in ``sources[]``;
    2. the R1 and A1-P endpoint authorities must agree;
    3. the capability manifest's cells must be exactly the binding's cells;
    4. its E2 node ids must be exactly the binding's ``e2Nodes``.

    (1) is the strong one and the other three are what makes a failure of (1)
    legible: a binding that was cut from these values cannot disagree with them,
    so if any of 2-4 fires, one of the two documents was edited after the
    binding was cut and the message says which field.
    """
    digests = {Path(path).resolve(): digest
               for path, digest in binding.source_digests.items()}
    resolved = values_path.resolve()
    if resolved not in digests:
        listed = ", ".join(str(path) for path in sorted(digests)) or "none"
        raise LiveConsoleError(
            f"{binding_path} was not cut from {values_path}: the binding's "
            f"identity sources are {listed}. A live run resolves its R1 client "
            "from the integration values and its frozen deployment binding "
            "from the binding; unless the binding pins these very values, the "
            "two may address different equipment and only one of them would "
            "reach the evidence.")
    # Re-read rather than trust the loader's read a moment ago: cheap, and it
    # is the only thing standing between this run and a values file swapped
    # between the binding's verification and the R1 client's construction.
    actual = hashlib.sha256(values_path.read_bytes()).hexdigest()
    if actual != digests[resolved]:
        raise LiveConsoleError(
            f"integration values digest moved: {values_path} is {actual}, the "
            f"binding {binding_path} was cut from {digests[resolved]}. Re-cut "
            "the binding, or point the profile at the values this binding "
            "pins; running would record one deployment and address another.")

    binding_endpoints = {"r1.apiRoot": binding.r1.api_root,
                         "a1p.apiRoot": binding.a1p.base_url}
    for values_key, binding_key in CROSS_BOUND_ENDPOINTS:
        stated, pinned = values.get(values_key), binding_endpoints[binding_key]
        if stated is None:
            continue
        if _authority(stated) != _authority(pinned):
            raise LiveConsoleError(
                f"{values_key} and the binding's {binding_key} name different "
                f"endpoints: {stated} against {pinned}. The client writes to "
                "the first and the Kernel records the second.")

    cells = sorted({int(cell["cellId"]["cId"]["ncI"])
                    for cell in (capability.get("topology") or {}).get("cells") or []
                    if isinstance(cell, Mapping)})
    if cells != sorted(int(cell) for cell in binding.cells):
        raise LiveConsoleError(
            f"the capability manifest advertises cells {cells} and the binding "
            f"names {sorted(int(cell) for cell in binding.cells)}. The topology "
            "maps E2 nodes onto cells and the binding states which cells this "
            "case may steer between; a disagreement means one of them is stale.")

    nodes: List[int] = []
    for cell in (capability.get("topology") or {}).get("cells") or []:
        try:
            nodes.append(int(str(cell["globalE2NodeId"]["nodeId"]["hex"]), 16))
        except (KeyError, TypeError, ValueError):
            raise LiveConsoleError(
                "the capability manifest states an E2 node id this deployment "
                "cannot read") from None
    if sorted(set(nodes)) != sorted({int(node, 16) for node in binding.e2_nodes}):
        raise LiveConsoleError(
            "the capability manifest's E2 nodes and the binding's e2Nodes "
            f"disagree: {[hex(node) for node in sorted(set(nodes))]} against "
            f"{sorted(binding.e2_nodes)}. The KPM reader attributes a UE to a "
            "cell through this map, so a disagreement mis-attributes the UE.")


@dataclass(frozen=True)
class ActionProducerType:
    """One A1 policy type the action producer serves, and its fixed adapter."""

    policy_type_id: str
    adapter: str
    action_id: str


@dataclass(frozen=True)
class ActionProducer:
    """The second A1-P endpoint: the in-repo producer for Style-2 actions.

    Separate from the stage producer on purpose.  The released steering
    producer is immutable and owns ``AIC_UECellSteering_1.0.0``; the
    supplementary UE controls are served by the in-repo Campaign 5 producer,
    which is a different process at a different address with its own
    credentials.  A composition that reached both through one endpoint would be
    claiming a deployment that does not exist.
    """

    api_root: str
    policy_types: Mapping[str, ActionProducerType]
    secret_refs: Mapping[str, str]
    state_dir: Optional[Path] = None

    def adapters(self) -> Mapping[str, ActionProducerType]:
        """Adapter key -> the type it is permanently bound to."""
        return {entry.adapter: entry for entry in self.policy_types.values()}

    def for_action(self, action_id: str) -> Optional[ActionProducerType]:
        for entry in self.policy_types.values():
            if entry.action_id == action_id:
                return entry
        return None


@dataclass(frozen=True)
class LiveDeployment:
    """One deployment, addressed entirely from one profile document."""

    profile: ExperimentProfile
    document_path: Path
    binding_path: Path
    integration_values_path: Path
    capability_path: Path
    producer_database: Path
    r1_state_dir: Path
    evidence_dir: Path
    binding: AssuranceLiveBinding
    values: Mapping[str, Any]
    capability: Mapping[str, Any]
    #: The second producer, when the profile names one.  ``None`` is the
    #: single-endpoint deployment this console has always composed.
    action_producer: Optional[ActionProducer] = None
    #: The UE this profile addresses, when it names one.  Optional: a
    #: single-UE deployment need not, and a profile that names none is refused
    #: at observation time unless exactly one UE is fresh on the stream.
    requested_amf_ue_ngap_id: Optional[int] = None
    #: The heavy, non-target UE a SUPPLEMENTARY control may act on, when the
    #: deployment names one.  ``None`` composes no supplementary control at
    #: all: which UE may be capped is a decision about the traffic in the cell,
    #: and there is no default a composition root may pick.
    requested_controlled_amf_ue_ngap_id: Optional[int] = None

    @property
    def kpm_jsonl_path(self) -> Path:
        """The live indication stream, named by the binding and nowhere else."""
        return Path(self.binding.kpm_jsonl_path)

    def summary(self) -> Dict[str, Any]:
        """What this run addressed, for the preflight record.  No secret."""
        return {
            "profileId": self.profile.profile_id,
            "profileDocument": str(self.document_path),
            "bindingPath": str(self.binding_path),
            "bindingId": self.binding.binding_id,
            "sourceDigests": dict(self.binding.source_digests),
            "integrationValuesPath": str(self.integration_values_path),
            "capabilityPath": str(self.capability_path),
            "capabilitySha256": self.values.get(
                "backend.capabilityManifestSha256"),
            "producerDatabase": str(self.producer_database),
            "requestedAmfUeNgapId": self.requested_amf_ue_ngap_id,
            "requestedControlledAmfUeNgapId":
                self.requested_controlled_amf_ue_ngap_id,
            "kpmJsonlPath": str(self.kpm_jsonl_path),
            "r1StateDir": str(self.r1_state_dir),
            "evidenceDir": str(self.evidence_dir),
            "r1ApiRoot": self.binding.r1.api_root,
            "policyTypeId": self.binding.r1.policy_type_id,
            "actionProducerApiRoot": (
                self.action_producer.api_root
                if self.action_producer is not None else None),
            "actionProducerPolicyTypes": (
                {entry.policy_type_id: {"adapter": entry.adapter,
                                        "actionId": entry.action_id}
                 for entry in self.action_producer.policy_types.values()}
                if self.action_producer is not None else {}),
        }


def _controlled_amf_ue_ngap_id(
    block: Mapping[str, Any], path: Path
) -> Optional[int]:
    """Parse the optional controlled non-target UE, or refuse it by name.

    Absent is legitimate and composes nothing.  Present and malformed is
    refused rather than ignored: a profile that meant to name a UE to cap and
    misspelled it would otherwise run a steering-only case while the operator
    believed a cap was live.
    """
    declared = block.get("controlledUe")
    if declared is None:
        return None
    if not isinstance(declared, Mapping):
        raise LiveConsoleError(
            f"{path}: {LIVE_CONSOLE_KEY}.controlledUe must be an object")
    value = declared.get("amfUeNgapId")
    if value is None:
        raise LiveConsoleError(
            f"{path}: {LIVE_CONSOLE_KEY}.controlledUe names no amfUeNgapId; a "
            "supplementary control acts on a UE, and which one is not "
            "something this console may choose")
    return _amf_ue_ngap_id(value, path)


def _action_producer(
    block: Mapping[str, Any], path: Path, resolve
) -> Optional[ActionProducer]:
    """Parse the optional second producer endpoint, or refuse it by name.

    Absent is legitimate and is the deployment this console composed before:
    one endpoint, one adapter, steering only.  Present and malformed is
    refused rather than half-read -- a composition that silently dropped the
    cap adapter would run a steering-only trial while the Operator believed a
    supplementary action was live.
    """
    declared = block.get(ACTION_PRODUCER_KEY)
    if declared is None:
        return None
    if not isinstance(declared, Mapping):
        raise LiveConsoleError(
            f"{path}: {LIVE_CONSOLE_KEY}.{ACTION_PRODUCER_KEY} must be an object")
    api_root = str(declared.get("apiRoot") or "")
    if not api_root:
        raise LiveConsoleError(
            f"{path}: {ACTION_PRODUCER_KEY} names no apiRoot; the second A1-P "
            "endpoint has no honest default")
    types = declared.get("policyTypes")
    if not isinstance(types, Mapping) or not types:
        raise LiveConsoleError(
            f"{path}: {ACTION_PRODUCER_KEY}.policyTypes must map each policy "
            "type to the adapter permanently bound to it")
    parsed: Dict[str, ActionProducerType] = {}
    adapters: Dict[str, str] = {}
    for policy_type_id, body in types.items():
        if not isinstance(body, Mapping):
            raise LiveConsoleError(
                f"{path}: {ACTION_PRODUCER_KEY}.policyTypes.{policy_type_id} "
                "must be an object")
        adapter = str(body.get("adapter") or "")
        action_id = str(body.get("actionId") or "")
        if not adapter or not action_id:
            raise LiveConsoleError(
                f"{path}: {ACTION_PRODUCER_KEY}.policyTypes.{policy_type_id} "
                "needs both adapter and actionId")
        if adapter in adapters:
            raise LiveConsoleError(
                f"{path}: adapter {adapter!r} is bound to both "
                f"{adapters[adapter]} and {policy_type_id}; one adapter carries "
                "one policy type, fixed at composition")
        adapters[adapter] = str(policy_type_id)
        parsed[str(policy_type_id)] = ActionProducerType(
            policy_type_id=str(policy_type_id), adapter=adapter, action_id=action_id)
    refs = declared.get("secretRefs") or {}
    if not isinstance(refs, Mapping):
        raise LiveConsoleError(
            f"{path}: {ACTION_PRODUCER_KEY}.secretRefs must be an object of "
            "references")
    state_dir = declared.get("r1StateDir")
    return ActionProducer(
        api_root=api_root,
        policy_types=parsed,
        secret_refs={str(key): str(value) for key, value in refs.items()},
        state_dir=resolve(str(state_dir)) if state_dir else None,
    )


def _document(path: Path) -> Mapping[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise LiveConsoleError(f"live profile not found: {path}") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise LiveConsoleError(f"live profile is not readable JSON: {path}: {exc}") from None
    if not isinstance(document, Mapping):
        raise LiveConsoleError(f"live profile must be a JSON object: {path}")
    return document


def _block(document: Mapping[str, Any], path: Path) -> Mapping[str, Any]:
    block = document.get(LIVE_CONSOLE_KEY)
    if block is None:
        raise LiveConsoleError(
            f"{path} carries no {LIVE_CONSOLE_KEY!r} block, so it names a "
            "Replay/Disconnected profile rather than a live deployment. Add "
            f"{LIVE_CONSOLE_KEY} with " + " and ".join(REQUIRED_LIVE_KEYS) + ".")
    if not isinstance(block, Mapping):
        raise LiveConsoleError(f"{path}: {LIVE_CONSOLE_KEY} must be an object")
    missing = [key for key in REQUIRED_LIVE_KEYS if not block.get(key)]
    if missing:
        raise LiveConsoleError(
            f"{path}: {LIVE_CONSOLE_KEY} is missing " + ", ".join(missing)
            + ". These have no honest default; name them rather than letting "
              "this run address a deployment nobody stated.")
    return block


def _amf_ue_ngap_id(value: Any, document_path: Path) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise LiveConsoleError(
            f"{document_path}: {LIVE_CONSOLE_KEY}.amfUeNgapId is {value!r}, "
            "which is not an integer amfUeNgapId") from None


def load_live_deployment(path: str | Path) -> LiveDeployment:
    """Resolve every live input from one profile document, or refuse by name."""
    document_path = Path(path)
    document = _document(document_path)
    here = document_path.parent

    def resolve(value: str) -> Path:
        candidate = Path(str(value))
        return candidate if candidate.is_absolute() else (here / candidate)

    try:
        profile = ExperimentProfile.load(document_path)
    except ProfileError as exc:
        raise LiveConsoleError(f"{document_path}: {exc}") from None
    block = _block(document, document_path)

    if not profile.integration_values_path:
        raise LiveConsoleError(
            f"{document_path} names no integrationValuesPath, so it cannot back "
            "a Live session (gui/operator/session/profile.py says as much).")
    if not profile.capability_manifest_path:
        raise LiveConsoleError(
            f"{document_path} names no capabilityManifestPath; the capability "
            "manifest is what maps E2 node ids onto cells and what the policy "
            "type discovery is checked against.")

    integration_values_path = resolve(profile.integration_values_path)
    try:
        values = dict(load_integration_values(integration_values_path)["values"])
    except IntegrationValuesError as exc:
        raise LiveConsoleError(
            f"integration values refused: {integration_values_path}: {exc}") from None

    capability_path = resolve(profile.capability_manifest_path)
    # The values name the manifest too, relative to their own document.  Two
    # names for one file is one too many, so they must be the same file before
    # the digest is even consulted.
    named = values.get("backend.capabilityManifestPath")
    if named:
        expected = Path(str(named))
        if not expected.is_absolute():
            expected = integration_values_path.parent / expected
        if expected.resolve() != capability_path.resolve():
            raise LiveConsoleError(
                f"{document_path} names capability manifest {capability_path}, "
                f"but its integration values name {expected}. One deployment, "
                "one manifest: fix the profile rather than running against "
                "whichever of the two answers first.")
    capability = _pinned_json(
        capability_path, values.get("backend.capabilityManifestSha256"),
        "capability manifest")

    binding_path = resolve(str(block["assuranceBindingPath"]))
    try:
        binding = load_assurance_live_binding(binding_path)
    except LiveBindingError as exc:
        # The loader already names the source whose digest moved; keeping its
        # message and adding the binding is what makes the refusal actionable.
        raise LiveConsoleError(f"live binding refused: {binding_path}: {exc}") from None
    _cross_bind(values=values, binding=binding, capability=capability,
                values_path=integration_values_path, binding_path=binding_path)

    runs_root = resolve(profile.runs_root) if profile.runs_root else here
    default_root = runs_root / "liveconsole"
    r1_state_dir = (resolve(str(block["r1StateDir"])) if block.get("r1StateDir")
                    else default_root / "r1-state")
    evidence_dir = (resolve(str(block["evidenceDir"])) if block.get("evidenceDir")
                    else default_root / "evidence")

    action_producer = _action_producer(block, document_path, resolve)

    return LiveDeployment(
        profile=profile,
        document_path=document_path,
        binding_path=binding_path,
        integration_values_path=integration_values_path,
        capability_path=capability_path,
        producer_database=resolve(str(block["producerDatabasePath"])),
        r1_state_dir=r1_state_dir,
        evidence_dir=evidence_dir,
        binding=binding,
        values=values,
        capability=capability,
        action_producer=action_producer,
        requested_amf_ue_ngap_id=(
            None if block.get("amfUeNgapId") is None
            else _amf_ue_ngap_id(block["amfUeNgapId"], document_path)),
        requested_controlled_amf_ue_ngap_id=_controlled_amf_ue_ngap_id(
            block, document_path),
    )
