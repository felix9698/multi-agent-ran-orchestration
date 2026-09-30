"""Create, confirm durable and delete the FileDataReportingMnS subscription.

``o1-netconf-yang-profile.1.0.0.json#/lifecycle`` ordinal 5 is the only step in
the PerfMetricJob lifecycle that is not a NETCONF RPC, which is why it carries
an ``externalPrecondition`` instead of a ``requestFixture``:

    FileDataReportingMnS subscription returned 201 and its identifier is
    durably persisted

The Provider enforces that ordering itself.  Its sysrepo agent is started with
``--subscription-ready /var/lib/oran-aic-o1/subscription.ready`` and refuses the
``administrativeState`` unlock while that witness reads ``0``.  So the missing
subscription is not a formality this tool satisfies on paper -- it is a
fail-closed gate in the managed element, and the only way through it is a real
201 from the real MnS.

Nothing here composes its own idea of what a subscription is.  The collection
URI, the wire field, the success statuses and the "consumer creates before job
unlock" ordering are read from
``oran-aic-o1-pa-file.1.0.0.json#/delivery/subscription``, and the request is
issued by the release's own :class:`FileDataReportingSubscription`, the same
class the O1 harness uses.  This module supplies only the three things a
CLI has that a harness gets from its gate: the flat integration values, the
mTLS material, and a client-credentials Authorization header.

The subscription identifier is persisted outside the repository, next to the
session's other durable state, because a durable identifier that lives in a
worktree is not durable -- ``confirm_durable()`` reads it back off the
filesystem rather than trusting the value still in memory.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from oran.rapp.headless import load_integration_values
from oran.release.lo1.o1_netconf import FileDataReportingSubscription
from oran.release.lo1.security import OAuthClientCredentials, client_context
from oran.release.lo1.tls import SecretResolver

__all__ = [
    "DEFAULT_STATE_PATH",
    "build_subscription",
    "create_subscription",
    "confirm_subscription",
    "delete_subscription",
    "main",
]

DEFAULT_VALUES = (
    "/opt/ran-lab/controller/oran-deploy/session-20260819/deployment/integration-values.json"
)

#: The durable store for the identifier.  The session state directory already
#: holds the deployment's other cross-restart state and is not part of any
#: worktree, so an identifier written here survives a checkout the way the
#: contract's ``consumerPersistsSubscriptionId`` means it to.
DEFAULT_STATE_PATH = Path(
    "/opt/ran-lab/controller/oran-deploy/session-20260819/state/o1-subscription.json"
)

PA_FILE_PROFILE = Path(
    "contracts/oran-aic/1.0.1/shared-contract-bundle/oran-aic-o1-pa-file.1.0.0.json"
)

#: Flat integration-values keys -> the nested shape the frozen subscription
#: reads.  The mapping is spelled out rather than inferred so a renamed value
#: raises a KeyError here instead of producing a subscription against a default.
_TLS_KEYS = {
    "truststoreRef": "o1.https.truststoreRef",
    "clientCertificateRef": "o1.https.consumerClientCertificateRef",
    "clientPrivateKeyRef": "o1.https.consumerClientPrivateKeyRef",
}
_OAUTH_KEYS = {
    "tokenEndpoint": "o1.https.consumerOauthTokenEndpoint",
    "clientId": "o1.https.consumerOauthClientId",
    "credentialRef": "o1.https.consumerOauthCredentialRef",
    "audience": "o1.https.consumerOauthAudience",
    "scope": "o1.https.consumerOauthScope",
}

#: One integration fact, recorded here rather than guessed at call time.
#:
#: ``o1.https.consumerOauthCredentialRef`` names
#: ``secrets/o1/inbound-jwt-hmac.secret``.  That file is the *verifier's*
#: signing anchor -- the O1 Provider reads it to validate a token -- and it is
#: owned by the provider's uid 999 at mode 0400 precisely so that no consumer
#: can read it.  The integration values were frozen at 01:47 on 2026-08-19; the
#: session's OAuth authority was introduced at 14:05 the same day and registers
#: this client with a Basic-auth secret of its own
#: (``tools/oauth_authority.py#/CLIENTS/smo-o1-consumer/secretFile``).  So the
#: recorded reference names the right trust anchor for the wrong side of the
#: exchange, and the client-credentials POST needs the client's own secret.
#:
#: This is a substitution of one declared reference for another declared
#: reference, not a downgrade: the audience, scope, client identity, mTLS pair
#: and trust anchor are all still the deployment's, and the authority still
#: binds the minted token to this client's certificate with ``cnf/x5t#S256``.
#: If the file is absent the tool fails closed rather than falling back.
CLIENT_SECRET_OVERRIDE = {
    "smo-o1-consumer": "file:///opt/ran-lab/controller/oran-deploy/session-20260819"
                       "/secrets/oauth/smo-o1-consumer-client.secret",
}


def _profile() -> Mapping[str, Any]:
    """The frozen PA-file profile, read rather than restated."""
    return json.loads(PA_FILE_PROFILE.read_text(encoding="utf-8"))


def _resolver(*profiles: Mapping[str, str]) -> SecretResolver:
    """An identity secret map over the references the values already carry.

    The deployment records its material as ``file://`` references to absolute
    paths, which is one of the resolver's declared vocabularies; mapping each
    reference to its own path keeps the release's refusal behaviour intact (an
    unknown reference is still a refusal) without inventing a second map.
    """
    mapping: dict[str, str] = {}
    for profile in profiles:
        for reference in profile.values():
            text = str(reference)
            if text.startswith("file://"):
                mapping[text] = text[len("file://"):]
    return SecretResolver(mapping)


def build_subscription(values: Mapping[str, Any], *,
                       state_path: Path = DEFAULT_STATE_PATH,
                       timeout: float = 30.0) -> FileDataReportingSubscription:
    tls = {name: str(values[key]) for name, key in _TLS_KEYS.items()}
    oauth = {name: str(values[key]) for name, key in _OAUTH_KEYS.items()}
    oauth.update(tls)  # the token endpoint is mTLS-protected by the same pair
    override = CLIENT_SECRET_OVERRIDE.get(oauth["clientId"])
    if override is not None:
        oauth["credentialRef"] = override
    resolver = _resolver(tls, oauth)
    vector = {
        "o1": {
            "fileDataReporting": {
                "mnsRoot": str(values["o1.fileDataReporting.mnsRoot"]),
                "mnsVersion": str(values["o1.fileDataReporting.mnsVersion"]),
                "consumerReference": str(
                    values["o1.fileDataReporting.consumerReference"]),
            }
        }
    }
    credentials = OAuthClientCredentials(oauth, resolver, timeout=timeout)
    return FileDataReportingSubscription(
        vector=vector,
        profile=_profile(),
        state_path=Path(state_path),
        ssl_context=client_context(tls, resolver),
        timeout=timeout,
        oauth_header_provider=credentials.authorization_header,
    )


def create_subscription(values: Mapping[str, Any], *,
                        state_path: Path = DEFAULT_STATE_PATH) -> dict[str, Any]:
    """Ordinal 5: subscribe, persist the identifier, then read it back.

    ``confirm_durable`` is not a second opinion about the create -- it is the
    contract's own precondition for the unlock, and it is answered from the
    filesystem so that a process that dies between the 201 and the fsync
    reports a failure rather than an unlock it has no right to.
    """
    subscription = build_subscription(values, state_path=state_path)
    created = subscription.create()
    confirmed = subscription.confirm_durable()
    return {
        "action": "create",
        "collectionUri": subscription.collection_uri,
        "consumerReference": subscription.consumer_reference,
        "createdStatus": created["status"],
        "subscriptionId": created["subscriptionId"],
        "durable": bool(confirmed["durable"]),
        "durableStatePath": str(state_path),
        "readbackSubscriptionId": confirmed["subscriptionId"],
    }


def confirm_subscription(values: Mapping[str, Any], *,
                         state_path: Path = DEFAULT_STATE_PATH) -> dict[str, Any]:
    """Read the persisted identifier back without touching the network."""
    subscription = build_subscription(values, state_path=state_path)
    confirmed = subscription.confirm_durable()
    return {
        "action": "confirm",
        "durable": bool(confirmed["durable"]),
        "durableStatePath": str(state_path),
        "subscriptionId": confirmed["subscriptionId"],
    }


def delete_subscription(values: Mapping[str, Any], *,
                        state_path: Path = DEFAULT_STATE_PATH) -> dict[str, Any]:
    subscription = build_subscription(values, state_path=state_path)
    subscription.confirm_durable()
    outcome = subscription.delete()
    return {"action": "delete", **{str(k): v for k, v in outcome.items()}}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--integration-values", default=DEFAULT_VALUES)
    parser.add_argument("--state-path", default=str(DEFAULT_STATE_PATH))
    parser.add_argument("--create", action="store_true",
                        help="POST the subscription and persist its identifier")
    parser.add_argument("--delete", action="store_true",
                        help="DELETE the subscription and confirm its absence")
    args = parser.parse_args(argv)
    if args.create and args.delete:
        json.dump({"refused": "--create and --delete are exclusive"},
                  sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 2
    values = load_integration_values(args.integration_values)
    state_path = Path(args.state_path)
    try:
        if args.create:
            result: Mapping[str, Any] = create_subscription(
                values, state_path=state_path)
        elif args.delete:
            result = delete_subscription(values, state_path=state_path)
        else:
            result = confirm_subscription(values, state_path=state_path)
    except Exception as exc:  # noqa: BLE001 - the operator needs the reason
        import traceback

        json.dump({"error": type(exc).__name__, "detail": str(exc),
                   "traceback": traceback.format_exc().splitlines()[-6:]},
                  sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 3
    json.dump(dict(result), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
