"""Runnable Non-RT RIC framework process.

The deployment's integration-values document is the only source for R1 and
A1 endpoints.  The command intentionally has no production endpoint defaults:
operators must provide the pinned values/artifacts generated for the release.
"""

from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.request import Request, urlopen

from .a1_client import A1PClient, A1Transport
from .capability import CapabilityArtifacts, CapabilityError
from .config import SecurityProfile, load, require_https_or_loopback
from .http_server import NonRtHttpServer
from .service import POLICY_TYPE_ID, NonRtRicService
from oran.conformance.contracts import ContractBundle


def _load_callable(reference: str) -> Callable[..., Any]:
    """Load an explicit deployment hook in ``module:attribute`` form."""
    module_name, separator, attribute = reference.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("hook must use module:attribute form")
    hook = getattr(importlib.import_module(module_name), attribute)
    if not callable(hook):
        raise ValueError("configured hook is not callable")
    return hook


def _callback_sender(insecure_dev_mode: bool) -> Callable[[str, dict[str, Any]], int]:
    def send(destination: str, body: dict[str, Any]) -> int:
        require_https_or_loopback(destination, insecure_dev_mode)
        request = Request(
            destination,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=10.0) as response:  # noqa: S310 - URI is validated above
            return response.status

    return send


def _a1_discovery(client: A1PClient) -> Mapping[str, Any]:
    """Return the authoritative per-policy-type discovery object.

    A1-P deployments expose the ``PolicyTypeObject`` at the standard endpoint,
    and the frozen contract defines it as carrying the policy and status
    schemas - not a RIC identity.  The identifier is the resource this request
    named, so it is recorded here rather than expected in the body; the schemas
    themselves are what :meth:`CapabilityArtifacts.assert_a1_discovery` binds to
    the pinned capability manifest, and a partial response fails there.
    """
    discovered = client.get_policy_type(POLICY_TYPE_ID)
    if not isinstance(discovered, Mapping):
        raise CapabilityError("A1 policy-type discovery response must be an object")
    result = dict(discovered)
    result.setdefault("policyTypeId", POLICY_TYPE_ID)
    return result


def create_service_from_values(
    values: Mapping[str, Any],
    *,
    bundle_dir: str | Path,
    database_path: str | Path,
    artifact_base_dir: str | Path | None = None,
    listen_host: str,
    insecure_dev_mode: bool,
    auth_hook: Callable[[Mapping[str, str]], str] | None = None,
    capability_registration: Mapping[str, Any] | None = None,
    a1_client: A1PClient | None = None,
    callback_sender: Callable[[str, dict[str, Any]], int] | None = None,
) -> NonRtRicService:
    """Build a service from validated integration values and pinned artifacts."""
    client = a1_client or A1PClient(
        A1Transport(values["a1.apiRoot"], insecure_dev_mode=insecure_dev_mode)
    )
    artifacts = CapabilityArtifacts.load(
        values,
        bundle_dir=bundle_dir,
        base_dir=artifact_base_dir or Path(database_path).parent,
        a1_discovery=_a1_discovery(client),
    )
    security = SecurityProfile(
        insecure_dev_mode=insecure_dev_mode,
        listen_host=listen_host,
        auth_hook=auth_hook,
    )
    return NonRtRicService(
        database_path=database_path,
        a1_client=client,
        capability_manifest=artifacts.capability_manifest,
        a1_ready=artifacts.a1_ready,
        capability_artifacts=artifacts,
        bundle_dir=bundle_dir,
        security=security,
        r1_api_root=values["r1.apiRoot"],
        a1_notification_destination=values["a1.notificationDestination"],
        bootstrap_info={"bootstrapInformation": []},
        callback_sender=callback_sender or _callback_sender(insecure_dev_mode),
        capability_registration=capability_registration,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the O-RAN Non-RT RIC framework")
    parser.add_argument("--integration-values", required=True, type=Path)
    authority = parser.add_mutually_exclusive_group()
    authority.add_argument(
        "--contract-authority", type=Path,
        help="authority tree containing shared-contract-bundle (defaults to 1.0.1)")
    authority.add_argument(
        "--contract-bundle", type=Path,
        help="historical compatibility alias accepting a bundle directory")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", default=18080, type=int)
    parser.add_argument("--insecure-dev-mode", action="store_true")
    parser.add_argument("--auth-hook", help="production authentication hook (module:attribute)")
    parser.add_argument("--capability-registration", type=Path)
    args = parser.parse_args(argv)

    bundle = ContractBundle.discover(
        args.contract_authority or args.contract_bundle)
    values = load(args.integration_values, bundle_dir=bundle.path)
    auth_hook = _load_callable(args.auth_hook) if args.auth_hook else None
    registration = (
        json.loads(args.capability_registration.read_text(encoding="utf-8"))
        if args.capability_registration else None
    )
    service = create_service_from_values(
        values,
        bundle_dir=bundle.path,
        database_path=args.database,
        artifact_base_dir=args.integration_values.parent,
        listen_host=args.listen_host,
        insecure_dev_mode=args.insecure_dev_mode,
        auth_hook=auth_hook,
        capability_registration=registration,
    )
    server = NonRtHttpServer((args.listen_host, args.listen_port), service)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
