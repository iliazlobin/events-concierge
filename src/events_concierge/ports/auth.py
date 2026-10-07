"""Request-edge authentication context boundary (FR-1.1, AC-1/AC-2).

The API receives a tenant only through this port.  The offline adapter intentionally accepts a
single documented test header; a deployed Backend-for-Frontend must replace it with signed OIDC
session resolution at the composition root.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID


class AuthenticationFailedError(PermissionError):
    """The request did not establish one valid authenticated tenant (FR-1.1)."""


class ConsumerSignInFailureReason(StrEnum):
    """Fixed, non-sensitive categories for private sign-in rejection counters."""

    ORIGIN = "origin"
    LOGIN_COOKIE = "login_cookie"
    CHALLENGE = "challenge"
    TOKEN_VERIFICATION = "token_verification"
    CLAIM_AUTHORITY = "claim_authority"
    CLAIM_PROVIDER = "claim_provider"
    CLAIM_UID = "claim_uid"
    CLAIM_EMAIL = "claim_email"
    CLAIM_FRESHNESS = "claim_freshness"
    REAUTHENTICATION = "reauthentication"
    ACCOUNT_UNAVAILABLE = "account_unavailable"
    ACCOUNT_FENCED = "account_fenced"
    EXISTING_SESSION_COOKIE = "existing_session_cookie"
    UNKNOWN = "unknown"


class ConsumerSignInRejectedError(AuthenticationFailedError):
    """Retain a bounded private category while keeping the public response generic."""

    def __init__(self, reason: ConsumerSignInFailureReason) -> None:
        if not isinstance(reason, ConsumerSignInFailureReason):
            raise ValueError("unknown consumer sign-in rejection reason")
        super().__init__("sign-in could not be verified")
        self.reason = reason


class CsrfVerificationFailedError(PermissionError):
    """A session-authenticated state change lacked valid anti-CSRF evidence."""


class BrowserSessionUnavailableError(RuntimeError):
    """The shared browser-session control plane could not safely complete an operation."""


class RecentAuthenticationRequiredError(PermissionError):
    """The browser session was valid but was not backed by sufficiently recent authentication."""


class BrowserStepUpUnavailableError(PermissionError):
    """The selected provider cannot prove the required destructive-action step-up."""


@dataclass(frozen=True, slots=True)
class BrowserIdentity:
    """Signed OIDC identity plus optional provider authentication time for step-up proof."""

    tenant_id: UUID
    subject: str
    authenticated_at: int | None = None


@dataclass(frozen=True, slots=True)
class BrowserLoginStart:
    """A provider redirect and the opaque, short-lived transaction cookie paired with it."""

    authorization_url: str
    transaction_token: str


@dataclass(frozen=True, slots=True)
class BrowserLoginCompletion:
    """Verified OIDC identity plus the same-origin return path sealed into its transaction."""

    identity: BrowserIdentity
    return_to: str
    reauthenticated: bool = False


@dataclass(frozen=True, slots=True)
class BrowserSessionCredentials:
    """Fresh opaque browser credentials returned only after a verified OIDC callback."""

    session_token: str
    csrf_token: str


class AuthContextPort(Protocol):
    """Resolve the request's authenticated tenant before any tenant-scoped store access.

    ``headers`` deliberately models only the edge input available to the BFF/session adapter.  The
    resolved UUID, rather than any caller-supplied body field, is the tenant value threaded through
    the application and RLS boundary (FR-1.1, FR-1.2, AC-1/AC-2).
    """

    async def resolve_tenant_id(self, headers: Mapping[str, str]) -> UUID:
        """Return the one authenticated tenant or raise ``AuthenticationFailedError``."""
        ...


class CsrfProtectionPort(Protocol):
    """Authorize one state-changing request after its tenant session is authenticated.

    The API invokes this boundary for every authenticated consumer mutation. A production adapter
    owns the deployment's session format and must bind its anti-CSRF evidence to that same session,
    rejecting missing, malformed, replayed, or mismatched evidence with
    ``CsrfVerificationFailedError``. If the deployment also accepts a non-cookie authentication
    mechanism, any exemption must be an explicit decision inside that adapter rather than an API
    fallback.

    The signed handoff-capability POST is a separate bearer-authority boundary and intentionally
    does not pass through this port.
    """

    async def verify_state_change(
        self,
        tenant_id: UUID,
        headers: Mapping[str, str],
    ) -> None:
        """Return only when this authenticated state change has valid CSRF authority."""
        ...


class BrowserSessionLifecyclePort(AuthContextPort, CsrfProtectionPort, Protocol):
    """Same-origin OIDC BFF login, session issuance, and revocation boundary.

    Authentication and CSRF resolution remain on the two narrower ports above so application
    routes cannot accidentally gain login authority.  The API uses this wider lifecycle only on
    the explicitly registered ``/auth/*`` endpoints.
    """

    login_cookie_name: str
    session_cookie_name: str
    csrf_cookie_name: str
    csrf_header_name: str
    login_ttl_seconds: int
    session_ttl_seconds: int

    async def start_login(self, return_to: str) -> BrowserLoginStart:
        """Persist a one-shot PKCE transaction and return its provider redirect."""
        ...

    async def cancel_login(self, headers: Mapping[str, str], *, state: str) -> None:
        """Consume a provider-denied login only with its matching browser transaction/state."""
        ...

    async def start_reauthentication(
        self,
        tenant_id: UUID,
        headers: Mapping[str, str],
        *,
        return_to: str,
    ) -> BrowserLoginStart:
        """Bind an interactive provider reauthentication to the current tenant/session.

        Implementations must use a purpose-specific, one-shot transaction, request visible
        provider authentication, verify ``auth_time``, and mint only a short-lived server-side
        grant bound to the still-live session. A fresh callback or client flag is not authority.
        """
        ...

    async def complete_login(
        self,
        headers: Mapping[str, str],
        *,
        code: str,
        state: str,
    ) -> BrowserLoginCompletion:
        """Consume one transaction, exchange its code, and verify the signed identity token."""
        ...

    async def issue_session(self, identity: BrowserIdentity) -> BrowserSessionCredentials:
        """Create a new fixed-lifetime session after application account binding succeeds."""
        ...

    async def revoke_session(self, headers: Mapping[str, str]) -> None:
        """Revoke the current opaque session if present; repeated revocation is safe."""
        ...

    async def verify_recent_auth(
        self,
        tenant_id: UUID,
        headers: Mapping[str, str],
        *,
        max_age_seconds: int,
    ) -> None:
        """Require a provider-verified authentication no older than ``max_age_seconds``."""
        ...

    async def revoke_tenant_sessions(self, tenant_id: UUID) -> None:
        """Atomically fence future issuance and revoke every indexed session for the tenant."""
        ...

    async def is_ready(self) -> bool:
        """Return whether the shared session control plane can safely admit browser traffic."""
        ...

    async def aclose(self) -> None:
        """Close adapter-owned HTTP and Redis clients."""
        ...
