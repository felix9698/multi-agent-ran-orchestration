"""GUI-free typed-intent entry point for the O-RAN rApp profile."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from decision.llm_backend import LLMBackendManager

from decision.intent_model import (
    ConstraintType, Intent, IntentPriority, IntentScope, IntentTarget, IntentType,
)

from .assurance import CombinedAssurance
from .contract_support import validate
from .evidence import EvidenceLedger
from .policy_translator import PolicyTranslationContext
from .r1_client import R1Client
from .r1_security import build_r1_security
from oran.contract.integration_values import load_integration_values as load_contract_integration_values
from oran.integration.deployment_binding import (
    DeploymentBindingContracts,
    resolve_deployment_binding,
)
from oran.integration.objectives import assert_submittable

# Compatibility seam for callers that patch the retired resolver spelling.
resolve_binding = resolve_deployment_binding


def load_integration_values(path: str | Path) -> Dict[str, Any]:
    """Return kernel-validated values without a second endpoint source."""
    return dict(load_contract_integration_values(path)["values"])


def _load_pinned_json(config_path: Path, artifact_path: str,
                      expected_sha256: str) -> Dict[str, Any]:
    path = Path(artifact_path)
    if not path.is_absolute():
        path = config_path.parent / path
    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha256:
        raise ValueError(f"artifact digest mismatch for {artifact_path}")
    return json.loads(raw.decode("utf-8"))


def _typed_intent(value: Dict[str, Any]) -> Intent:
    allowed = {"id", "type", "target", "scope", "priority", "description",
               "createdAt", "expiresAt"}
    if set(value) - allowed:
        raise ValueError("typed intent contains unknown fields")
    target = value["target"]
    scope = value["scope"]
    return Intent(
        id=value["id"], type=IntentType(value["type"]),
        target=IntentTarget(
            kpi_name=target["kpiName"],
            constraint_type=ConstraintType(target["constraintType"]),
            target_value=target["targetValue"], unit=target["unit"]),
        scope=IntentScope(ue_ids=list(scope["ueIds"]),
                          bs_ids=list(scope["bsIds"]),
                          cell_ids=list(scope["cellIds"]),
                          area_id=scope.get("areaId")),
        priority=IntentPriority[value["priority"]],
        description=value.get("description", ""),
        expires_at=(datetime.fromisoformat(value["expiresAt"][:-1] + "+00:00")
                    if value.get("expiresAt") else None))


def _context(value: Dict[str, Any]) -> PolicyTranslationContext:
    return PolicyTranslationContext(
        ue_id=value["ueId"], allowed_cells=value["allowedCells"],
        forbidden_cells=value["forbiddenCells"],
        objective_kind=value["objectiveKind"],
        improvement_threshold_prb=value.get("improvementThresholdPrb"),
        min_seconds_between_actuations=value["minSecondsBetweenActuations"],
        required_kpi_freshness_ms=value["requiredKpiFreshnessMs"],
        action_deadline_ms=value["actionDeadlineMs"],
        not_before=value["notBefore"], expires_at=value["expiresAt"],
        rollback_on=value["rollbackOn"],
        rollback_timeout_ms=value["rollbackTimeoutMs"],
        intent_revision=value["intentRevision"],
        policy_revision=value["policyRevision"],
        correlation_id=value["correlationId"], producer_id=value["producerId"],
        intent_id=value.get("intentId"))


#: The only values a deployment may resolve at runtime instead of stating in
#: its integration-values document: endpoints, and nothing else.
#:
#: This list is the whole of the overlay contract, and it lives here - in the
#: authority every caller goes through - rather than in any one caller.  An
#: allowlist enforced only at the GUI seam is not an allowlist: it is a
#: convention that the next caller of ``run_once`` silently opts out of.
#:
#: What is deliberately absent is everything that establishes *who* and *what*
#: is being addressed: ``r1.rAppId`` and the other identity values, every
#: ``backend.*`` artifact path and digest, ``deployment.testVector*``, and every
#: credential, certificate and trust reference.  Those are the document's own
#: pinned claims; an overlay that could rewrite them could point a validated
#: deployment at a different producer, a different capability manifest or a
#: different trust root while still passing the kernel's validation.
RUNTIME_ENDPOINT_KEYS: tuple = (
    "r1.apiRoot",
    "a1.apiRoot",
    "a1.notificationDestination",
    "r1.dme.policyEvidencePushBaseUri",
    "o1.fileDataReporting.mnsRoot",
    "o1.fileDataReporting.consumerReference",
)


def _endpoint_overlay(runtime_values: Dict[str, Any] | None) -> Dict[str, Any]:
    """Accept an endpoint-only overlay, or refuse it by name.

    Fail-closed: one key outside :data:`RUNTIME_ENDPOINT_KEYS` refuses the whole
    overlay rather than applying the acceptable part of it, because a caller
    that asked to rewrite an identity has to be told, not quietly half-obeyed.
    """
    overlay = {str(key): value for key, value in (runtime_values or {}).items()}
    refused = sorted(set(overlay) - set(RUNTIME_ENDPOINT_KEYS))
    if refused:
        raise ValueError(
            "runtime_values may overlay endpoints only; refused "
            f"{', '.join(refused)}. Identity, artifact digest, deployment "
            "vector and credential values are pinned by the integration-values "
            "document and cannot be replaced at runtime. Permitted keys: "
            + ", ".join(RUNTIME_ENDPOINT_KEYS))
    return overlay


def run_once(*, integration_path: str, request: Dict[str, Any],
             insecure_dev: bool = False,
             now: datetime | None = None,
             llm_manager: LLMBackendManager | None = None,
             coordinator_config=None,
             runtime_values: Dict[str, Any] | None = None,
             composed_release_root: Any = None,
             composed_release_contracts: DeploymentBindingContracts | None = None,
             # Retired spellings of the two parameters above.  They are kept
             # because the preserved history-only GUI entry
             # (``oran/rapp/gui_entry.py``) passes them by name and that file
             # is not edited; new callers must use the neutral names.
             lower_release_root: Any = None,
             lower_contracts: DeploymentBindingContracts | None = None) -> Dict[str, Any]:
    """Run one authoritative episode against the deployment named by ``integration_path``.

    ``runtime_values`` overlays already-resolved *endpoint* values on top of the
    kernel-validated document.  It exists for one case only: a deployment whose
    endpoints live in its own digest-pinned deployment vector rather than in the
    integration-values document, which is how the loopback development profile
    is defined.  Only :data:`RUNTIME_ENDPOINT_KEYS` may be overlaid, and the
    check happens here, before anything is read or built, so no caller can reach
    the episode with an overlay this entry would not accept.  The caller is
    responsible for having verified that vector's digest; nothing here invents
    an endpoint, and a caller that passes nothing gets exactly the previous
    behaviour.

    ``composed_release_contracts`` - or ``composed_release_root``, which loads
    them - binds this episode to a verified composed release.  The deployment
    normally declares that binding itself, beside its integration-values
    document, so neither the GUI nor a headless caller has to carry it.  When a binding is in force the
    executable objective set narrows to what the composed release advertises as
    official, and the objective is checked *here*, before the R1 client exists,
    so a refused objective causes no bootstrap, no discovery, no policy and no
    RAN write.
    """
    overlay = _endpoint_overlay(runtime_values)
    config_path = Path(integration_path).resolve()
    values = load_integration_values(config_path)
    if overlay:
        values = {**values, **overlay}
    capability = _load_pinned_json(
        config_path, values["backend.capabilityManifestPath"],
        values["backend.capabilityManifestSha256"])
    validate(capability, "aic:ran-capability:1.0.0")
    contracts = (composed_release_contracts if composed_release_contracts
                 is not None else lower_contracts)
    root = (composed_release_root if composed_release_root is not None
            else lower_release_root)
    composed = (contracts if contracts is not None else
                resolve_binding(config_path, override=root))
    if composed is not None:
        # The final composition, and therefore the narrow one.  The check runs
        # before any transport exists, so an objective this composition does not
        # advertise cannot produce an R1 bootstrap, let alone a policy or a RAN
        # write.  An unbound development deployment keeps exactly its previous
        # behaviour: the capability admission inside ``translate_intent`` is
        # still what refuses an objective its manifest does not advertise, and
        # nothing here loosens that.
        assert_submittable(request["policyContext"].get("objectiveKind"),
                           capability_manifest=capability, lower=composed)
    state_path = request["statePath"]
    evidence_path = request["evidencePath"]
    # Secure R1 is mutually authenticated and authorized, and the material for
    # both comes from this document's own pinned credential references - the
    # same source, through the same builder, as the console's read-only client.
    # One episode entry, one way of establishing R1 trust: a console that bound
    # securely and an episode that could not would be two transports.
    security = None if insecure_dev else build_r1_security(values)
    client = R1Client(
        api_root=values["r1.apiRoot"], r_app_id=values["r1.rAppId"],
        policy_evidence_push_base_uri=values["r1.dme.policyEvidencePushBaseUri"],
        state_path=state_path, insecure_dev=insecure_dev,
        ssl_context=None if security is None else security.ssl_context,
        oauth_header_provider=(
            None if security is None else security.oauth_header_provider))
    # Imported here rather than at module scope: ``load_integration_values``
    # in this module is a plain settings loader that the Kernel-path OTA runner
    # also uses, and a module-level import would drag the preserved
    # ``IntentCoordinator`` into that process for nothing.
    from .coordinator_adapter import RAppCoordinatorAdapter

    adapter = RAppCoordinatorAdapter(
        dispatch=client, assurance=CombinedAssurance(capability),
        ledger=EvidenceLedger(evidence_path), capability_manifest=capability,
        llm_manager=llm_manager, config=coordinator_config)
    identifiers = dict(request["identifiers"])
    for name in ("run_id", "episode_id", "cycle_id", "proposal_id", "trial_id"):
        identifiers.setdefault(name, f"{name[:-3]}-{uuid.uuid4().hex[:12]}")
    if "intentText" in request:
        if "intent" in request:
            raise ValueError("provide exactly one of intentText or intent")
        intent_input = request["intentText"]
        if not isinstance(intent_input, str) or not intent_input.strip():
            raise ValueError("intentText must be a non-empty string")
    else:
        intent_input = _typed_intent(request["intent"])
    return adapter.process_intent(
        intent_input, context=_context(request["policyContext"]),
        evidence_records=request.get("evidenceRecords", []),
        now=now or datetime.now(timezone.utc), identifiers=identifiers)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Headless O-RAN rApp episode")
    parser.add_argument("--integration-values", required=True)
    parser.add_argument("--intent-file", help="typed intent episode JSON; stdin otherwise")
    parser.add_argument("--insecure-dev", action="store_true",
                        help="allow loopback plaintext for Phase A only")
    args = parser.parse_args(argv)
    try:
        if args.intent_file:
            request = json.loads(Path(args.intent_file).read_text(encoding="utf-8"))
        else:
            request = json.load(sys.stdin)
        result = run_once(integration_path=args.integration_values,
                          request=request, insecure_dev=args.insecure_dev)
        json.dump(result, sys.stdout, separators=(",", ":"), sort_keys=True)
        sys.stdout.write("\n")
        return 0 if result.get("success") else 2
    except Exception as exc:
        json.dump({"success": False, "terminal_outcome": "technical_failsafe",
                   "error": str(exc)}, sys.stdout, separators=(",", ":"),
                  sort_keys=True)
        sys.stdout.write("\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
