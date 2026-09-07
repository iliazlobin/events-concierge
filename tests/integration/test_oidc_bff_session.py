"""Shared Redis identity-session behavior across independently composed API replicas."""

from __future__ import annotations

import hashlib
import os
from uuid import uuid4

import pytest
import redis.asyncio as aioredis
from jwt import PyJWK

from events_concierge.adapters.oidc.auth import OidcJwtAuthContext
from events_concierge.adapters.oidc.session import OidcBffSessionAdapter
from events_concierge.ports.auth import (
    AuthenticationFailedError,
    BrowserIdentity,
    BrowserSessionUnavailableError,
)

pytestmark = pytest.mark.integration

_ORIGIN = "https://events.example.test"
_ISSUER = "https://identity.example.test"
_CLIENT_ID = "events-concierge-web"
_TENANT_CLAIM = "https://events.example.test/tenant_id"


def _adapter(redis_url: str) -> OidcBffSessionAdapter:
    # Session issuance and resolution do not touch the verifier or HTTP client. Supplying these
    # inert dependencies keeps this test narrowly about the shared multi-replica Redis boundary.
    verifier = OidcJwtAuthContext(
        issuer=_ISSUER,
        audience=_CLIENT_ID,
        jwks_url=f"{_ISSUER}/jwks.json",
        tenant_claim=_TENANT_CLAIM,
        signing_key_resolver=lambda _token: PyJWK.from_dict({}),
    )
    return OidcBffSessionAdapter(
        issuer=_ISSUER,
        authorization_url=f"{_ISSUER}/authorize",
        token_url=f"{_ISSUER}/oauth/token",
        jwks_url=f"{_ISSUER}/jwks.json",
        client_id=_CLIENT_ID,
        client_secret="integration-fixture-secret",
        tenant_claim=_TENANT_CLAIM,
        redirect_uri=f"{_ORIGIN}/auth/callback",
        trusted_origin=_ORIGIN,
        redis_url=redis_url,
        identity_verifier=verifier,
    )


async def test_redis_bff_sessions_are_shared_indexed_ttl_bounded_and_tenant_revocable(  # noqa: PLR0915 -- one complete cross-replica session lifecycle
) -> None:
    redis_url = os.environ["EC_REDIS_URL"]
    first, second = _adapter(redis_url), _adapter(redis_url)
    raw = aioredis.from_url(redis_url, decode_responses=True)
    credentials = None
    concurrent = None
    tenant_id = uuid4()
    fence_key = ""
    try:
        credentials = await first.issue_session(BrowserIdentity(tenant_id, "oidc|redis-user"))
        concurrent = await second.issue_session(BrowserIdentity(tenant_id, "oidc|redis-user"))
        additional = [
            await first.issue_session(BrowserIdentity(tenant_id, "oidc|redis-user"))
            for _ in range(30)
        ]
        with pytest.raises(BrowserSessionUnavailableError, match="allocate browser session"):
            await first.issue_session(BrowserIdentity(tenant_id, "oidc|redis-user"))
        cookie = (
            f"{first.session_cookie_name}={credentials.session_token}; "
            f"{first.csrf_cookie_name}={credentials.csrf_token}"
        )

        assert await first.is_ready() is True
        assert await second.resolve_tenant_id({"Cookie": cookie}) == tenant_id
        await second.verify_state_change(
            tenant_id,
            {
                "Cookie": cookie,
                "Origin": _ORIGIN,
                second.csrf_header_name: credentials.csrf_token,
            },
        )

        digest = hashlib.sha256(credentials.session_token.encode("ascii")).hexdigest()
        key = f"ec:oidc-bff:v2:{{tenant-sessions}}:session:{digest}"
        stored = await raw.get(key)
        ttl = await raw.ttl(key)
        assert isinstance(stored, str)
        assert credentials.session_token not in stored
        assert credentials.csrf_token not in stored
        assert 0 < ttl <= first.session_ttl_seconds
        tenant_digest = hashlib.sha256(tenant_id.bytes).hexdigest()
        index_key = f"ec:oidc-bff:v2:{{tenant-sessions}}:tenant:{tenant_digest}:sessions"
        fence_key = f"ec:oidc-bff:v2:{{tenant-sessions}}:tenant:{tenant_digest}:erased"
        assert str(tenant_id) not in index_key
        assert await raw.scard(index_key) == 32

        concurrent_cookie = (
            f"{second.session_cookie_name}={concurrent.session_token}; "
            f"{second.csrf_cookie_name}={concurrent.csrf_token}"
        )
        assert await first.resolve_tenant_id({"Cookie": concurrent_cookie}) == tenant_id

        await second.revoke_tenant_sessions(tenant_id)
        with pytest.raises(AuthenticationFailedError):
            await first.resolve_tenant_id({"Cookie": cookie})
        with pytest.raises(AuthenticationFailedError):
            await first.resolve_tenant_id({"Cookie": concurrent_cookie})
        with pytest.raises(BrowserSessionUnavailableError, match="allocate browser session"):
            await first.issue_session(BrowserIdentity(tenant_id, "oidc|redis-user"))
        assert await raw.exists(key) == 0
        for issued in additional:
            issued_digest = hashlib.sha256(issued.session_token.encode("ascii")).hexdigest()
            assert (
                await raw.exists(
                    f"ec:oidc-bff:v2:{{tenant-sessions}}:session:{issued_digest}"
                )
                == 0
            )
        assert await raw.exists(index_key) == 0
        assert await raw.exists(fence_key) == 1
    finally:
        if credentials is not None:
            await first.revoke_session(
                {"Cookie": f"{first.session_cookie_name}={credentials.session_token}"}
            )
        await first.aclose()
        await second.aclose()
        if fence_key:
            await raw.delete(fence_key)
        await raw.aclose()
