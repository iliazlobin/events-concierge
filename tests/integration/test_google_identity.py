"""Exact pre-provisioned Google lookup and session issuance ordered with real erasure fencing."""

import asyncio
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from tests.unit.test_google_oidc import _google_settings, _token
from tests.unit.test_oidc_bff_session import (
    _CLIENT_ID,
    _CLIENT_SECRET,
    _ORIGIN,
    _PUBLIC_JWK,
    _MemorySessionStore,
    app_module,
)

from events_concierge.adapters.oidc.auth import OidcJwtAuthContext
from events_concierge.adapters.oidc.session import OidcBffSessionAdapter
from events_concierge.adapters.postgres.account_erasure import PostgresAccountErasureRepository
from events_concierge.adapters.postgres.tenant_effects import PostgresTenantEffectAuthority
from events_concierge.adapters.postgres.tenant_repos import PostgresTenantRepository
from events_concierge.domain.credentials import Tenant
from events_concierge.domain.oidc import (
    GOOGLE_AUTHORIZATION_URL,
    GOOGLE_ISSUER,
    GOOGLE_JWKS_URL,
    GOOGLE_SUBJECT_PREFIX,
    GOOGLE_TOKEN_URL,
    google_subject_binding,
)
from events_concierge.infra.db import system_session_scope
from events_concierge.ports.tenant_effects import (
    TenantEffectFencedError,
    TenantEffectKind,
    TenantEffectRequest,
)

pytestmark = pytest.mark.integration


def _tenant(*, google: bool = True) -> Tenant:
    tenant_id = uuid4()
    subject = tenant_id.hex
    return Tenant(
        tenant_id,
        google_subject_binding(subject) if google else f"oidc|{subject}",
        "shared-contact@example.test",
        f"{tenant_id.hex}@relay.example.test",
    )


async def test_google_lookup_is_exact_read_only_narrow_and_keeps_tenant_rls(db: None) -> None:
    repository = PostgresTenantRepository()
    assert await repository.google_identity_is_ready()
    google, same_email, legacy = _tenant(), _tenant(), _tenant(google=False)
    for tenant in (google, same_email, legacy):
        await repository.add(tenant)
    for tenant in (google, same_email):
        assert await repository.resolve_google_tenant(tenant.oidc_subject) == tenant.tenant_id
    for missing in (
        legacy.oidc_subject,
        google.notify_email,
        google.oidc_subject.replace("accounts.google.com", "accounts.google.com.attacker"),
        google.oidc_subject + "\n",
        google_subject_binding(uuid4().hex),
    ):
        assert await repository.resolve_google_tenant(missing) is None
    async with system_session_scope() as session:
        assert (
            await session.execute(text("SELECT count(*) FROM public.tenants"))
        ).scalar_one() == 0
        metadata = (
            await session.execute(
                text("""
                SELECT proretset, prorettype::regtype::text AS result_type,
                       prosecdef, proconfig,
                       has_function_privilege('ec_app', oid, 'EXECUTE') AS app_can_execute,
                       has_function_privilege('ec_operator_viewer', oid, 'EXECUTE') AS operator_can_execute
                FROM pg_proc WHERE oid = 'public.fn_resolve_google_tenant(text)'::regprocedure
            """)
            )
        ).one()
        assert metadata.result_type == "uuid" and not metadata.proretset
        assert metadata.prosecdef and metadata.app_can_execute
        assert not metadata.operator_can_execute
        assert "search_path=pg_catalog, public" in metadata.proconfig
        denied_roles = (
            await session.execute(
                text("""
                SELECT role_name, has_function_privilege(
                    role_name, 'public.fn_resolve_google_tenant(text)', 'EXECUTE'
                ) AS can_execute
                FROM unnest(ARRAY['ec_operator_viewer', 'ec_operator_controller',
                                  'ec_ingestion_executor']) AS role_name
            """)
            )
        ).all()
        assert len(denied_roles) == 3 and not any(row.can_execute for row in denied_roles)
        assert not (
            await session.execute(
                text("""
                SELECT EXISTS (
                    SELECT 1 FROM pg_proc AS proc,
                    LATERAL aclexplode(coalesce(proc.proacl, acldefault('f', proc.proowner))) AS acl
                    WHERE proc.oid = 'public.fn_resolve_google_tenant(text)'::regprocedure
                      AND acl.grantee = 0 AND acl.privilege_type = 'EXECUTE'
                )
            """)
            )
        ).scalar_one()
    for tenant in (google, same_email, legacy):
        assert await repository.get(tenant.tenant_id) == tenant
    await PostgresAccountErasureRepository().begin(google.tenant_id, uuid4())
    assert await repository.resolve_google_tenant(google.oidc_subject) is None
    assert await repository.resolve_google_tenant(same_email.oidc_subject) == same_email.tenant_id


async def test_session_issuance_and_erasure_are_serialized_without_nested_db_reads(
    db: None,
) -> None:
    repository = PostgresTenantRepository()
    tenant = _tenant()
    await repository.add(tenant)
    resolved = await repository.resolve_google_tenant(tenant.oidc_subject)
    assert resolved == tenant.tenant_id
    assert await repository.get(resolved) == tenant
    authority = PostgresTenantEffectAuthority()
    entered, release = asyncio.Event(), asyncio.Event()
    issued: list[str] = []

    async def redis_issue() -> str:
        entered.set()
        await release.wait()
        issued.append("session")
        return "opaque-session"

    request = TenantEffectRequest(tenant.tenant_id, TenantEffectKind.BROWSER_SESSION, 5)
    issuing = asyncio.create_task(authority.run(request, redis_issue))
    erasing = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        erasing = asyncio.create_task(
            PostgresAccountErasureRepository().begin(tenant.tenant_id, uuid4())
        )
        # A second tenant is independent while the first account's issuance is held.
        other = _tenant()
        await repository.add(other)
        assert await repository.resolve_google_tenant(other.oidc_subject) == other.tenant_id
        await asyncio.sleep(0.05)
        assert not erasing.done()
        release.set()
        assert await asyncio.wait_for(issuing, timeout=3) == "opaque-session"
        await asyncio.wait_for(erasing, timeout=3)
        with pytest.raises(TenantEffectFencedError):
            await authority.run(request, redis_issue)
        assert issued == ["session"]
        assert await repository.resolve_google_tenant(tenant.oidc_subject) is None
    finally:
        release.set()
        await asyncio.gather(issuing, *([erasing] if erasing else []), return_exceptions=True)


async def test_google_callback_requires_real_preprovisioning_and_rejects_fenced_account(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = PostgresTenantRepository()
    tenant = _tenant()
    store = _MemorySessionStore()
    nonce = ""

    async def exchange(request: httpx.Request) -> httpx.Response:
        assert request.url == GOOGLE_TOKEN_URL
        return httpx.Response(
            200,
            json={
                "id_token": _token(
                    nonce, sub=tenant.oidc_subject.removeprefix(GOOGLE_SUBJECT_PREFIX)
                )
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(exchange)) as token_client:
        session = OidcBffSessionAdapter(
            issuer=GOOGLE_ISSUER,
            authorization_url=GOOGLE_AUTHORIZATION_URL,
            token_url=GOOGLE_TOKEN_URL,
            jwks_url=GOOGLE_JWKS_URL,
            client_id=_CLIENT_ID,
            client_secret=_CLIENT_SECRET,
            provider="google",
            google_tenant_lookup=repository.resolve_google_tenant,
            redirect_uri=f"{_ORIGIN}/auth/callback",
            trusted_origin=_ORIGIN,
            redis_url="redis://unused",
            store=store,
            http_client=token_client,
            identity_verifier=OidcJwtAuthContext(
                issuer=GOOGLE_ISSUER,
                audience=_CLIENT_ID,
                jwks_url=GOOGLE_JWKS_URL,
                provider="google",
                google_tenant_lookup=repository.resolve_google_tenant,
                signing_key_resolver=lambda _: _PUBLIC_JWK,
            ),
        )
        monkeypatch.setattr(app_module, "get_settings", _google_settings)
        app = app_module.create_app()
        app.state.container = SimpleNamespace(
            browser_session=session,
            auth_context=session,
            csrf_protection=session,
            tenant_repo=repository,
            account_erasure_repo=PostgresAccountErasureRepository(),
            tenant_effect_authority=PostgresTenantEffectAuthority(),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=_ORIGIN
        ) as client:

            async def callback() -> httpx.Response:
                nonlocal nonce
                login = await client.get("/auth/login")
                query = parse_qs(urlsplit(login.headers["location"]).query)
                nonce = json.loads(next(iter(store.logins.values())))["nonce"]
                return await client.get(
                    "/auth/callback", params={"code": "fixture-code", "state": query["state"][0]}
                )

            unknown = await callback()
            assert unknown.status_code == 303
            assert unknown.headers["location"] == "/sign-in?reason=not_authorized"
            assert await repository.get(tenant.tenant_id) is None
            assert store.sessions == {}
            await repository.add(tenant)
            accepted = await callback()
            assert accepted.status_code == 303 and accepted.headers["location"] == "/app"
            assert len(store.sessions) == 1
            assert await repository.get(tenant.tenant_id) == tenant
            await PostgresAccountErasureRepository().begin(tenant.tenant_id, uuid4())
            fenced = await callback()
            assert fenced.headers["location"] == "/sign-in?reason=not_authorized"
            assert len(store.sessions) == 1  # no new session while the worker is pending
            assert (await client.get("/v1/me")).status_code == 423
        await session.aclose()
