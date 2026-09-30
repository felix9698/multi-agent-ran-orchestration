"""O-RAN GUI episode adapter with headless-equivalent decision semantics.

**History-only.**  This module is the composition surface of the pre-Kernel
Coordinator runtime, preserved so the published S0-S6 campaign stays
reproducible.  It is *not* part of the deployed product: the Operator Console
no longer imports it, and the only thing that hands it to a console is the
explicitly opt-in ``tools.legacy.episode_support``.  The deployed write path is
Operator Console -> Assurance Kernel -> Write Gateway.

It is left importable rather than fail-closed on import on purpose.  The
reachability property this cutover needed is *structural* - the console holds
no import of this module and cannot construct one - and a module-level refusal
here would additionally break the preserved evidence replays and the tests that
read this runtime directly, without making the deployed entry point any safer
than it already is.

Two things live here, and they are deliberately the same two things the
headless rApp uses:

* :func:`run_gui_once` - the canonical episode entry.  It runs the headless
  authority first and presents afterwards, so no display can alter an outcome.
* :class:`LiveIntegration` - the composition root that turns one
  integration-values document into the facts an operator console needs before
  it may claim a Live session: the digest-pinned capability manifest, a
  read-only R1 client for preflight and status, the deployment's identity for
  the export header, and a bound submit that reaches :func:`run_gui_once`.

The console owned no second runtime: everything it could do to *this*
deployment it did through this module, which is why an intent submitted from
the GUI and an intent submitted headlessly traverse identical code.  That
equivalence still holds for the preserved campaign.  What changed at the
cutover is who may reach it - a console has to be handed this runtime by
``tools.legacy.episode_support``, and the deployed one never is.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from .headless import (RUNTIME_ENDPOINT_KEYS, _endpoint_overlay,
                       _load_pinned_json, run_once)
from oran.contract.integration_values import load_integration_values
from oran.integration.lower_release import (LowerReleaseContracts,
                                            LowerReleaseError, resolve_binding)
from oran.integration.objectives import AdvertisedComposition, advertise

#: Re-exported, not restated.  The overlay allowlist belongs to the episode
#: authority in ``headless``; a second copy here could drift from it, and a
#: console that checked its own copy would prove nothing about what the episode
#: entry accepts.  ``RUNTIME_ENDPOINT_KEYS`` is imported above for that reason.


class IntegrationError(ValueError):
    """An integration input that cannot be trusted enough to go Live."""


#: Where each overlayable endpoint lives inside a deployment vector.  A
#: deployment that declares its endpoints in the vector the integration-values
#: document already pins declares them at exactly these paths.
_VECTOR_ENDPOINTS: tuple = (
    ("r1.apiRoot", ("r1", "apiRoot")),
    ("a1.apiRoot", ("a1", "apiRoot")),
    ("a1.notificationDestination", ("a1", "statusCallbackRoot")),
    ("r1.dme.policyEvidencePushBaseUri", ("r1", "dme", "policyEvidencePushBaseUri")),
    ("o1.fileDataReporting.mnsRoot", ("o1", "fileDataReporting", "mnsRoot")),
    ("o1.fileDataReporting.consumerReference",
     ("o1", "fileDataReporting", "consumerReference")),
)


def endpoints_from_deployment_vector(integration_path: Any,
                                     values: Mapping[str, Any]) -> Dict[str, Any]:
    """Read the endpoints out of the document's own digest-pinned vector.

    Some deployments - the loopback development profile is the one in this
    repository - state their endpoints in the deployment vector rather than in
    the integration-values document, because the frozen values schema requires
    HTTPS and a loopback development stack is HTTP.  The vector is named *and
    byte-pinned* by that same document, so reading it is reading the document's
    own pinned artifact, exactly as the capability manifest is read.

    Nothing is invented: only the six overlayable endpoint slots are taken, only
    when the vector declares them, and the digest is verified first.  An
    operator therefore does not have to hand-configure endpoints that the
    deployment has already published and pinned.
    """
    path = Path(integration_path).resolve()
    reference = values.get("deployment.testVectorPath")
    digest = values.get("deployment.testVectorSha256")
    if not reference or not digest:
        raise IntegrationError(
            "this integration-values document names no deployment vector, so "
            "there are no published endpoints to resolve from one")
    try:
        vector = _load_pinned_json(path, str(reference), str(digest))
    except Exception as exc:
        raise IntegrationError(
            f"deployment vector named by {path} was not accepted: {exc}") from exc
    resolved: Dict[str, Any] = {}
    for key, route in _VECTOR_ENDPOINTS:
        current: Any = vector
        for part in route:
            if not isinstance(current, Mapping) or part not in current:
                current = None
                break
            current = current[part]
        if isinstance(current, str) and current:
            resolved[key] = current
    if not resolved:
        raise IntegrationError(
            "the deployment vector declares none of the endpoints an overlay "
            "may carry")
    return resolved


def run_gui_once(*, integration_path: str, request: dict[str, Any],
                 present: Callable[[Mapping[str, Any]], None], **kwargs: Any) -> dict[str, Any]:
    """Execute the headless authority first, then best-effort presentation.

    A broken widget or display callback cannot alter the returned terminal
    outcome or evidence because presentation receives a deep copy only after
    the authoritative episode has completed.
    """
    result = run_once(integration_path=integration_path, request=request, **kwargs)
    authoritative = deepcopy(result)
    try:
        present(deepcopy(authoritative))
    except Exception:
        pass
    return authoritative


@dataclass(frozen=True)
class LiveIntegration:
    """One resolved deployment, ready to be named, checked and submitted to.

    Construction is the check: an unreadable document, a capability manifest
    whose bytes do not match the pinned digest, or an overlay that names a key
    outside :data:`RUNTIME_ENDPOINT_KEYS` all raise here rather than becoming a
    console that looks connected.
    """

    integration_path: str
    document: Mapping[str, Any]
    values: Mapping[str, Any]
    capability_manifest: Mapping[str, Any]
    state_path: str
    evidence_path: str
    insecure_dev: bool = False
    runtime_values: Mapping[str, Any] = field(default_factory=dict)
    lower: Optional[LowerReleaseContracts] = None

    # -- construction ------------------------------------------------------- #

    @classmethod
    def load(cls, integration_path: Any, *, state_dir: Any,
             runtime_values: Optional[Mapping[str, Any]] = None,
             insecure_dev: bool = False,
             endpoints_from_vector: bool = False,
             lower_release_root: Any = None) -> "LiveIntegration":
        """Resolve one integration-values document into a usable integration.

        ``runtime_values`` carries endpoints a deployment publishes through its
        own digest-pinned deployment vector.  It is passed straight through to
        the same overlay the headless entry applies, and it is restricted to
        endpoint keys so it cannot silently redirect identity or trust material.

        ``endpoints_from_vector`` asks this loader to read those same endpoints
        out of the vector the document pins, instead of requiring the caller to
        have extracted them.  That is what lets an operator bind a development
        deployment from the GUI without hand-configuring anything - and it is
        still the document's own pinned artifact being read.

        The Lower release binding is resolved here too, from the deployment's
        own binding file or from ``lower_release_root``.  Binding it at load
        time is what makes an unreachable, incomplete or misidentified Lower
        release a *binding* failure rather than a submit-time surprise: a
        console that came up bound is a console whose Lower contracts were
        verified byte for byte.
        """
        path = Path(integration_path).resolve()
        try:
            document = load_integration_values(path)
        except Exception as exc:
            raise IntegrationError(
                f"integration values at {path} were not accepted: {exc}") from exc
        if endpoints_from_vector:
            published = endpoints_from_deployment_vector(
                path, document["values"])
            # A caller-supplied overlay still wins: it is the more specific
            # statement, and it goes through the same allowlist either way.
            runtime_values = {**published, **dict(runtime_values or {})}
        # The same check the episode entry applies, applied early so a console
        # refuses at binding time rather than at submit time.  It is literally
        # the same function: there is one allowlist, not one per caller.
        try:
            overlay = _endpoint_overlay(runtime_values)
        except ValueError as exc:
            raise IntegrationError(str(exc)) from exc
        values = {**dict(document["values"]), **overlay}
        try:
            capability = _load_pinned_json(
                path, values["backend.capabilityManifestPath"],
                values["backend.capabilityManifestSha256"])
        except Exception as exc:
            raise IntegrationError(
                f"capability manifest named by {path} was not accepted: "
                f"{exc}") from exc
        try:
            lower = resolve_binding(path, override=lower_release_root)
        except LowerReleaseError as exc:
            raise IntegrationError(
                f"the Lower release this deployment declares was not accepted: "
                f"{exc}") from exc
        root = Path(state_dir)
        root.mkdir(parents=True, exist_ok=True)
        return cls(
            integration_path=str(path), document=dict(document), values=values,
            capability_manifest=capability,
            state_path=str(root / "rapp-r1-state.json"),
            evidence_path=str(root / "rapp-evidence.jsonl"),
            insecure_dev=bool(insecure_dev), runtime_values=overlay, lower=lower)

    # -- identity ----------------------------------------------------------- #

    def identity(self) -> Dict[str, Any]:
        """Contract and release identity, for status, provenance and export.

        Every value is read from the document or the pinned manifest.  A field
        the deployment did not declare stays absent rather than becoming a
        plausible-looking string.
        """
        values = self.values
        manifest = self.capability_manifest
        identity = {
            "integrationValuesPath": self.integration_path,
            "contractProfile": self.document.get("contractProfile"),
            "deploymentMode": self.document.get("deploymentMode"),
            "schemaVersion": self.document.get("schemaVersion"),
            "bundleManifestJcsSha256": self.document.get("bundleManifestJcsSha256"),
            "capabilityManifestId": manifest.get("manifestId"),
            "capabilityManifestSha256": values.get("backend.capabilityManifestSha256"),
            "capabilityContractProfile": manifest.get("contractProfile"),
            "releaseManifestSha256": values.get("backend.releaseManifestSha256"),
            "e2CapabilityInventorySha256": values.get("backend.e2CapabilityInventorySha256"),
            "deploymentVectorSha256": values.get("deployment.testVectorSha256"),
            "nearRtRicId": manifest.get("nearRtRicId"),
            "hardwareProfileId": manifest.get("hardwareProfileId"),
            "sourceRevision": manifest.get("sourceRevision"),
            "r1ApiRoot": values.get("r1.apiRoot"),
            "rAppId": values.get("r1.rAppId"),
            "dmeTypeId": manifest.get("dmeTypeId"),
            "endpointSource": ("DEPLOYMENT_VECTOR_RUNTIME_OVERLAY"
                               if self.runtime_values else "INTEGRATION_VALUES"),
            "insecureDevLoopback": self.insecure_dev,
        }
        if self.lower is not None:
            identity["lowerReleaseBinding"] = self.lower.binding()
        try:
            identity["compositionBasis"] = self.advertised_objectives().basis
        except Exception as exc:
            # Provenance must not be able to abort an export or a status read.
            # The submit path calls the same advertisement and *does* fail
            # closed, so degrading here hides nothing: it only keeps a header
            # readable while the reason is stated in it.
            identity["compositionBasis"] = f"UNAVAILABLE: {exc}"
        return {key: value for key, value in identity.items() if value is not None}

    def advertised_objectives(self) -> AdvertisedComposition:
        """What this deployment may offer, select and submit.

        The console renders this rather than deciding for itself: the executable
        set is the Upper allowlist intersected with the deployment's capability
        manifest and, when bound, the Lower release's official objectives.  A
        non-executable objective is returned with the reason so the reason can
        be shown, and the Lower release's experimental extensions come back as
        extensions - never as something an operator could select.
        """
        return advertise(capability_manifest=self.capability_manifest,
                         lower=self.lower)

    # -- read-only surfaces -------------------------------------------------- #

    def r1_client(self) -> Any:
        """A client for the read paths preflight and the status projection use.

        It is the same client the episode uses; the console simply never calls a
        mutating method on it, which the boundary gate enforces mechanically for
        the projection module.

        A secure deployment gets the transport security ``R1Client`` requires -
        a mutually authenticated TLS context and an OAuth authorization hook -
        built from the credential references this deployment's own
        integration-values document declares.  Those references are read from
        the pinned document, never from the endpoint overlay: an endpoint may be
        resolved at runtime, a trust anchor may not.  A loopback development
        deployment (``insecure_dev``) is unchanged and needs none of it.
        """
        from .r1_client import R1Client
        from .r1_security import build_r1_security

        security = None if self.insecure_dev else build_r1_security(self.values)
        return R1Client(
            api_root=self.values["r1.apiRoot"],
            r_app_id=self.values["r1.rAppId"],
            policy_evidence_push_base_uri=self.values["r1.dme.policyEvidencePushBaseUri"],
            state_path=self.state_path, insecure_dev=self.insecure_dev,
            ssl_context=None if security is None else security.ssl_context,
            oauth_header_provider=(
                None if security is None else security.oauth_header_provider))

    # -- the write path ------------------------------------------------------ #

    def episode_request(self, *, intent_text: str,
                        policy_context: Mapping[str, Any],
                        identifiers: Optional[Mapping[str, Any]] = None,
                        evidence_records: Any = ()) -> Dict[str, Any]:
        """Build the episode request for one operator intent.

        The policy context is passed through untouched.  A context that is
        missing a behaviour-bearing value is refused here, by name, rather than
        being completed with a default - the contract forbids inventing one, and
        an operator is entitled to know which value the deployment owes.
        """
        text = str(intent_text or "").strip()
        if not text:
            raise IntegrationError("intentText must be a non-empty string")
        missing = sorted(set(REQUIRED_POLICY_CONTEXT_KEYS) - set(policy_context or {}))
        if missing:
            raise IntegrationError(
                "the experiment profile's policyContext is missing "
                f"{', '.join(missing)}; the deployment owns these values and "
                "no default may be invented for them")
        return {
            "statePath": self.state_path,
            "evidencePath": self.evidence_path,
            "intentText": text,
            "policyContext": deepcopy(dict(policy_context)),
            "evidenceRecords": [deepcopy(dict(record)) for record in evidence_records],
            "identifiers": dict(identifiers or {}),
        }

    def runner_kwargs(self, *, llm_manager: Any = None,
                      now: Optional[datetime] = None) -> Dict[str, Any]:
        """Keyword arguments that bind :func:`run_gui_once` to this deployment."""
        kwargs: Dict[str, Any] = {"insecure_dev": self.insecure_dev}
        if self.runtime_values:
            kwargs["runtime_values"] = dict(self.runtime_values)
        if self.lower is not None:
            # Pass the already-verified contracts, not the root: the episode
            # must not re-resolve a binding the console has already checked.
            kwargs["lower_contracts"] = self.lower
        if llm_manager is not None:
            kwargs["llm_manager"] = llm_manager
        if now is not None:
            kwargs["now"] = now
        return kwargs


#: Behaviour-bearing policy values a deployment must state.  The contract
#: forbids defaulting any of them, so an incomplete profile is refused with the
#: missing names rather than silently completed.
REQUIRED_POLICY_CONTEXT_KEYS: tuple = (
    "ueId", "allowedCells", "forbiddenCells", "objectiveKind",
    "minSecondsBetweenActuations", "requiredKpiFreshnessMs", "actionDeadlineMs",
    "notBefore", "expiresAt", "rollbackOn", "rollbackTimeoutMs",
    "intentRevision", "policyRevision", "correlationId", "producerId",
)


__all__ = ["IntegrationError", "LiveIntegration", "REQUIRED_POLICY_CONTEXT_KEYS",
           "RUNTIME_ENDPOINT_KEYS", "endpoints_from_deployment_vector",
           "run_gui_once"]
