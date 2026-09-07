"""Operator role lookup.

The role is deliberately read-only to the application: 0161 grants ``ec_app`` only ``SELECT``, so no
request path -- however compromised -- can promote an account. Grants are an operator action taken
with database credentials.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol
from uuid import UUID


class TenantRole(StrEnum):
    """What an account is allowed to see beyond its own consumer surface."""

    MEMBER = "member"
    OPERATOR = "operator"
    ADMIN = "admin"


class TenantRoleRepository(Protocol):
    """Resolve one tenant's role."""

    async def get_role(self, tenant_id: UUID) -> TenantRole:
        """Return the granted role, defaulting to ``MEMBER`` when no grant exists."""
        ...
