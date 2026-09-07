"""Offline request-authentication boundary coverage (FR-1.1, AC-1/AC-2)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from events_concierge.adapters.mock.auth import HeaderAuthContext, LocalHeaderCsrfProtection
from events_concierge.composition import _build_auth_context, _build_csrf_protection
from events_concierge.config import Settings
from events_concierge.ports.auth import AuthenticationFailedError


async def test_header_auth_context_resolves_the_documented_header_case_insensitively() -> None:
    """The local adapter models HTTP header semantics but accepts only X-EC-Tenant-ID (FR-1.1)."""
    tenant_id = uuid4()

    resolved = await HeaderAuthContext().resolve_tenant_id({"x-ec-tenant-id": str(tenant_id)})

    assert resolved == tenant_id


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-EC-Tenant-ID": "not-a-uuid"},
        {"X-EC-Tenant-ID": "A3B3F7D2-0DA9-4A9E-A60B-EC5EF682279E"},
        {"X-EC-Tenant-ID": "{a3b3f7d2-0da9-4a9e-a60b-ec5ef682279e}"},
        {"X-EC-Tenant-ID": " a3b3f7d2-0da9-4a9e-a60b-ec5ef682279e"},
        {
            "X-EC-Tenant-ID": "a3b3f7d2-0da9-4a9e-a60b-ec5ef682279e",
            "x-ec-tenant-id": "a3b3f7d2-0da9-4a9e-a60b-ec5ef682279e",
        },
    ],
)
async def test_header_auth_context_rejects_missing_malformed_or_ambiguous_tenants(
    headers: dict[str, str],
) -> None:
    """No absent, noncanonical, or duplicate local identity can select an RLS tenant (AC-1/AC-2)."""
    with pytest.raises(AuthenticationFailedError, match="valid tenant authentication is required"):
        await HeaderAuthContext().resolve_tenant_id(headers)


def test_non_mock_composition_requires_an_injected_oidc_bff_auth_context() -> None:
    """The test header cannot silently become a production authentication mechanism (FR-1.1)."""
    with pytest.raises(ValueError, match="must inject a provisioned AuthContextPort"):
        _build_auth_context(Settings(mock_cloud=False))


async def test_local_header_auth_has_no_ambient_cookie_csrf_requirement() -> None:
    """The explicit local tenant header preserves existing mock mutation behavior."""
    await LocalHeaderCsrfProtection().verify_state_change(uuid4(), {})


def test_non_mock_composition_requires_an_injected_csrf_boundary() -> None:
    """Production cannot infer anti-CSRF semantics independently from its session adapter."""
    with pytest.raises(ValueError, match="must inject a provisioned CsrfProtectionPort"):
        _build_csrf_protection(Settings(mock_cloud=False))
