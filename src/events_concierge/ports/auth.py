"""Request-edge authentication context boundary (FR-1.1, AC-1/AC-2).

The API receives a tenant only through this port.  The offline adapter intentionally accepts a
single documented test header; a deployed Backend-for-Frontend must replace it with signed OIDC
session resolution at the composition root.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol
from uuid import UUID


class AuthenticationFailedError(PermissionError):
    """The request did not establish one valid authenticated tenant (FR-1.1)."""


class AuthContextPort(Protocol):
    """Resolve the request's authenticated tenant before any tenant-scoped store access.

    ``headers`` deliberately models only the edge input available to the BFF/session adapter.  The
    resolved UUID, rather than any caller-supplied body field, is the tenant value threaded through
    the application and RLS boundary (FR-1.1, FR-1.2, AC-1/AC-2).
    """

    async def resolve_tenant_id(self, headers: Mapping[str, str]) -> UUID:
        """Return the one authenticated tenant or raise ``AuthenticationFailedError``."""
        ...
