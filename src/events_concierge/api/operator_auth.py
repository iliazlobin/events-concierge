"""Independent operator identity: verified Google IAP assertion plus explicit subject roles.

IAP's unsigned email/ID headers, consumer sessions, and request-body actors confer no authority.
The assertion is verified again by the API after the same-origin web proxy forwards it.
https://cloud.google.com/iap/docs/signed-headers-howto
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from fastapi import HTTPException, Request
from jwt import PyJWK, PyJWKClient, PyJWTError, decode, get_unverified_header

from ..adapters.oidc.auth import _BoundedJwksResolver

OperatorRole = Literal["viewer", "operator", "reviewer"]
IAP_ISSUER = "https://cloud.google.com/iap"
IAP_JWKS_URL = "https://www.gstatic.com/iap/verify/public_key-jwk"
IAP_HEADER = "x-goog-iap-jwt-assertion"
_MAX_ASSERTION_BYTES = 8192
_MAX_SUBJECT_LENGTH = 200
_MAX_ASSERTION_LIFETIME_SECONDS = 3600
_NO_STORE = {"Cache-Control": "no-store, max-age=0"}
_ROLE_CAPABILITIES: dict[OperatorRole, frozenset[str]] = {
    "viewer": frozenset({"ingestion.read"}),
    "operator": frozenset({"ingestion.read", "ingestion.refresh", "ingestion.sources.enable"}),
    "reviewer": frozenset(
        {
            "ingestion.read",
            "ingestion.refresh",
            "ingestion.sources.enable",
            "ingestion.sources.configure",
        }
    ),
}


@dataclass(frozen=True, slots=True)
class OperatorPrincipal:
    subject: str
    role: OperatorRole

    @property
    def actor(self) -> str:
        return f"iap:{self.subject}"

    @property
    def capabilities(self) -> frozenset[str]:
        return _ROLE_CAPABILITIES[self.role]


class IapOperatorIdentityVerifier:
    """ES256-only, fixed issuer/keys/audience, bounded key rotation and subject allowlist."""

    def __init__(
        self,
        *,
        audience: str,
        subject_roles: Mapping[str, OperatorRole],
        key_resolver: Callable[[str], Awaitable[PyJWK]] | None = None,
    ) -> None:
        if not audience or not subject_roles:
            raise ValueError("operator identity requires audience and subject roles")
        self._audience = audience
        self._roles = dict(subject_roles)
        self._key_resolver = (
            key_resolver
            or _BoundedJwksResolver(
                PyJWKClient(IAP_JWKS_URL, timeout=5, cache_jwk_set=True)
            ).resolve
        )

    async def verify(self, token: str) -> OperatorPrincipal:
        """Return identity only after every signature, timestamp and authorization check."""
        if not token or len(token.encode("utf-8")) > _MAX_ASSERTION_BYTES:
            raise _unauthenticated()
        try:
            header = get_unverified_header(token)
            if header.get("alg") != "ES256" or header.get("crit"):
                raise _unauthenticated()
            key = await self._key_resolver(token)
            claims = decode(
                token,
                key.key,
                algorithms=["ES256"],
                audience=self._audience,
                issuer=IAP_ISSUER,
                leeway=30,
                options={"require": ["iss", "aud", "exp", "iat", "sub"], "strict_aud": True},
            )
            subject = claims["sub"]
            issued, expires = claims["iat"], claims["exp"]
            if (
                not isinstance(subject, str)
                or not subject
                or len(subject) > _MAX_SUBJECT_LENGTH
                or not subject.isprintable()
                or any(char.isspace() for char in subject)
                or type(issued) is not int
                or type(expires) is not int
                or not 0 < expires - issued <= _MAX_ASSERTION_LIFETIME_SECONDS
            ):
                raise _unauthenticated()
        except (PyJWTError, ValueError, TypeError, KeyError, OSError) as error:
            raise _unauthenticated() from error
        role = self._roles.get(subject)
        if role is None:
            raise HTTPException(403, "operator access is not assigned", headers=_NO_STORE)
        return OperatorPrincipal(subject, role)


def _unauthenticated() -> HTTPException:
    return HTTPException(401, "verified operator identity required", headers=_NO_STORE)


def required_capability(request: Request) -> str:
    """Default deny unknown writes even if a new route is later installed."""
    if request.method in {"GET", "HEAD"}:
        return "ingestion.read"
    path = request.url.path
    if request.method == "POST" and path == "/admin/v1/ingestion/commands":
        return "ingestion.refresh"
    if request.method == "PATCH" and path == "/admin/v1/ingestion/sources/bulk/enabled":
        return "ingestion.sources.enable"
    if request.method == "PATCH" and re.fullmatch(
        r"/admin/v1/ingestion/sources/[a-z0-9][a-z0-9-]{1,79}", path
    ):
        return "ingestion.sources.configure"
    raise HTTPException(403, "operator operation is not authorized", headers=_NO_STORE)


async def authorize_operator(request: Request) -> OperatorPrincipal:
    """Authenticate on every request; a cached principal only lives in this request object."""
    principal = getattr(request.state, "operator_principal", None)
    if isinstance(principal, OperatorPrincipal):
        return principal
    assertions = request.headers.getlist(IAP_HEADER)
    if len(assertions) != 1:
        raise _unauthenticated()
    verifier: IapOperatorIdentityVerifier = request.app.state.operator_identity_verifier
    principal = await verifier.verify(assertions[0])
    if required_capability(request) not in principal.capabilities:
        raise HTTPException(403, "operator capability is not assigned", headers=_NO_STORE)
    request.state.operator_principal = principal
    return principal


def verify_operator_mutation_origin(request: Request) -> None:
    """Require the fixed browser origin and JSON; forwarded Host is never CSRF authority."""
    origins = request.headers.getlist("origin")
    expected = request.app.state.settings.operator_public_origin
    if len(origins) != 1 or origins[0] != expected:
        raise HTTPException(403, "same-origin operator request required", headers=_NO_STORE)
    if request.headers.get("content-type", "").partition(";")[0].strip() != "application/json":
        raise HTTPException(415, "operator mutations require JSON", headers=_NO_STORE)
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site != "same-origin":
        raise HTTPException(403, "same-origin operator request required", headers=_NO_STORE)
