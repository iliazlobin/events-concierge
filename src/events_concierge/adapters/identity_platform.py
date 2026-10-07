"""GCP Identity Platform sign-in with application-owned, revocable browser sessions.

Only the official Admin SDK verifies project-bound tokens. The web SDK uses memory persistence;
the API exchanges one fresh verified token and browser-bound challenge for an opaque cookie.
Provider credentials and refresh tokens are never retained by this application.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from time import monotonic, time
from typing import Any, TypeVar, cast
from uuid import UUID

import firebase_admin  # type: ignore[import-untyped]
from firebase_admin import auth
from firebase_admin.exceptions import FirebaseError  # type: ignore[import-untyped]
from google.auth.exceptions import GoogleAuthError

from ..domain.consumer_identity import VerifiedConsumerIdentity
from ..domain.credentials import Tenant
from ..ports.auth import (
    AuthenticationFailedError,
    BrowserIdentity,
    BrowserLoginCompletion,
    BrowserLoginStart,
    BrowserSessionUnavailableError,
    ConsumerSignInFailureReason,
    ConsumerSignInRejectedError,
)
from .oidc.session import (
    OpaqueBrowserSessionAdapter,
    _digest,
    _json,
    _one_header,
    _random_token,
    _required_cookie,
    _safe_return_path,
    _valid_token,
)

_MAX_TOKEN_BYTES = 16 * 1024
_MAX_UID_LENGTH = 128
_MIN_EMAIL_LENGTH = 3
_MAX_EMAIL_LENGTH = 320
_MAX_SIGN_IN_AGE = 300
_CLOCK_SKEW = 30
_MAX_REVOCATION_CACHE = 512
_REVOCATION_CACHE_SECONDS = 60
_T = TypeVar("_T")


class IdentityPlatformVerifier:
    """Fixed project, revoked/disabled-user checks, and bounded off-loop SDK requests."""

    def __init__(self, project_id: str, providers: tuple[str, ...]) -> None:
        if os.environ.get("FIREBASE_AUTH_EMULATOR_HOST"):
            raise ValueError("Identity Platform cannot use an authentication emulator")
        self.project_id = project_id
        self.providers = frozenset(providers)
        if not self.providers or not self.providers <= {"google.com", "apple.com"}:
            raise ValueError("only Google and Apple consumer identities are allowed")
        name = f"events-concierge-{project_id}"
        try:
            self._app = firebase_admin.get_app(name)
        except ValueError:
            self._app = firebase_admin.initialize_app(
                options={"projectId": project_id, "httpTimeout": 5}, name=name
            )
        if self._app.project_id != project_id:
            raise ValueError("Identity Platform project authority changed")
        self._slots = asyncio.Semaphore(4)
        self._revocation_cache: dict[str, tuple[float, bool, int]] = {}
        self._inflight: dict[str, asyncio.Task[Any]] = {}

    async def _call(self, operation: Callable[[], _T]) -> _T:
        try:
            await asyncio.wait_for(self._slots.acquire(), timeout=2)
        except TimeoutError as error:
            raise BrowserSessionUnavailableError("identity verification is busy") from error

        async def held() -> _T:
            try:
                return await asyncio.to_thread(operation)
            finally:
                self._slots.release()

        # A timed-out SDK thread keeps its capacity slot until it actually finishes.
        task = asyncio.create_task(held())
        task.add_done_callback(
            lambda completed: None if completed.cancelled() else completed.exception()
        )
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=10)
        except (TimeoutError, OSError, FirebaseError, GoogleAuthError) as error:
            raise BrowserSessionUnavailableError("identity service unavailable") from error

    async def verify(self, token: str) -> VerifiedConsumerIdentity:
        if not token or len(token.encode("utf-8")) > _MAX_TOKEN_BYTES:
            raise ConsumerSignInRejectedError(ConsumerSignInFailureReason.TOKEN_VERIFICATION)
        try:
            claims = await self._call(
                lambda: auth.verify_id_token(token, app=self._app, check_revoked=True)
            )
        except BrowserSessionUnavailableError as error:
            if isinstance(
                error.__cause__,
                (
                    auth.InvalidIdTokenError,
                    auth.ExpiredIdTokenError,
                    auth.RevokedIdTokenError,
                    auth.UserDisabledError,
                    auth.UserNotFoundError,
                ),
            ):
                raise ConsumerSignInRejectedError(
                    ConsumerSignInFailureReason.TOKEN_VERIFICATION
                ) from error
            raise
        except (ValueError, TypeError) as error:
            raise ConsumerSignInRejectedError(
                ConsumerSignInFailureReason.TOKEN_VERIFICATION
            ) from error
        return verified_identity(claims, self.project_id, self.providers)

    async def check_session(self, uid: str, authenticated_at: int) -> None:
        cached = self._revocation_cache.get(uid)
        if cached is None or monotonic() - cached[0] >= _REVOCATION_CACHE_SECONDS:
            task = self._inflight.get(uid)
            if task is None:
                task = asyncio.create_task(self._call(lambda: auth.get_user(uid, app=self._app)))
                self._inflight[uid] = task
                task.add_done_callback(
                    lambda completed: None if completed.cancelled() else completed.exception()
                )
            try:
                user = await asyncio.shield(task)
            except BrowserSessionUnavailableError as error:
                if isinstance(error.__cause__, auth.UserNotFoundError):
                    raise AuthenticationFailedError("account no longer available") from error
                raise
            finally:
                if task.done():
                    self._inflight.pop(uid, None)
            cached = (monotonic(), user.disabled, user.tokens_valid_after_timestamp)
            if len(self._revocation_cache) >= _MAX_REVOCATION_CACHE:
                self._revocation_cache.pop(next(iter(self._revocation_cache)))
            self._revocation_cache[uid] = cached
        if cached[1] or authenticated_at * 1000 < cached[2]:
            raise AuthenticationFailedError("account session revoked")

    async def delete_account(self, uid: str) -> None:
        """The erasure worker removes the managed identity; retries of a missing user succeed."""
        self._revocation_cache.pop(uid, None)
        try:
            await self._call(lambda: auth.delete_user(uid, app=self._app))
        except BrowserSessionUnavailableError as error:
            if not isinstance(error.__cause__, auth.UserNotFoundError):
                raise
        # A read already in flight cannot restore a valid cache entry after deletion.
        task = self._inflight.pop(uid, None)
        if task is not None:
            with suppress(BrowserSessionUnavailableError):
                await asyncio.shield(task)
        self._revocation_cache.pop(uid, None)


def verified_identity(
    claims: Mapping[str, Any], project_id: str, providers: frozenset[str]
) -> VerifiedConsumerIdentity:
    """Validate the application contract after the Admin SDK verifies the signature and lifetime."""
    firebase = claims.get("firebase")
    uid, email, authenticated_at = claims.get("sub"), claims.get("email"), claims.get("auth_time")
    provider = firebase.get("sign_in_provider") if isinstance(firebase, dict) else None
    now = int(time())
    if (
        claims.get("iss") != f"https://securetoken.google.com/{project_id}"
        or claims.get("aud") != project_id
        or not isinstance(firebase, dict)
        or firebase.get("tenant") is not None
    ):
        raise ConsumerSignInRejectedError(ConsumerSignInFailureReason.CLAIM_AUTHORITY)
    if not isinstance(provider, str) or provider not in providers:
        raise ConsumerSignInRejectedError(ConsumerSignInFailureReason.CLAIM_PROVIDER)
    if (
        not isinstance(uid, str)
        or not 1 <= len(uid) <= _MAX_UID_LENGTH
        or not uid.isascii()
        or not uid.isprintable()
        or any(char.isspace() for char in uid)
    ):
        raise ConsumerSignInRejectedError(ConsumerSignInFailureReason.CLAIM_UID)
    if (
        claims.get("email_verified") is not True
        or not isinstance(email, str)
        or not _MIN_EMAIL_LENGTH <= len(email) <= _MAX_EMAIL_LENGTH
        or email.count("@") != 1
        or not email.isprintable()
        or any(char.isspace() for char in email)
    ):
        raise ConsumerSignInRejectedError(ConsumerSignInFailureReason.CLAIM_EMAIL)
    if (
        type(authenticated_at) is not int
        or not now - _MAX_SIGN_IN_AGE <= authenticated_at <= now + _CLOCK_SKEW
    ):
        raise ConsumerSignInRejectedError(ConsumerSignInFailureReason.CLAIM_FRESHNESS)
    return VerifiedConsumerIdentity(project_id, uid, provider, email, authenticated_at)


@dataclass(frozen=True, slots=True)
class IdentityLoginChallenge:
    state: str
    transaction_token: str


@dataclass(frozen=True, slots=True)
class IdentityLoginCompletion:
    identity: VerifiedConsumerIdentity
    return_to: str
    reauthenticated: bool


class IdentityPlatformBrowserSessionAdapter(OpaqueBrowserSessionAdapter):
    """One-shot sign-in and step-up, using the existing Redis session/CSRF/erasure controls."""

    def __init__(
        self,
        *,
        verifier: IdentityPlatformVerifier,
        tenant_lookup: Callable[[UUID], Awaitable[Tenant | None]] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.verifier = verifier
        self._tenant_lookup = tenant_lookup

    def _session_metadata(self, identity: BrowserIdentity) -> dict[str, str | int]:
        if type(identity.authenticated_at) is not int:
            raise AuthenticationFailedError("verified authentication time required")
        return {
            "identity_project": self.verifier.project_id,
            "identity_auth_time": identity.authenticated_at,
        }

    def _assert_session_authority(self, record: Mapping[str, Any]) -> None:
        if record.get("identity_project") != self.verifier.project_id or not record[
            "subject"
        ].startswith(f"identity-platform:v1:{self.verifier.project_id}:"):
            raise AuthenticationFailedError("browser session identity authority changed")

    def _assert_step_up_available(self) -> None:
        pass

    async def resolve_tenant_id(self, headers: Mapping[str, str]) -> UUID:
        token, record = await self._session(headers)
        prefix = f"identity-platform:v1:{self.verifier.project_id}:"
        try:
            await self.verifier.check_session(
                record["subject"][len(prefix) :], record["identity_auth_time"]
            )
        except AuthenticationFailedError:
            await self._store.delete_session(token)
            raise
        return cast("UUID", record["tenant_id"])

    async def revoke_tenant_sessions(self, tenant_id: UUID) -> None:
        """Fence sessions before deleting this account's exact managed identity."""
        await super().revoke_tenant_sessions(tenant_id)
        if self._tenant_lookup is None:
            raise BrowserSessionUnavailableError("managed account erasure is not configured")
        tenant = await self._tenant_lookup(tenant_id)
        if tenant is None:
            raise BrowserSessionUnavailableError("managed account binding is unavailable")
        prefix = f"identity-platform:v1:{self.verifier.project_id}:"
        if not tenant.oidc_subject.startswith("identity-platform:v1:"):
            # Pre-existing external OIDC accounts have no Identity Platform record to delete.
            return
        if not tenant.oidc_subject.startswith(prefix):
            raise BrowserSessionUnavailableError("managed account project changed")
        await self.verifier.delete_account(tenant.oidc_subject[len(prefix) :])

    async def begin_login(self, return_to: str) -> IdentityLoginChallenge:
        return await self._begin(
            {"purpose": "identity-login", "return_to": _safe_return_path(return_to)}
        )

    async def _begin(self, payload: dict[str, Any]) -> IdentityLoginChallenge:
        for _attempt in range(2):
            state, token = _random_token(), _random_token()
            record = {
                "v": 1,
                "project": self.verifier.project_id,
                "state_hash": _digest(state),
                "started_at": int(time()),
                **payload,
            }
            if await self._store.create_login(token, _json(record), self.login_ttl_seconds):
                return IdentityLoginChallenge(state, token)
        raise BrowserSessionUnavailableError("could not allocate sign-in challenge")

    async def complete_identity_login(
        self, headers: Mapping[str, str], *, token: str, state: str
    ) -> IdentityLoginCompletion:
        if _one_header(headers, "origin") != self._trusted_origin:
            raise ConsumerSignInRejectedError(ConsumerSignInFailureReason.ORIGIN)
        if not _valid_token(state):
            raise ConsumerSignInRejectedError(ConsumerSignInFailureReason.CHALLENGE)
        try:
            transaction_token = _required_cookie(headers, self.login_cookie_name)
        except AuthenticationFailedError as error:
            raise ConsumerSignInRejectedError(ConsumerSignInFailureReason.LOGIN_COOKIE) from error
        raw = await self._store.consume_login(transaction_token)
        try:
            record = json.loads(raw or "null")
            if (
                not isinstance(record, dict)
                or record.get("v") != 1
                or record.get("project") != self.verifier.project_id
                or record.get("purpose") not in {"identity-login", "identity-reauth"}
                or not hmac.compare_digest(record.get("state_hash", ""), _digest(state))
            ):
                raise ValueError("invalid sign-in challenge")
            return_to = _safe_return_path(record["return_to"])
        except (ValueError, TypeError, KeyError) as error:
            raise ConsumerSignInRejectedError(ConsumerSignInFailureReason.CHALLENGE) from error
        identity = await self.verifier.verify(token)
        reauthenticated = record["purpose"] == "identity-reauth"
        if reauthenticated:
            try:
                session_token, session = await self._session(headers)
            except AuthenticationFailedError as error:
                raise ConsumerSignInRejectedError(
                    ConsumerSignInFailureReason.REAUTHENTICATION
                ) from error
            if (
                record.get("tenant_id") != str(identity.tenant_id)
                or session["tenant_id"] != identity.tenant_id
                or session["subject"] != identity.subject
                or not hmac.compare_digest(record.get("session_hash", ""), _digest(session_token))
                or identity.authenticated_at < record["started_at"] - _CLOCK_SKEW
                or not await self._store.mark_recent_auth(
                    session_token, identity.tenant_id, identity.subject, identity.authenticated_at
                )
            ):
                raise ConsumerSignInRejectedError(ConsumerSignInFailureReason.REAUTHENTICATION)
        return IdentityLoginCompletion(identity, return_to, reauthenticated)

    async def start_login(self, return_to: str) -> BrowserLoginStart:
        # The web SDK owns the Google/Apple provider popup. Never redirect back into /auth/login.
        raise AuthenticationFailedError("use the application sign-in page")

    async def start_reauthentication(
        self, tenant_id: UUID, headers: Mapping[str, str], *, return_to: str
    ) -> BrowserLoginStart:
        await self.verify_state_change(tenant_id, headers)
        session_token, record = await self._session(headers)
        challenge = await self._begin(
            {
                "purpose": "identity-reauth",
                "return_to": _safe_return_path(return_to),
                "tenant_id": str(tenant_id),
                "session_hash": _digest(session_token),
            }
        )
        del record
        return BrowserLoginStart(
            f"/sign-in?reauth=1&state={challenge.state}", challenge.transaction_token
        )

    async def cancel_login(self, headers: Mapping[str, str], *, state: str) -> None:
        del state
        await self._store.consume_login(_required_cookie(headers, self.login_cookie_name))

    async def complete_login(
        self, headers: Mapping[str, str], *, code: str, state: str
    ) -> BrowserLoginCompletion:
        raise AuthenticationFailedError("use the application sign-in page")
