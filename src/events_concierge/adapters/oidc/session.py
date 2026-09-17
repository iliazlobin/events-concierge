"""Redis-backed same-origin OIDC BFF sessions.

Only random opaque credentials reach the browser. Login transactions and authenticated sessions
are shared across API replicas in Redis, keyed by SHA-256 digests and bounded by fixed TTLs. The
authorization-code flow uses state, nonce, and S256 PKCE; the token endpoint and signing-key URL
are deployment configuration and can never be selected by browser input.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Awaitable, Callable, Mapping
from time import time as wall_time
from typing import Any, Literal, Protocol, cast
from unicodedata import category
from urllib.parse import urlencode, urlsplit
from uuid import UUID

import httpx
import redis.asyncio as aioredis
from redis.exceptions import RedisError

from ...domain.oidc import GOOGLE_AUTHORIZATION_URL, GOOGLE_SUBJECT_PREFIX, GOOGLE_TOKEN_URL
from ...ports.auth import (
    AuthenticationFailedError,
    BrowserIdentity,
    BrowserLoginCompletion,
    BrowserLoginStart,
    BrowserSessionCredentials,
    BrowserSessionUnavailableError,
    BrowserStepUpUnavailableError,
    CsrfVerificationFailedError,
    RecentAuthenticationRequiredError,
)
from .auth import OidcJwtAuthContext

_TOKEN_BYTES = 32
_TOKEN_LENGTH = 43
_MAX_COOKIE_HEADER_BYTES = 8192
_MAX_CODE_BYTES = 4096
_MAX_ID_TOKEN_BYTES = 16 * 1024
_MAX_TOKEN_RESPONSE_BYTES = 64 * 1024
_MAX_RETURN_TO_BYTES = 2048
_MAX_SUBJECT_BYTES = 1024
_MAX_CLIENT_ID_BYTES = 512
_MAX_CLIENT_SECRET_BYTES = 4096
_MIN_LOGIN_TTL_SECONDS = 60
_MAX_LOGIN_TTL_SECONDS = 900
_MIN_SESSION_TTL_SECONDS = 300
_MAX_SESSION_TTL_SECONDS = 86_400
_MAX_HTTP_TIMEOUT_SECONDS = 30
_SERVER_ERROR_STATUS = 500
_SUCCESS_STATUS = 200
_SHA256_HEX_LENGTH = 64
_MAX_URL_PORT = 65_535
_DEFAULT_HTTPS_PORT = 443
_STORE_VERSION = 1
_MAX_TENANT_SESSIONS = 32
_MAX_RECENT_AUTH_SECONDS = 900
_FORCED_REAUTH_CALLBACK_AGE_SECONDS = 120
_AUTH_TIME_CLOCK_SKEW_SECONDS = 60
_REAUTH_PURPOSE = "account_erasure"
_CREATE_INDEXED_SESSION_SCRIPT = """
if redis.call('EXISTS', KEYS[3]) == 1 then
    return -1
end
local indexed_count = redis.call('SCARD', KEYS[2])
if indexed_count > tonumber(ARGV[3]) then
    return redis.error_reply('tenant session index exceeds safety cap')
end
local indexed_sessions = redis.call('SMEMBERS', KEYS[2])
for _, session_key in ipairs(indexed_sessions) do
    if redis.call('EXISTS', session_key) == 0 then
        redis.call('SREM', KEYS[2], session_key)
    end
end
if redis.call('SCARD', KEYS[2]) >= tonumber(ARGV[3]) then
    return -2
end
local created = redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2], 'NX')
if not created then
    return 0
end
redis.call('SADD', KEYS[2], KEYS[1])
local current_ttl = redis.call('TTL', KEYS[2])
if current_ttl < tonumber(ARGV[2]) then
    redis.call('EXPIRE', KEYS[2], ARGV[2])
end
return 1
"""
_DELETE_INDEXED_SESSION_SCRIPT = """
redis.call('DEL', KEYS[1])
redis.call('SREM', KEYS[2], KEYS[1])
if redis.call('SCARD', KEYS[2]) == 0 then
    redis.call('DEL', KEYS[2])
end
return 1
"""
_FENCE_AND_REVOKE_TENANT_SCRIPT = """
redis.call('SET', KEYS[2], '1')
local sessions = redis.call('SPOP', KEYS[1], tonumber(ARGV[1]))
for _, session_key in ipairs(sessions) do
    redis.call('DEL', session_key)
end
if redis.call('SCARD', KEYS[1]) > 0 then
    return redis.error_reply('tenant session index exceeds safety cap; retry revocation')
end
redis.call('DEL', KEYS[1])
return #sessions
"""
_MARK_RECENT_AUTH_SCRIPT = """
local raw_session = redis.call('GET', KEYS[1])
if not raw_session then
    return 0
end
local decoded_ok, session = pcall(cjson.decode, raw_session)
if not decoded_ok then
    return -1
end
if session['tenant_id'] ~= ARGV[1] or session['subject'] ~= ARGV[2] then
    return -1
end
local ttl = redis.call('PTTL', KEYS[1])
if ttl <= 0 then
    return 0
end
session['recent_auth_at'] = tonumber(ARGV[3])
local stored = redis.call('SET', KEYS[1], cjson.encode(session), 'PX', ttl, 'XX')
if not stored then
    return 0
end
return 1
"""


class _SessionStore(Protocol):
    async def create_login(self, token: str, payload: str, ttl_seconds: int) -> bool: ...

    async def consume_login(self, token: str) -> str | None: ...

    async def create_session(
        self,
        token: str,
        payload: str,
        ttl_seconds: int,
        tenant_id: UUID,
    ) -> bool: ...

    async def get_session(self, token: str) -> str | None: ...

    async def delete_session(self, token: str) -> None: ...

    async def mark_recent_auth(
        self,
        token: str,
        tenant_id: UUID,
        subject: str,
        authenticated_at: int,
    ) -> bool: ...

    async def revoke_tenant_sessions(self, tenant_id: UUID) -> None: ...

    async def is_ready(self) -> bool: ...

    async def aclose(self) -> None: ...


class RedisOidcSessionStore:
    """Minimal fail-closed Redis store for one-shot logins and revocable browser sessions."""

    def __init__(
        self,
        redis_url: str,
        *,
        timeout_seconds: float = 2.0,
        redis_client: Any | None = None,
    ) -> None:
        self._redis = redis_client or aioredis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=timeout_seconds,
            socket_timeout=timeout_seconds,
        )
        self._owns_client = redis_client is None

    async def create_login(self, token: str, payload: str, ttl_seconds: int) -> bool:
        return await self._set_once("login", token, payload, ttl_seconds)

    async def consume_login(self, token: str) -> str | None:
        try:
            value = await self._redis.getdel(self._key("login", token))
        except (RedisError, TimeoutError) as error:
            raise BrowserSessionUnavailableError("browser session store unavailable") from error
        return cast("str | None", value)

    async def create_session(
        self,
        token: str,
        payload: str,
        ttl_seconds: int,
        tenant_id: UUID,
    ) -> bool:
        try:
            outcome = await self._redis.eval(
                _CREATE_INDEXED_SESSION_SCRIPT,
                3,
                self._session_key(token),
                self._tenant_index_key(tenant_id),
                self._tenant_fence_key(tenant_id),
                payload,
                ttl_seconds,
                _MAX_TENANT_SESSIONS,
            )
        except (RedisError, TimeoutError) as error:
            raise BrowserSessionUnavailableError("browser session store unavailable") from error
        return int(outcome) == 1

    async def get_session(self, token: str) -> str | None:
        try:
            value = await self._redis.get(self._key("session", token))
        except (RedisError, TimeoutError) as error:
            raise BrowserSessionUnavailableError("browser session store unavailable") from error
        return cast("str | None", value)

    async def delete_session(self, token: str) -> None:
        try:
            session_key = self._session_key(token)
            raw_session = await self._redis.get(session_key)
            tenant_id = self._stored_tenant_id(raw_session)
            if tenant_id is None:
                await self._redis.delete(session_key)
                return
            await self._redis.eval(
                _DELETE_INDEXED_SESSION_SCRIPT,
                2,
                session_key,
                self._tenant_index_key(tenant_id),
            )
        except (RedisError, TimeoutError) as error:
            raise BrowserSessionUnavailableError("browser session store unavailable") from error

    async def mark_recent_auth(
        self,
        token: str,
        tenant_id: UUID,
        subject: str,
        authenticated_at: int,
    ) -> bool:
        """Atomically add a bounded step-up grant without extending the session TTL."""
        try:
            outcome = await self._redis.eval(
                _MARK_RECENT_AUTH_SCRIPT,
                1,
                self._session_key(token),
                str(tenant_id),
                subject,
                authenticated_at,
            )
        except (RedisError, TimeoutError) as error:
            raise BrowserSessionUnavailableError("browser session store unavailable") from error
        return int(outcome) == 1

    async def revoke_tenant_sessions(self, tenant_id: UUID) -> None:
        try:
            await self._redis.eval(
                _FENCE_AND_REVOKE_TENANT_SCRIPT,
                2,
                self._tenant_index_key(tenant_id),
                self._tenant_fence_key(tenant_id),
                _MAX_TENANT_SESSIONS,
            )
        except (RedisError, TimeoutError) as error:
            raise BrowserSessionUnavailableError("browser session store unavailable") from error

    async def is_ready(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except (RedisError, TimeoutError):
            return False

    async def aclose(self) -> None:
        if self._owns_client:
            await self._redis.aclose()

    async def _set_once(self, kind: str, token: str, payload: str, ttl_seconds: int) -> bool:
        try:
            created = await self._redis.set(
                self._key(kind, token),
                payload,
                ex=ttl_seconds,
                nx=True,
            )
        except (RedisError, TimeoutError) as error:
            raise BrowserSessionUnavailableError("browser session store unavailable") from error
        return bool(created)

    @staticmethod
    def _key(kind: str, token: str) -> str:
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        if kind == "session":
            return f"ec:oidc-bff:v2:{{tenant-sessions}}:session:{digest}"
        return f"ec:oidc-bff:v1:{kind}:{digest}"

    @classmethod
    def _session_key(cls, token: str) -> str:
        return cls._key("session", token)

    @staticmethod
    def _tenant_index_key(tenant_id: UUID) -> str:
        digest = hashlib.sha256(tenant_id.bytes).hexdigest()
        return f"ec:oidc-bff:v2:{{tenant-sessions}}:tenant:{digest}:sessions"

    @staticmethod
    def _tenant_fence_key(tenant_id: UUID) -> str:
        digest = hashlib.sha256(tenant_id.bytes).hexdigest()
        return f"ec:oidc-bff:v2:{{tenant-sessions}}:tenant:{digest}:erased"

    @staticmethod
    def _stored_tenant_id(raw_session: object) -> UUID | None:
        if not isinstance(raw_session, str):
            return None
        try:
            tenant_id = json.loads(raw_session).get("tenant_id")
            return UUID(tenant_id) if isinstance(tenant_id, str) else None
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            return None


class OidcBffSessionAdapter:
    """Authorization-code BFF implementing auth, CSRF, and browser-session lifecycle ports."""

    login_cookie_name = "__Host-ec_login"
    session_cookie_name = "__Host-ec_session"
    csrf_cookie_name = "__Host-ec_csrf"
    csrf_header_name = "X-EC-CSRF"

    def __init__(
        self,
        *,
        issuer: str,
        authorization_url: str,
        token_url: str,
        jwks_url: str,
        client_id: str,
        client_secret: str,
        tenant_claim: str | None = None,
        provider: Literal["custom_claim", "google"] = "custom_claim",
        google_tenant_lookup: Callable[[str], Awaitable[UUID | None]] | None = None,
        google_identity_ready: Callable[[], Awaitable[bool]] | None = None,
        redirect_uri: str,
        trusted_origin: str,
        redis_url: str,
        algorithms: tuple[str, ...] = ("RS256",),
        login_ttl_seconds: int = 600,
        session_ttl_seconds: int = 28_800,
        http_timeout_seconds: float = 5.0,
        store_timeout_seconds: float = 2.0,
        store: _SessionStore | None = None,
        http_client: httpx.AsyncClient | None = None,
        identity_verifier: OidcJwtAuthContext | None = None,
    ) -> None:
        self._authorization_url = _https_url(authorization_url, "authorization URL")
        self._token_url = _https_url(token_url, "token URL")
        self._provider = provider
        self._google_identity_ready = google_identity_ready
        if provider == "google" and (
            authorization_url != GOOGLE_AUTHORIZATION_URL or token_url != GOOGLE_TOKEN_URL
        ):
            raise ValueError("Google login requires fixed Google authorization and token endpoints")
        self._redirect_uri = _https_url(redirect_uri, "redirect URI")
        self._trusted_origin = _origin(trusted_origin)
        if _url_origin(self._redirect_uri) != self._trusted_origin:
            raise ValueError("OIDC redirect URI must use the configured public origin")
        if not client_id or len(client_id.encode("utf-8")) > _MAX_CLIENT_ID_BYTES:
            raise ValueError("OIDC client ID must be a non-empty bounded value")
        if not client_secret or len(client_secret.encode("utf-8")) > _MAX_CLIENT_SECRET_BYTES:
            raise ValueError("OIDC client secret must be a non-empty bounded value")
        if not _MIN_LOGIN_TTL_SECONDS <= login_ttl_seconds <= _MAX_LOGIN_TTL_SECONDS:
            raise ValueError("OIDC login TTL must be between 60 and 900 seconds")
        if not _MIN_SESSION_TTL_SECONDS <= session_ttl_seconds <= _MAX_SESSION_TTL_SECONDS:
            raise ValueError("OIDC session TTL must be between 300 and 86400 seconds")
        if not 0 < http_timeout_seconds <= _MAX_HTTP_TIMEOUT_SECONDS:
            raise ValueError("OIDC HTTP timeout must be between 0 and 30 seconds")
        self.login_ttl_seconds = login_ttl_seconds
        self.session_ttl_seconds = session_ttl_seconds
        self._client_id = client_id
        self._client_secret = client_secret
        self._store: _SessionStore = store or RedisOidcSessionStore(
            redis_url,
            timeout_seconds=store_timeout_seconds,
        )
        self._http = http_client or httpx.AsyncClient(
            timeout=http_timeout_seconds,
            follow_redirects=False,
        )
        self._owns_http = http_client is None
        self._identity_verifier = identity_verifier or OidcJwtAuthContext(
            issuer=issuer,
            audience=client_id,
            jwks_url=jwks_url,
            tenant_claim=tenant_claim,
            provider=provider,
            google_tenant_lookup=google_tenant_lookup,
            algorithms=algorithms,
            jwks_timeout_seconds=http_timeout_seconds,
        )

    async def start_login(self, return_to: str) -> BrowserLoginStart:
        """Seal one return path beside independent state, nonce, and S256 PKCE secrets."""
        return await self._start_authorization(return_to, transaction={"purpose": "login"})

    async def start_reauthentication(
        self,
        tenant_id: UUID,
        headers: Mapping[str, str],
        *,
        return_to: str,
    ) -> BrowserLoginStart:
        """Bind a purpose-specific provider step-up to the current live browser session."""
        await self.verify_state_change(tenant_id, headers)
        if self._provider == "google":
            raise BrowserStepUpUnavailableError(
                "Google account deletion requires a separately configured step-up method"
            )
        session_token, record = await self._session(headers)
        if record["tenant_id"] != tenant_id:
            raise AuthenticationFailedError("valid tenant authentication is required")
        return await self._start_authorization(
            return_to,
            transaction={
                "purpose": _REAUTH_PURPOSE,
                "bound_session_hash": _digest(session_token),
                "tenant_id": str(tenant_id),
                "subject": record["subject"],
            },
        )

    async def _start_authorization(
        self,
        return_to: str,
        *,
        transaction: dict[str, object],
    ) -> BrowserLoginStart:
        return_path = _safe_return_path(return_to)
        for _attempt in range(2):
            transaction_token = _random_token()
            state, nonce, verifier = _random_token(), _random_token(), _random_token()
            payload = _json(
                {
                    "v": _STORE_VERSION,
                    "state": state,
                    "nonce": nonce,
                    "verifier": verifier,
                    "return_to": return_path,
                    **transaction,
                }
            )
            if await self._store.create_login(
                transaction_token,
                payload,
                self.login_ttl_seconds,
            ):
                break
        else:
            raise BrowserSessionUnavailableError("could not allocate login transaction")

        challenge = _base64url(hashlib.sha256(verifier.encode("ascii")).digest())
        authorization_parameters = {
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "scope": "openid email" if self._provider == "google" else "openid",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if self._provider == "google":
            # Account choice makes a denied-account retry useful. It is never reauthentication.
            authorization_parameters["prompt"] = "select_account"
        if transaction["purpose"] == _REAUTH_PURPOSE:
            # Both controls are deliberate. ``prompt=login`` requests visible interaction while
            # ``max_age=0`` makes OIDC require an ``auth_time`` claim in the resulting ID token.
            authorization_parameters.update({"prompt": "login", "max_age": "0"})
        query = urlencode(authorization_parameters)
        query_separator = "&" if urlsplit(self._authorization_url).query else "?"
        return BrowserLoginStart(
            authorization_url=f"{self._authorization_url}{query_separator}{query}",
            transaction_token=transaction_token,
        )

    async def cancel_login(self, headers: Mapping[str, str], *, state: str) -> None:
        """Provider denial consumes only a browser-bound, one-shot login transaction."""
        await self._consume_transaction(headers, state=state)

    async def _consume_transaction(
        self, headers: Mapping[str, str], *, state: str
    ) -> dict[str, Any]:
        if not _valid_token(state):
            raise AuthenticationFailedError("valid login callback is required")
        transaction_token = _required_cookie(headers, self.login_cookie_name)
        raw_transaction = await self._store.consume_login(transaction_token)
        if raw_transaction is None:
            raise AuthenticationFailedError("valid login callback is required")
        transaction = _login_transaction(raw_transaction)
        if not hmac.compare_digest(transaction["state"], state):
            raise AuthenticationFailedError("valid login callback is required")
        return transaction

    async def complete_login(
        self,
        headers: Mapping[str, str],
        *,
        code: str,
        state: str,
    ) -> BrowserLoginCompletion:
        """Consume before exchange so every callback, successful or not, is strictly one-shot."""
        if not _bounded_text(code, _MAX_CODE_BYTES) or not _valid_token(state):
            raise AuthenticationFailedError("valid login callback is required")
        transaction = await self._consume_transaction(headers, state=state)
        if self._provider == "google" and transaction["purpose"] == _REAUTH_PURPOSE:
            raise BrowserStepUpUnavailableError("Google destructive-action step-up is unavailable")

        bound_session_token: str | None = None
        bound_record: dict[str, Any] | None = None
        if transaction["purpose"] == _REAUTH_PURPOSE:
            bound_session_token, bound_record = await self._session(headers)
            if (
                not hmac.compare_digest(
                    _digest(bound_session_token),
                    transaction["bound_session_hash"],
                )
                or bound_record["tenant_id"] != transaction["tenant_id"]
                or not hmac.compare_digest(bound_record["subject"], transaction["subject"])
            ):
                raise AuthenticationFailedError("reauthentication session binding changed")

        identity_token = await self._exchange_code(code, transaction["verifier"])
        identity = await self._identity_verifier.verify_identity_token(
            identity_token,
            expected_nonce=transaction["nonce"],
            require_auth_time=transaction["purpose"] == _REAUTH_PURPOSE,
        )
        reauthenticated = transaction["purpose"] == _REAUTH_PURPOSE
        if reauthenticated:
            if (
                bound_session_token is None
                or bound_record is None
                or identity.tenant_id != bound_record["tenant_id"]
                or not hmac.compare_digest(identity.subject, bound_record["subject"])
                or not _auth_time_is_recent(
                    identity.authenticated_at,
                    max_age_seconds=_FORCED_REAUTH_CALLBACK_AGE_SECONDS,
                )
            ):
                raise AuthenticationFailedError("fresh provider authentication is required")
            if not await self._store.mark_recent_auth(
                bound_session_token,
                identity.tenant_id,
                identity.subject,
                int(wall_time()),
            ):
                raise AuthenticationFailedError("reauthentication session is no longer active")
        return BrowserLoginCompletion(
            identity=BrowserIdentity(
                identity.tenant_id,
                identity.subject,
                authenticated_at=identity.authenticated_at,
            ),
            return_to=transaction["return_to"],
            reauthenticated=reauthenticated,
        )

    async def issue_session(self, identity: BrowserIdentity) -> BrowserSessionCredentials:
        """Create a fixed-lifetime session; a collision retries without reusing either secret."""
        _validate_identity(identity)
        for _attempt in range(2):
            session_token, csrf_token = _random_token(), _random_token()
            payload = _json(
                {
                    "v": _STORE_VERSION,
                    "tenant_id": str(identity.tenant_id),
                    "subject": identity.subject,
                    "csrf_hash": _digest(csrf_token),
                    "recent_auth_at": None,
                    **(
                        {"oidc_provider": "google", "oidc_client_id": self._client_id}
                        if self._provider == "google"
                        else {}
                    ),
                }
            )
            if await self._store.create_session(
                session_token,
                payload,
                self.session_ttl_seconds,
                identity.tenant_id,
            ):
                return BrowserSessionCredentials(session_token, csrf_token)
        raise BrowserSessionUnavailableError("could not allocate browser session")

    async def resolve_tenant_id(self, headers: Mapping[str, str]) -> UUID:
        try:
            _, record = await self._session(headers)
        except BrowserSessionUnavailableError:
            raise
        except (AuthenticationFailedError, ValueError) as error:
            raise AuthenticationFailedError("valid tenant authentication is required") from error
        return cast("UUID", record["tenant_id"])

    async def verify_state_change(
        self,
        tenant_id: UUID,
        headers: Mapping[str, str],
    ) -> None:
        try:
            _, record = await self._session(headers)
            if record["tenant_id"] != tenant_id:
                raise ValueError("session tenant changed between authentication and CSRF checks")
            origin = _one_header(headers, "origin")
            if origin is None or not hmac.compare_digest(origin, self._trusted_origin):
                raise ValueError("request origin is not the configured product origin")
            cookie_token = _required_cookie(headers, self.csrf_cookie_name)
            header_token = _one_header(headers, self.csrf_header_name)
            if (
                header_token is None
                or not _valid_token(header_token)
                or not hmac.compare_digest(cookie_token, header_token)
                or not hmac.compare_digest(record["csrf_hash"], _digest(header_token))
            ):
                raise ValueError("CSRF token is not bound to this session")
        except BrowserSessionUnavailableError:
            raise
        except (AuthenticationFailedError, ValueError) as error:
            raise CsrfVerificationFailedError("valid state-change authority is required") from error

    async def revoke_session(self, headers: Mapping[str, str]) -> None:
        token = _optional_cookie(headers, self.session_cookie_name)
        if token is not None:
            await self._store.delete_session(token)

    async def verify_recent_auth(
        self,
        tenant_id: UUID,
        headers: Mapping[str, str],
        *,
        max_age_seconds: int,
    ) -> None:
        """Require one tenant-bound session backed by a recent provider ``auth_time`` claim."""
        if not 1 <= max_age_seconds <= _MAX_RECENT_AUTH_SECONDS:
            raise ValueError("recent authentication age must be between 1 and 900 seconds")
        if self._provider == "google":
            raise BrowserStepUpUnavailableError(
                "Google account deletion requires a separately configured step-up method"
            )
        try:
            _, record = await self._session(headers)
            if record["tenant_id"] != tenant_id or not _auth_time_is_recent(
                record["recent_auth_at"],
                max_age_seconds=max_age_seconds,
            ):
                raise ValueError("session is not backed by recent provider authentication")
        except BrowserSessionUnavailableError:
            raise
        except (AuthenticationFailedError, ValueError) as error:
            raise RecentAuthenticationRequiredError(
                "recent provider authentication is required"
            ) from error

    async def revoke_tenant_sessions(self, tenant_id: UUID) -> None:
        """Fence issuance and revoke all concurrent sessions as one Redis operation."""
        await self._store.revoke_tenant_sessions(tenant_id)

    async def is_ready(self) -> bool:
        if not await self._store.is_ready():
            return False
        if self._provider == "google":
            return self._google_identity_ready is not None and await self._google_identity_ready()
        return True

    async def aclose(self) -> None:
        await self._store.aclose()
        if self._owns_http:
            await self._http.aclose()

    async def _session(
        self,
        headers: Mapping[str, str],
    ) -> tuple[str, dict[str, Any]]:
        session_token = _required_cookie(headers, self.session_cookie_name)
        raw_session = await self._store.get_session(session_token)
        if raw_session is None:
            raise AuthenticationFailedError("valid tenant authentication is required")
        try:
            record = _session_record(raw_session)
        except ValueError:
            # Corrupt or incompatible records cannot remain repeatedly parseable authority.
            await self._store.delete_session(session_token)
            raise
        if self._provider == "google":
            if (
                record.get("oidc_provider") != "google"
                or record.get("oidc_client_id") != self._client_id
                or not record["subject"].startswith(GOOGLE_SUBJECT_PREFIX)
            ):
                raise AuthenticationFailedError("browser session identity authority changed")
        elif "oidc_provider" in record:
            raise AuthenticationFailedError("browser session identity authority changed")
        return session_token, record

    async def _exchange_code(self, code: str, verifier: str) -> str:
        try:
            response = await self._http.post(
                self._token_url,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self._redirect_uri,
                    "code_verifier": verifier,
                },
                auth=(self._client_id, self._client_secret),
                headers={"Accept": "application/json"},
            )
        except (httpx.RequestError, TimeoutError) as error:
            raise BrowserSessionUnavailableError("OIDC token endpoint unavailable") from error
        if response.status_code >= _SERVER_ERROR_STATUS:
            raise BrowserSessionUnavailableError("OIDC token endpoint unavailable")
        if (
            response.status_code != _SUCCESS_STATUS
            or len(response.content) > _MAX_TOKEN_RESPONSE_BYTES
        ):
            raise AuthenticationFailedError("valid login callback is required")
        try:
            payload = response.json()
            identity_token = payload["id_token"]
        except (KeyError, TypeError, ValueError) as error:
            raise AuthenticationFailedError("valid login callback is required") from error
        if not _bounded_text(identity_token, _MAX_ID_TOKEN_BYTES):
            raise AuthenticationFailedError("valid login callback is required")
        return cast("str", identity_token)


def _login_transaction(raw: str) -> dict[str, Any]:
    try:
        payload = _object(raw)
    except ValueError as error:
        raise AuthenticationFailedError("valid login callback is required") from error
    legacy_fields = {"v", "state", "nonce", "verifier", "return_to"}
    login_fields = legacy_fields | {"purpose"}
    reauthentication_fields = login_fields | {"bound_session_hash", "tenant_id", "subject"}
    payload_fields = frozenset(payload)
    if payload_fields not in {
        frozenset(legacy_fields),
        frozenset(login_fields),
        frozenset(reauthentication_fields),
    }:
        raise AuthenticationFailedError("valid login callback is required")
    if payload["v"] != _STORE_VERSION:
        raise AuthenticationFailedError("valid login callback is required")
    values = {name: payload[name] for name in ("state", "nonce", "verifier")}
    if not all(isinstance(value, str) and _valid_token(value) for value in values.values()):
        raise AuthenticationFailedError("valid login callback is required")
    return_to = payload["return_to"]
    if not isinstance(return_to, str):
        raise AuthenticationFailedError("valid login callback is required")
    try:
        safe_return = _safe_return_path(return_to)
    except ValueError as error:
        raise AuthenticationFailedError("valid login callback is required") from error
    purpose = payload.get("purpose", "login")
    if purpose not in {"login", _REAUTH_PURPOSE}:
        raise AuthenticationFailedError("valid login callback is required")
    transaction: dict[str, Any] = {
        **cast("dict[str, str]", values),
        "return_to": safe_return,
        "purpose": purpose,
    }
    if purpose == "login":
        if payload_fields == frozenset(reauthentication_fields):
            raise AuthenticationFailedError("valid login callback is required")
        return transaction
    if payload_fields != frozenset(reauthentication_fields):
        raise AuthenticationFailedError("valid login callback is required")
    transaction.update(_reauthentication_binding(payload))
    return transaction


def _reauthentication_binding(payload: dict[str, object]) -> dict[str, object]:
    """Validate the exact session/identity binding on a destructive-action transaction."""
    bound_session_hash = payload["bound_session_hash"]
    raw_tenant_id = payload["tenant_id"]
    subject = payload["subject"]
    if not isinstance(bound_session_hash, str) or len(bound_session_hash) != _SHA256_HEX_LENGTH:
        raise AuthenticationFailedError("valid login callback is required")
    try:
        bytes.fromhex(bound_session_hash)
        tenant_id = UUID(raw_tenant_id) if isinstance(raw_tenant_id, str) else None
    except ValueError as error:
        raise AuthenticationFailedError("valid login callback is required") from error
    if tenant_id is None or raw_tenant_id != str(tenant_id) or not _valid_subject(subject):
        raise AuthenticationFailedError("valid login callback is required")
    return {
        "bound_session_hash": bound_session_hash,
        "tenant_id": tenant_id,
        "subject": subject,
    }


def _session_record(raw: str) -> dict[str, Any]:
    payload = _object(raw)
    old_fields = {"v", "tenant_id", "subject", "csrf_hash"}
    intermediate_fields = old_fields | {"authenticated_at"}
    new_fields = old_fields | {"recent_auth_at"}
    google_fields = new_fields | {"oidc_provider", "oidc_client_id"}
    if frozenset(payload) not in {
        frozenset(old_fields),
        frozenset(intermediate_fields),
        frozenset(new_fields),
        frozenset(google_fields),
    }:
        raise ValueError("invalid browser session record")
    if payload["v"] != _STORE_VERSION:
        raise ValueError("invalid browser session record")
    raw_tenant_id, subject, csrf_hash = (
        payload["tenant_id"],
        payload["subject"],
        payload["csrf_hash"],
    )
    if not isinstance(raw_tenant_id, str):
        raise ValueError("invalid browser session record")
    tenant_id = UUID(raw_tenant_id)
    if raw_tenant_id != str(tenant_id):
        raise ValueError("invalid browser session record")
    if not _valid_subject(subject):
        raise ValueError("invalid browser session record")
    if not isinstance(csrf_hash, str) or len(csrf_hash) != _SHA256_HEX_LENGTH:
        raise ValueError("invalid browser session record")
    try:
        bytes.fromhex(csrf_hash)
    except ValueError as error:
        raise ValueError("invalid browser session record") from error
    recent_auth_at = payload.get("recent_auth_at")
    if recent_auth_at is not None and (
        isinstance(recent_auth_at, bool)
        or not isinstance(recent_auth_at, int)
        or recent_auth_at < 0
    ):
        raise ValueError("invalid browser session record")
    provider_fields: dict[str, str] = {}
    if frozenset(payload) == frozenset(google_fields):
        if payload["oidc_provider"] != "google" or not _bounded_text(
            payload["oidc_client_id"], _MAX_CLIENT_ID_BYTES
        ):
            raise ValueError("invalid browser session identity authority")
        provider_fields = {
            "oidc_provider": "google",
            "oidc_client_id": cast("str", payload["oidc_client_id"]),
        }
    return {
        "tenant_id": tenant_id,
        "subject": subject,
        "csrf_hash": csrf_hash,
        "recent_auth_at": recent_auth_at,
        **provider_fields,
    }


def _auth_time_is_recent(value: object, *, max_age_seconds: int) -> bool:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return False
    now = int(wall_time())
    return (
        value <= now + _AUTH_TIME_CLOCK_SKEW_SECONDS
        and now - value <= max_age_seconds + _AUTH_TIME_CLOCK_SKEW_SECONDS
    )


def _object(raw: str) -> dict[str, object]:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise ValueError("invalid browser session record") from error
    if not isinstance(payload, dict):
        raise ValueError("invalid browser session record")
    return cast("dict[str, object]", payload)


def _validate_identity(identity: BrowserIdentity) -> None:
    if not _valid_subject(identity.subject):
        raise ValueError("OIDC subject must be a bounded printable value")


def _valid_subject(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value.encode("utf-8")) <= _MAX_SUBJECT_BYTES
        and not any(category(character) in {"Cc", "Cf"} for character in value)
    )


def _safe_return_path(value: str) -> str:
    if (
        not _bounded_text(value, _MAX_RETURN_TO_BYTES)
        or not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(category(character) in {"Cc", "Cf"} for character in value)
    ):
        raise ValueError("return path must be a bounded same-origin application path")
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.path not in {"/", "/app"}:
        raise ValueError("return path must target the same-origin application shell")
    return value


def _required_cookie(headers: Mapping[str, str], name: str) -> str:
    value = _optional_cookie(headers, name)
    if value is None:
        raise AuthenticationFailedError("valid tenant authentication is required")
    return value


def _optional_cookie(headers: Mapping[str, str], name: str) -> str | None:
    raw = _one_header(headers, "cookie")
    if raw is None:
        return None
    if len(raw.encode("utf-8")) > _MAX_COOKIE_HEADER_BYTES:
        raise AuthenticationFailedError("valid tenant authentication is required")
    matches: list[str] = []
    for part in raw.split(";"):
        cookie_name, separator, value = part.strip().partition("=")
        if separator and cookie_name == name:
            matches.append(value)
    if not matches:
        return None
    if len(matches) != 1 or not _valid_token(matches[0]):
        raise AuthenticationFailedError("valid tenant authentication is required")
    return matches[0]


def _one_header(headers: Mapping[str, str], name: str) -> str | None:
    values = [
        value
        for key, value in headers.items()
        if isinstance(key, str) and key.casefold() == name.casefold() and isinstance(value, str)
    ]
    return values[0] if len(values) == 1 else None


def _random_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def _valid_token(value: str) -> bool:
    if len(value) != _TOKEN_LENGTH:
        return False
    try:
        decoded = base64.urlsafe_b64decode(f"{value}=")
    except (ValueError, TypeError):
        return False
    return len(decoded) == _TOKEN_BYTES and _base64url(decoded) == value


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _bounded_text(value: object, maximum_bytes: int) -> bool:
    return isinstance(value, str) and bool(value) and len(value.encode("utf-8")) <= maximum_bytes


def _json(value: Mapping[str, object]) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _https_url(value: str, label: str) -> str:
    if not _bounded_text(value, 2048):
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
        or parsed.fragment
        or (port is not None and not 1 <= port <= _MAX_URL_PORT)
        or any(category(character) in {"Cc", "Cf"} or character.isspace() for character in value)
    ):
        raise ValueError(f"OIDC {label} must be a bounded HTTPS URL")
    return value


def _origin(value: str) -> str:
    parsed = urlsplit(_https_url(value, "public origin"))
    if parsed.path not in {"", "/"} or parsed.query:
        raise ValueError("OIDC public origin must not include a path or query")
    return _url_origin(value)


def _url_origin(value: str) -> str:
    parsed = urlsplit(_https_url(value, "URL"))
    port = f":{parsed.port}" if parsed.port not in {None, _DEFAULT_HTTPS_PORT} else ""
    assert parsed.hostname is not None
    return f"https://{parsed.hostname.lower()}{port}"
