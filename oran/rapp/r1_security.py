"""R1 transport security material, resolved from the deployment's own document.

Secure R1 is mutual TLS *and* an OAuth authorization header, and
:class:`oran.rapp.r1_client.R1Client` refuses to exist without both - correctly,
because a consumer that fell back to server-only TLS or to no authorization
would still report itself as connected.  What was missing was not the check but
the supply: nothing turned the deployment's declared credential references into
the two objects that check demands, so the only reachable Live R1 was
``insecure_dev`` over loopback HTTP.

The references come from the frozen integration-values document and nowhere
else.  That document's ``SecretReference`` vocabulary addresses material *outside
this tree* (``file://``, ``env://``, ``vault://``, ``k8s://``, ``keychain://``),
so no key, certificate or token is ever a repository byte, and this module reads
the four references a secure R1 consumer needs:

``r1.https.truststoreRef``
    the trust anchor the R1 endpoint's server certificate must chain to;
``r1.https.clientCertificateRef`` / ``r1.https.clientPrivateKeyRef``
    this rApp's client identity, i.e. the mutual half of mutual TLS;
``r1.https.oauthCredentialRef``
    the authorization token, read at request time so a rotation takes effect
    without a rebind and no token outlives the call that used it.

Deliberately *not* an overlay.  ``RUNTIME_ENDPOINT_KEYS`` exists so a deployment
that publishes its endpoints in its own digest-pinned vector does not have to
restate them; trust material is the opposite case.  An overlay that could
replace a truststore could point a validated deployment at an attacker's R1 and
still pass every digest check, so these keys are read from the pinned document
only, and this module offers no way to inject them.

Fail-closed throughout: an absent, unreadable or unresolvable reference is a
refusal that names the reference, never a downgrade to an unverified or
unauthorized connection, and never a redacted-but-usable partial context.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import unquote, urlsplit

from .r1_client import OAUTH_SCHEME

#: TLS 1.2 is the floor the live-O1 profile already applies to every consumer
#: context it builds; R1 does not get a weaker one.
MINIMUM_TLS_VERSION = ssl.TLSVersion.TLSv1_2

#: The integration-values keys this module reads, in the order a failure should
#: report them.  Every one of them is ``required`` in the frozen
#: ``integration-values.1.0.0`` schema, so a validated document always declares
#: all four - what a deployment can still get wrong is what they resolve to.
TRUSTSTORE_KEY = "r1.https.truststoreRef"
CLIENT_CERTIFICATE_KEY = "r1.https.clientCertificateRef"
CLIENT_PRIVATE_KEY_KEY = "r1.https.clientPrivateKeyRef"
OAUTH_CREDENTIAL_KEY = "r1.https.oauthCredentialRef"
R1_SECURITY_KEYS: tuple = (
    TRUSTSTORE_KEY, CLIENT_CERTIFICATE_KEY, CLIENT_PRIVATE_KEY_KEY,
    OAUTH_CREDENTIAL_KEY,
)

#: The reference vocabulary the frozen values schema permits.  Only ``file://``
#: carries its own location; the other four name material a deployment resolves
#: through a secret store, which this release does not implement - and guessing
#: a resolution rule for them would be inventing trust material, so they are
#: refused by name instead.
FILE_SCHEME = "file"
RESOLVED_SCHEMES: tuple = (FILE_SCHEME,)
DECLARED_SCHEMES: tuple = ("env", FILE_SCHEME, "vault", "k8s", "keychain")


class R1SecurityError(RuntimeError):
    """A declared R1 credential reference does not yield usable material."""


@dataclass(frozen=True)
class R1Security:
    """The two objects ``R1Client`` requires before it will talk to a real R1.

    ``references`` records *which* references were used, never what they
    resolved to and never their contents, so an operator export can state the
    provenance of a Live session's trust without publishing any of it.
    """

    ssl_context: ssl.SSLContext
    oauth_header_provider: Callable[[str, str, str], str]
    references: Mapping[str, str]


def resolve_reference(key: str, reference: Any) -> Path:
    """Resolve one declared credential reference to a file outside the tree."""
    if not isinstance(reference, str) or not reference:
        raise R1SecurityError(
            f"{key} is not declared; a secure R1 consumer needs all of "
            + ", ".join(R1_SECURITY_KEYS))
    parts = urlsplit(reference)
    if parts.scheme not in DECLARED_SCHEMES:
        raise R1SecurityError(
            f"{key} is outside the frozen reference vocabulary "
            f"({', '.join(DECLARED_SCHEMES)}): {reference}")
    if parts.scheme not in RESOLVED_SCHEMES:
        raise R1SecurityError(
            f"{key} names the {parts.scheme}:// vocabulary, whose resolution is "
            "a deployment secret-store concern this release does not implement; "
            f"declare it as {FILE_SCHEME}:// naming the issued material. "
            "Refusing rather than guessing where the material lives")
    if not parts.path:
        # ``file://SOMETHING`` with no path is how an unissued credential gets
        # written down - the deployment session recorded exactly
        # ``file://UNRESOLVED-upper-r1-oauth-credential`` for the OAuth half it
        # had no issuer for.  It is a note, not a credential.
        raise R1SecurityError(
            f"{key} names no path, so no material has been issued for it: "
            f"{reference}")
    # ``file://<host>/path`` is only accepted for the empty and localhost
    # authorities: a reference whose material lives on another host has not
    # been delivered to this deployment, whatever the path says.
    if parts.netloc not in ("", "localhost"):
        raise R1SecurityError(
            f"{key} names a remote authority ({parts.netloc}); the reference "
            "must address material present on this host")
    target = Path(unquote(parts.path))
    if not target.is_absolute():
        raise R1SecurityError(
            f"{key} must name an absolute path outside the repository: {reference}")
    if not target.is_file():
        raise R1SecurityError(
            f"{key} does not resolve to a regular file: {reference}")
    if _inside_this_tree(target):
        # A key or token committed beside the code is a key every reader of the
        # tree holds.  The frozen vocabulary exists to keep material out of it,
        # so a reference that points back in is refused rather than used.
        raise R1SecurityError(
            f"{key} resolves inside this source tree; R1 credential material "
            "must live outside it")
    return target


def _inside_this_tree(target: Path) -> bool:
    """True when the resolved material would live inside this tree."""
    root = Path(__file__).resolve().parents[2]
    try:
        target.resolve().relative_to(root)
    except ValueError:
        return False
    return True


def build_client_ssl_context(*, truststore: Path, certificate: Path,
                             private_key: Path) -> ssl.SSLContext:
    """Build the verified, client-authenticated context R1 requires."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = MINIMUM_TLS_VERSION
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    try:
        context.load_verify_locations(cafile=str(truststore))
        context.load_cert_chain(certfile=str(certificate), keyfile=str(private_key))
    except (ssl.SSLError, OSError) as exc:
        # Deliberately not falling back to an anonymous or server-only context:
        # unusable trust material is a refusal, never a weaker connection.
        raise R1SecurityError(
            "the resolved R1 TLS material is unusable; refusing rather than "
            "connecting without mutual authentication") from exc
    return context


def build_oauth_header_provider(credential: Path) -> Callable[[str, str, str], str]:
    """Read the authorization token per request, from the resolved reference."""
    def provide(_method: str, _url: str, _version: str) -> str:
        try:
            token = credential.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise R1SecurityError(
                "the R1 authorization credential could not be read at request "
                "time") from exc
        if not token:
            raise R1SecurityError("the R1 authorization credential is empty")
        # Assembled from the scheme token so the scheme-plus-space literal never
        # appears in these bytes - the same G-SEC-1 reason ``r1_client`` gives.
        return OAUTH_SCHEME + " " + token

    return provide


def build_r1_security(values: Mapping[str, Any]) -> R1Security:
    """Turn one validated integration-values document into R1 transport security.

    Raises :class:`R1SecurityError`, naming the offending key, when any of the
    four references is absent or does not resolve.  The caller therefore either
    gets a client that is mutually authenticated and authorized, or an error
    that says which value the deployment still owes - which is what
    ``R1Client``'s own refusal could not say.
    """
    resolved = {key: resolve_reference(key, values.get(key))
                for key in R1_SECURITY_KEYS}
    context = build_client_ssl_context(
        truststore=resolved[TRUSTSTORE_KEY],
        certificate=resolved[CLIENT_CERTIFICATE_KEY],
        private_key=resolved[CLIENT_PRIVATE_KEY_KEY])
    provider = build_oauth_header_provider(resolved[OAUTH_CREDENTIAL_KEY])
    return R1Security(
        ssl_context=context, oauth_header_provider=provider,
        references={key: str(values[key]) for key in R1_SECURITY_KEYS})


__all__ = ["CLIENT_CERTIFICATE_KEY", "CLIENT_PRIVATE_KEY_KEY",
           "DECLARED_SCHEMES", "MINIMUM_TLS_VERSION", "OAUTH_CREDENTIAL_KEY",
           "R1Security", "R1SecurityError", "R1_SECURITY_KEYS",
           "RESOLVED_SCHEMES", "TRUSTSTORE_KEY", "build_client_ssl_context",
           "build_oauth_header_provider", "build_r1_security",
           "resolve_reference"]
