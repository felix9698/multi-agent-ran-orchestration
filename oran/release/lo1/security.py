"""Versioned live-O1 mTLS and OAuth 2.0 authority.

The frozen deployment vector names OAuth client settings but does not provide
an authorization-server validation authority (issuer/JWKS/introspection), nor
does the historical startup document bind one.  Live operation therefore
consumes this additive, digest-pinned authority document.  Missing or
inconsistent authority is a startup refusal; bearer bytes are never treated as
self-authenticating credentials.
"""

from __future__ import annotations

import base64
import hashlib
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker

from .tls import SecretResolver, build_client_ssl_context

SECURITY_AUTHORITY_SPEC_VERSION = (
    "oran-aic-upper-live-o1-security-authority/1.0.0")
SECURITY_AUTHORITY_SCHEMA = "o1-security-authority.1.0.0.schema.json"


class SecurityAuthorityError(RuntimeError):
    """The authority or an OAuth exchange is invalid (fail closed)."""

    exit_code = 78


class OAuthAuthenticationError(SecurityAuthorityError):
    """No valid authenticated OAuth principal was established (HTTP 401)."""


class OAuthAuthorizationError(SecurityAuthorityError):
    """An authenticated principal lacks the required grant (HTTP 403)."""


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def authority_of(uri: str) -> str:
    parsed = urlsplit(str(uri))
    if parsed.scheme != "https" or not parsed.hostname or parsed.port is None:
        raise SecurityAuthorityError(
            "OAuth endpoints must be https URIs with an explicit port")
    return ("[%s]:%d" % (parsed.hostname, parsed.port)
            if ":" in parsed.hostname else "%s:%d" % (parsed.hostname, parsed.port))


def load_security_authority(path: Path, *, expected_sha256: str,
                            schema_path: Path) -> dict[str, Any]:
    target = Path(path)
    if not target.is_file():
        raise SecurityAuthorityError(
            "OAUTH_VALIDATION_AUTHORITY_UNSPECIFIED: security authority is absent")
    observed = sha256_file(target)
    if observed != str(expected_sha256):
        raise SecurityAuthorityError(
            "security authority byte digest differs from startup pin")
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
        schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecurityAuthorityError(
            "OAUTH_VALIDATION_AUTHORITY_UNSPECIFIED: authority/schema unreadable") from exc
    errors = sorted(Draft202012Validator(
        schema, format_checker=FormatChecker()).iter_errors(document),
        key=lambda item: tuple(str(part) for part in item.absolute_path))
    if errors:
        raise SecurityAuthorityError(
            "OAUTH_VALIDATION_AUTHORITY_UNSPECIFIED: %s" % errors[0].message)
    return document


def security_network_authorities(document: Mapping[str, Any]) -> tuple[str, ...]:
    oauth = document["oauth"]
    derived = {
        authority_of(str(oauth["mnsClient"]["tokenEndpoint"])),
        authority_of(str(oauth["notificationReceiver"]["tokenEndpoint"])),
        authority_of(str(oauth["notificationReceiver"]["introspectionEndpoint"])),
    }
    declared = {str(item) for item in document["networkAuthorities"]}
    if declared != derived:
        raise SecurityAuthorityError(
            "security networkAuthorities must exactly equal the OAuth endpoint authorities")
    return tuple(sorted(derived))


def security_secret_references(document: Mapping[str, Any]) -> tuple[str, ...]:
    tls = document["tls"]
    oauth = document["oauth"]
    refs = {
        tls["mnsClient"]["truststoreRef"],
        tls["mnsClient"]["clientCertificateRef"],
        tls["mnsClient"]["clientPrivateKeyRef"],
        tls["notificationReceiver"]["serverCertificateRef"],
        tls["notificationReceiver"]["serverPrivateKeyRef"],
        tls["notificationReceiver"]["clientTruststoreRef"],
    }
    for profile in (oauth["mnsClient"], oauth["notificationReceiver"]):
        refs.update((profile["credentialRef"], profile["truststoreRef"],
                     profile["clientCertificateRef"],
                     profile["clientPrivateKeyRef"]))
    return tuple(sorted(str(item) for item in refs))


def client_context(profile: Mapping[str, Any],
                   resolver: SecretResolver) -> ssl.SSLContext:
    return build_client_ssl_context(
        truststore_ref=str(profile["truststoreRef"]), resolver=resolver,
        client_certificate_ref=str(profile["clientCertificateRef"]),
        client_private_key_ref=str(profile["clientPrivateKeyRef"]))


def _client_basic(client_id: str, secret: str) -> str:
    raw = (str(client_id) + ":" + str(secret)).encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _post_form(uri: str, fields: Mapping[str, str], *, headers: Mapping[str, str],
               context: ssl.SSLContext, timeout: float,
               opener: Any = urllib.request.urlopen) -> dict[str, Any]:
    body = urllib.parse.urlencode(dict(fields)).encode("ascii")
    outgoing = {"Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json", **dict(headers)}
    request = urllib.request.Request(uri, data=body, method="POST", headers=outgoing)
    try:
        with opener(request, timeout=timeout, context=context) as response:
            if int(response.status) != 200:
                raise SecurityAuthorityError(
                    "OAuth authority returned HTTP %d" % int(response.status))
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise SecurityAuthorityError(
            "OAuth authority rejected the request with HTTP %d" % int(exc.code)) from None
    except (OSError, ssl.SSLError) as exc:
        raise SecurityAuthorityError(
            "OAuth authority exchange failed closed") from exc
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecurityAuthorityError("OAuth authority response is not JSON") from exc
    if not isinstance(document, dict):
        raise SecurityAuthorityError("OAuth authority response is not an object")
    return document


@dataclass
class OAuthClientCredentials:
    profile: Mapping[str, Any]
    resolver: SecretResolver
    timeout: float = 30.0
    opener: Any = urllib.request.urlopen

    def authorization_header(self) -> str:
        credential_material = self.resolver.resolve_path(str(
            self.profile["credentialRef"])).read_text(encoding="utf-8").strip()
        document = _post_form(
            str(self.profile["tokenEndpoint"]),
            {"grant_type": "client_credentials",
             "audience": str(self.profile["audience"]),
             "scope": str(self.profile["scope"])},
            headers={"Authorization": _client_basic(
                str(self.profile["clientId"]), credential_material)},
            context=client_context(self.profile, self.resolver),
            timeout=self.timeout, opener=self.opener)
        token = document.get("access_token")
        if not isinstance(token, str) or not token or \
                str(document.get("token_type", "")).lower() != "bearer":
            raise SecurityAuthorityError(
                "client-credentials response carries no bearer access token")
        return "Bearer " + token


@dataclass
class OAuthIntrospectionValidator:
    profile: Mapping[str, Any]
    resolver: SecretResolver
    timeout: float = 30.0
    opener: Any = urllib.request.urlopen

    def validate(self, authorization: str | None) -> Mapping[str, Any]:
        scheme, separator, token = str(authorization or "").partition(" ")
        if separator != " " or scheme.lower() != "bearer" or not token.strip():
            raise OAuthAuthenticationError("OAuth bearer token is missing")
        credential_material = self.resolver.resolve_path(str(
            self.profile["credentialRef"])).read_text(encoding="utf-8").strip()
        document = _post_form(
            str(self.profile["introspectionEndpoint"]), {"token": token.strip()},
            headers={"Authorization": _client_basic(
                str(self.profile["clientId"]), credential_material)},
            context=client_context(self.profile, self.resolver),
            timeout=self.timeout, opener=self.opener)
        if document.get("active") is not True:
            raise OAuthAuthenticationError("OAuth token is inactive")
        if str(document.get("iss", "")) != str(self.profile["issuer"]):
            raise OAuthAuthorizationError("OAuth token issuer is not authorized")
        audiences = document.get("aud", ())
        if isinstance(audiences, str):
            audiences = [audiences]
        if str(self.profile["audience"]) not in set(str(item) for item in audiences):
            raise OAuthAuthorizationError("OAuth token audience is not authorized")
        scopes = set(str(document.get("scope", "")).split())
        required = set(str(self.profile["scope"]).split())
        if not required or not required <= scopes:
            raise OAuthAuthorizationError("OAuth token scope is not authorized")
        if str(document.get("client_id", "")) not in {
                str(item) for item in self.profile["authorizedClientIds"]}:
            raise OAuthAuthorizationError("OAuth client identity is not authorized")
        try:
            expires = float(document["exp"])
        except (KeyError, TypeError, ValueError):
            raise OAuthAuthenticationError(
                "OAuth introspection lacks token expiry") from None
        if expires <= time.time():
            raise OAuthAuthenticationError("OAuth token is expired")
        return document


def certificate_sha256(der_certificate: bytes | None) -> str | None:
    return hashlib.sha256(der_certificate).hexdigest() if der_certificate else None


def allowed_tls_roles(document: Mapping[str, Any]) -> dict[str, str]:
    entries = document["tls"]["notificationReceiver"]["allowedClientIdentities"]
    return {str(item["certificateSha256"]): str(item["role"]) for item in entries}
