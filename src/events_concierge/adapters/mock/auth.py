"""Fail-closed local request authentication adapter (FR-1.1, AC-1/AC-2).

``X-EC-Tenant-ID`` is intentionally a local/test-only stand-in for the production OIDC BFF
session.  It is not an authorization mechanism for a non-mock deployment.
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from ...ports.auth import AuthenticationFailedError

_TENANT_HEADER = "x-ec-tenant-id"


class HeaderAuthContext:
    """Resolve one canonical UUID from the documented local ``X-EC-Tenant-ID`` header.

    HTTP header names are case-insensitive, but multiple spellings of the tenant header are
    rejected rather than letting an intermediary choose one.  The value must be the exact
    lowercase-hyphenated UUID representation, so malformed identity input never reaches RLS
    (FR-1.1, AC-1/AC-2).
    """

    async def resolve_tenant_id(self, headers: Mapping[str, str]) -> UUID:
        """Return the authenticated local tenant or fail closed without echoing header content."""
        values = [
            value
            for name, value in headers.items()
            if isinstance(name, str) and name.casefold() == _TENANT_HEADER
        ]
        if len(values) != 1 or not isinstance(values[0], str):
            raise AuthenticationFailedError("valid tenant authentication is required")

        raw_tenant_id = values[0]
        try:
            tenant_id = UUID(raw_tenant_id)
        except (AttributeError, TypeError, ValueError) as error:
            raise AuthenticationFailedError("valid tenant authentication is required") from error
        if raw_tenant_id != str(tenant_id):
            raise AuthenticationFailedError("valid tenant authentication is required")
        return tenant_id
