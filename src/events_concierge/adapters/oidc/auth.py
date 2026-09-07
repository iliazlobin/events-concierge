"""Strict OIDC/JWKS request authentication (FR-1.1/1.3, AC-1/AC-2).

The adapter accepts a tenant identifier only as a signed claim from one configured issuer.  It
never trusts a tenant header, token-selected algorithm, token-selected key endpoint, or request
body field.  JWKS retrieval is performed by PyJWT against the deployment-configured HTTPS URL and
is moved off the event loop.
"""

from __future__ import annotations

import asyncio
import hmac
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from time import monotonic
from unicodedata import category
from urllib.parse import urlsplit
from uuid import UUID

from jwt import PyJWK, PyJWKClient, PyJWTError, decode, get_unverified_header
from jwt.exceptions import PyJWKClientError

from ...ports.auth import AuthenticationFailedError

_AUTHORIZATION_HEADER = "authorization"
_MAX_TOKEN_BYTES = 8192
_MAX_AUDIENCE_LENGTH = 512
_MAX_CLAIM_NAME_LENGTH = 256
_MAX_URL_LENGTH = 2048
_MAX_URL_PORT = 65535
_MAX_KEY_ID_BYTES = 256
_MAX_SUBJECT_BYTES = 1024
_MAX_NONCE_BYTES = 256
_MAX_LEEWAY_SECONDS = 300
_MAX_JWKS_TIMEOUT_SECONDS = 30
_JWKS_CACHE_LIFETIME_SECONDS = 300.0
_JWKS_REFRESH_COOLDOWN_SECONDS = 30.0
_NEGATIVE_KEY_CACHE_SIZE = 256
_ALLOWED_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"})


@dataclass(frozen=True, slots=True)
class OidcIdentity:
    """Canonical identity claims returned only after complete signature/claim verification."""

    tenant_id: UUID
    subject: str
    authenticated_at: int | None = None


class _BoundedJwksResolver:
    """Resolve keys without letting arbitrary ``kid`` values become an HTTPS oracle.

    PyJWKClient refreshes its JWKS once for every cache miss.  A global refresh cooldown bounds
    outbound traffic even when every request uses a unique attacker-controlled key ID.  The async
    lock also makes an initial fetch or rotation refresh single-flight, while the local snapshot
    keeps ordinary known-key authentication lock-free.
    """

    def __init__(self, client: PyJWKClient) -> None:
        self._client = client
        self._refresh_lock = asyncio.Lock()
        self._keys: tuple[PyJWK, ...] = ()
        self._fetched_at: float | None = None
        self._last_refresh_at: float | None = None
        self._negative_keys: OrderedDict[str, float] = OrderedDict()

    async def resolve(self, token: str) -> PyJWK:
        key_id = _unverified_key_id(token)
        now = monotonic()
        if self._is_negative(key_id, now):
            raise PyJWKClientError("token key ID is not in the configured JWKS")

        if self._snapshot_is_fresh(now):
            signing_key = _matching_key(self._keys, key_id)
            if signing_key is not None:
                return signing_key

        async with self._refresh_lock:
            now = monotonic()
            if self._is_negative(key_id, now):
                raise PyJWKClientError("token key ID is not in the configured JWKS")

            if self._snapshot_is_fresh(now):
                signing_key = _matching_key(self._keys, key_id)
                if signing_key is not None:
                    return signing_key

            if (
                self._last_refresh_at is not None
                and now - self._last_refresh_at < _JWKS_REFRESH_COOLDOWN_SECONDS
            ):
                self._remember_negative(key_id, now)
                raise PyJWKClientError("token key ID is not in the configured JWKS")

            # Record the attempt before yielding to a worker thread. The lock supplies
            # single-flight behavior; the timestamp also rate-limits subsequent failures.
            self._last_refresh_at = now
            try:
                keys = await asyncio.to_thread(self._client.get_signing_keys, refresh=True)
            finally:
                self._last_refresh_at = monotonic()

            self._keys = tuple(keys)
            self._fetched_at = monotonic()
            # A successful provider refresh may make any prior negative key valid.
            self._negative_keys.clear()
            signing_key = _matching_key(self._keys, key_id)
            if signing_key is None:
                self._remember_negative(key_id, self._fetched_at)
                raise PyJWKClientError("token key ID is not in the configured JWKS")
            return signing_key

    def _snapshot_is_fresh(self, now: float) -> bool:
        return (
            self._fetched_at is not None and now - self._fetched_at < _JWKS_CACHE_LIFETIME_SECONDS
        )

    def _is_negative(self, key_id: str, now: float) -> bool:
        expires_at = self._negative_keys.get(key_id)
        if expires_at is None:
            return False
        if expires_at <= now:
            del self._negative_keys[key_id]
            return False
        self._negative_keys.move_to_end(key_id)
        return True

    def _remember_negative(self, key_id: str, now: float) -> None:
        self._negative_keys[key_id] = now + _JWKS_REFRESH_COOLDOWN_SECONDS
        self._negative_keys.move_to_end(key_id)
        while len(self._negative_keys) > _NEGATIVE_KEY_CACHE_SIZE:
            self._negative_keys.popitem(last=False)


class OidcJwtAuthContext:
    """Resolve a canonical tenant UUID from a verified OIDC JWT.

    ``algorithms`` is deployment configuration and is deliberately restricted to asymmetric
    algorithms.  It is never inferred from the untrusted JWT header.  ``tenant_claim`` should be a
    collision-resistant private claim name for a multi-tenant identity provider configuration.
    """

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str,
        tenant_claim: str,
        algorithms: tuple[str, ...] = ("RS256",),
        leeway_seconds: int = 30,
        jwks_timeout_seconds: float = 5.0,
        signing_key_resolver: Callable[[str], PyJWK] | None = None,
    ) -> None:
        # OIDC issuer comparison is exact; a configured trailing slash is significant.
        self._issuer = _validated_https_url(issuer, label="issuer", allow_query=False)
        # A provider-owned JWKS endpoint may use a fixed version/routing query. It remains safe
        # because this deployment value is never selected by the bearer token.
        self._jwks_url = _validated_https_url(jwks_url, label="JWKS URL", allow_query=True)
        if not audience.strip() or len(audience) > _MAX_AUDIENCE_LENGTH:
            raise ValueError("OIDC audience must be a non-empty bounded value")
        if not tenant_claim.strip() or len(tenant_claim) > _MAX_CLAIM_NAME_LENGTH:
            raise ValueError("OIDC tenant claim must be a non-empty bounded value")
        if (
            not algorithms
            or len(set(algorithms)) != len(algorithms)
            or not set(algorithms) <= _ALLOWED_ALGORITHMS
        ):
            raise ValueError("OIDC algorithms must be a unique asymmetric allowlist")
        if not 0 <= leeway_seconds <= _MAX_LEEWAY_SECONDS:
            raise ValueError("OIDC clock leeway must be between 0 and 300 seconds")
        if not 0 < jwks_timeout_seconds <= _MAX_JWKS_TIMEOUT_SECONDS:
            raise ValueError("OIDC JWKS timeout must be between 0 and 30 seconds")

        self._audience = audience
        self._tenant_claim = tenant_claim
        self._algorithms = algorithms
        self._leeway_seconds = leeway_seconds
        self._resolve_signing_key: Callable[[str], Awaitable[PyJWK]]
        if signing_key_resolver is None:
            jwks_client = PyJWKClient(
                self._jwks_url,
                cache_keys=False,
                cache_jwk_set=True,
                lifespan=300,
                timeout=jwks_timeout_seconds,
            )
            bounded_resolver = _BoundedJwksResolver(jwks_client)
            self._resolve_signing_key = bounded_resolver.resolve
        else:

            async def resolve_signing_key(token: str) -> PyJWK:
                return await asyncio.to_thread(signing_key_resolver, token)

            self._resolve_signing_key = resolve_signing_key

    async def resolve_tenant_id(self, headers: Mapping[str, str]) -> UUID:
        """Verify the bearer token and return its canonical, issuer-owned tenant claim."""
        token = _bearer_token(headers)
        identity = await self.verify_identity_token(token)
        return identity.tenant_id

    async def verify_identity_token(
        self,
        token: str,
        *,
        expected_nonce: str | None = None,
        require_auth_time: bool = False,
    ) -> OidcIdentity:
        """Verify an OIDC identity token and return only bounded authorization claims.

        ``auth_time`` is optional for an ordinary authorization-code login. A step-up transaction
        sets ``require_auth_time`` so the caller can prove an actual provider authentication took
        place recently instead of treating a new callback as fresh authentication.
        """
        try:
            signing_key = await self._resolve_signing_key(token)
            required_claims = [
                "iss",
                "sub",
                "aud",
                "exp",
                "iat",
                self._tenant_claim,
            ]
            if expected_nonce is not None:
                required_claims.append("nonce")
            if require_auth_time:
                required_claims.append("auth_time")
            claims = decode(
                token,
                signing_key,
                algorithms=list(self._algorithms),
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway_seconds,
                options={
                    "require": required_claims,
                },
            )
            _validate_authorized_party(claims, self._audience)
            raw_subject = claims["sub"]
            if (
                not isinstance(raw_subject, str)
                or not raw_subject
                or len(raw_subject.encode("utf-8")) > _MAX_SUBJECT_BYTES
                or any(category(character) in {"Cc", "Cf"} for character in raw_subject)
            ):
                raise ValueError("subject claim must be a bounded printable string")
            raw_tenant_id = claims[self._tenant_claim]
            if not isinstance(raw_tenant_id, str):
                raise ValueError("tenant claim must be a string")
            tenant_id = UUID(raw_tenant_id)
            if raw_tenant_id != str(tenant_id):
                raise ValueError("tenant claim must be canonical")
            if expected_nonce is not None:
                raw_nonce = claims["nonce"]
                if (
                    not isinstance(raw_nonce, str)
                    or not raw_nonce
                    or len(raw_nonce.encode("utf-8")) > _MAX_NONCE_BYTES
                    or not hmac.compare_digest(raw_nonce, expected_nonce)
                ):
                    raise ValueError("identity token nonce does not match the login transaction")
            raw_authenticated_at = claims.get("auth_time")
            if raw_authenticated_at is None:
                authenticated_at = None
            elif (
                isinstance(raw_authenticated_at, bool)
                or not isinstance(raw_authenticated_at, int)
                or raw_authenticated_at < 0
            ):
                raise ValueError("auth_time claim must be a non-negative integer NumericDate")
            else:
                authenticated_at = raw_authenticated_at
        except (KeyError, PyJWTError, TypeError, ValueError) as error:
            raise AuthenticationFailedError("valid tenant authentication is required") from error
        return OidcIdentity(
            tenant_id=tenant_id,
            subject=raw_subject,
            authenticated_at=authenticated_at,
        )


def _bearer_token(headers: Mapping[str, str]) -> str:
    """Extract exactly one ordinary Bearer credential without accepting ambiguous syntax."""
    values = [
        value
        for name, value in headers.items()
        if isinstance(name, str) and name.casefold() == _AUTHORIZATION_HEADER
    ]
    if len(values) != 1 or not isinstance(values[0], str):
        raise AuthenticationFailedError("valid tenant authentication is required")
    value = values[0]
    if not value.startswith("Bearer ") or value.count(" ") != 1 or "," in value or "\t" in value:
        raise AuthenticationFailedError("valid tenant authentication is required")
    token = value[7:]
    if not token or len(token.encode("utf-8")) > _MAX_TOKEN_BYTES:
        raise AuthenticationFailedError("valid tenant authentication is required")
    return token


def _validate_authorized_party(claims: Mapping[str, object], audience: str) -> None:
    """Apply OIDC's multi-audience/authorized-party binding in addition to JWT validation."""
    token_audience = claims.get("aud")
    authorized_party = claims.get("azp")
    if isinstance(token_audience, list) and len(token_audience) > 1:
        if authorized_party != audience:
            raise ValueError("multi-audience token is not bound to this client")
    elif authorized_party is not None and authorized_party != audience:
        raise ValueError("token authorized party does not match this client")


def _unverified_key_id(token: str) -> str:
    """Read only the bounded selector needed to choose a deployment-owned verification key."""
    key_id = get_unverified_header(token).get("kid")
    if not isinstance(key_id, str) or not key_id or len(key_id.encode("utf-8")) > _MAX_KEY_ID_BYTES:
        raise ValueError("token key ID must be a non-empty bounded string")
    return key_id


def _matching_key(keys: tuple[PyJWK, ...], key_id: str) -> PyJWK | None:
    return next((key for key in keys if key.key_id == key_id), None)


def _validated_https_url(value: str, *, label: str, allow_query: bool) -> str:
    """Allow only a fixed absolute HTTPS endpoint with no credentials or fragment."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_URL_LENGTH
        or _has_url_control_or_whitespace(value)
    ):
        raise ValueError(f"OIDC {label} must be a bounded HTTPS URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"OIDC {label} must be a bounded HTTPS URL") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or "#" in value
        or (not allow_query and "?" in value)
        or parsed.netloc.endswith(":")
        or (port is not None and not 1 <= port <= _MAX_URL_PORT)
    ):
        raise ValueError(f"OIDC {label} must be a bounded HTTPS URL")
    return value


def _has_url_control_or_whitespace(value: str) -> bool:
    return any(character.isspace() or category(character) in {"Cc", "Cf"} for character in value)
