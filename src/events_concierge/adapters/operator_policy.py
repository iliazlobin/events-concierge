"""Versioned operator authorization from Parameter Manager, with bounded fail-closed caching.

Only non-secret JSON is read. Cloud credentials come from Application Default Credentials;
the deployment supplies a fixed global parameter version, never a request-controlled URL.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from time import monotonic
from types import MappingProxyType
from typing import Literal, Protocol, cast

import google.auth
import httpx
from google.auth.credentials import Credentials
from google.auth.transport import Request as GoogleTransportRequest
from google.auth.transport.requests import Request as GoogleAuthRequest

OperatorRole = Literal["viewer", "operator", "reviewer"]
_MAX_POLICY_BYTES = 65_536
_MAX_RESPONSE_BYTES = 131_072
_FETCH_SECONDS = 5.0
_MAX_BINDINGS = 100
_MAX_SUBJECT_LENGTH = 200
_MIN_EMAIL_LENGTH = 3
_MAX_EMAIL_LENGTH = 320
_MAX_CACHE_SECONDS = 60
_RESOURCE = re.compile(
    r"projects/[1-9][0-9]{0,19}/locations/global/"
    r"parameters/[A-Za-z0-9_][A-Za-z0-9_-]{0,62}/versions/[A-Za-z0-9_][A-Za-z0-9_-]{0,62}"
)


class OperatorPolicyUnavailableError(Exception):
    """Public error deliberately excludes parameter contents, identities and SDK details."""

    def __init__(self) -> None:
        super().__init__("operator authorization policy unavailable")


class OperatorPolicySource(Protocol):
    async def role_for(self, subject: str, email: object) -> OperatorRole | None: ...

    async def ready(self) -> bool: ...


@dataclass(frozen=True, slots=True, repr=False)
class OperatorBinding:
    email: str
    role: OperatorRole


@dataclass(frozen=True, slots=True, repr=False)
class OperatorPolicy:
    bindings: Mapping[str, OperatorBinding]

    def role_for(self, subject: str, email: object) -> OperatorRole | None:
        binding = self.bindings.get(subject)
        return binding.role if binding is not None and email == binding.email else None


def validate_operator_policy_version(version: str) -> str:
    """Require one explicit immutable global version, excluding moving aliases."""
    if not _RESOURCE.fullmatch(version) or version.rsplit("/", 1)[1].lower() == "latest":
        raise ValueError("operator policy requires an explicit global parameter version")
    return version


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise OperatorPolicyUnavailableError()
        result[key] = value
    return result


def _json(data: bytes) -> object:
    return json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)


def parse_operator_policy(data: bytes) -> OperatorPolicy:
    """Validate schema and identity bounds without exposing validation input in errors."""
    try:
        if not data or len(data) > _MAX_POLICY_BYTES:
            raise OperatorPolicyUnavailableError()
        payload = _json(data)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema", "bindings"}
            or type(payload["schema"]) is not int
            or payload["schema"] != 1
            or not isinstance(payload["bindings"], list)
            or len(payload["bindings"]) > _MAX_BINDINGS
        ):
            raise OperatorPolicyUnavailableError()
        bindings: dict[str, OperatorBinding] = {}
        for item in payload["bindings"]:
            if not isinstance(item, dict) or set(item) != {"subject", "email", "role"}:
                raise OperatorPolicyUnavailableError()
            subject, email, role = item["subject"], item["email"], item["role"]
            if (
                not isinstance(subject, str)
                or not 1 <= len(subject) <= _MAX_SUBJECT_LENGTH
                or not subject.isascii()
                or not subject.isprintable()
                or any(char.isspace() for char in subject)
                or subject in bindings
                or not isinstance(email, str)
                or not _MIN_EMAIL_LENGTH <= len(email) <= _MAX_EMAIL_LENGTH
                or not re.fullmatch(r"[^\s@]+@[^\s@]+", email, flags=re.ASCII)
                or not email.isascii()
                or not email.isprintable()
                or role not in ("viewer", "operator", "reviewer")
            ):
                raise OperatorPolicyUnavailableError()
            bindings[subject] = OperatorBinding(email, cast("OperatorRole", role))
        return OperatorPolicy(MappingProxyType(bindings))
    except (ValueError, TypeError, UnicodeError, RecursionError, OperatorPolicyUnavailableError):
        raise OperatorPolicyUnavailableError() from None


class StaticOperatorPolicy:
    """Explicit local compatibility; production composition uses Parameter Manager."""

    def __init__(
        self, subject_roles: Mapping[str, OperatorRole], allowed_email: str | None = None
    ) -> None:
        self._roles = dict(subject_roles)
        self._email = allowed_email

    async def role_for(self, subject: str, email: object) -> OperatorRole | None:
        if self._email is not None and email != self._email:
            return None
        return self._roles.get(subject)

    async def ready(self) -> bool:
        return True


class _DeadlineAuthRequest:
    """Bound ADC discovery/refresh transports to the same fetch deadline."""

    def __init__(self, deadline: float) -> None:
        self._deadline = deadline
        self._request = GoogleAuthRequest()

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = _FETCH_SECONDS,
        **kwargs: object,
    ) -> object:
        remaining = self._deadline - monotonic()
        if remaining <= 0:
            raise OperatorPolicyUnavailableError()
        return self._request(
            url,
            method=method,
            body=body,
            headers=headers,
            timeout=min(timeout, remaining),
            **kwargs,
        )


class ParameterManagerOperatorPolicy:
    """Coalesce refreshes; expired or disabled versions never reuse old grants.

    GET FULL avoids Secret Manager rendering and validates the version's disabled state.
    A disabled version or failed refresh takes effect within the configured cache lifetime.
    """

    def __init__(
        self,
        version: str,
        cache_seconds: int = 30,
        *,
        fetch: Callable[[str], bytes] | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.version = validate_operator_policy_version(version)
        if type(cache_seconds) is not int or not 1 <= cache_seconds <= _MAX_CACHE_SECONDS:
            raise ValueError("operator policy cache must be between one and 60 seconds")
        self._cache_seconds = cache_seconds
        self._clock = clock
        self._fetch = fetch or self._fetch_version
        self._credentials: Credentials | None = None
        self._cached: OperatorPolicy | None = None
        self._expires = 0.0
        self._retry_after = 0.0
        self._inflight: asyncio.Task[OperatorPolicy] | None = None

    def _fetch_version(self, version: str) -> bytes:
        deadline = monotonic() + _FETCH_SECONDS
        auth_request = _DeadlineAuthRequest(deadline)
        if self._credentials is None:
            self._credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
                request=cast("GoogleTransportRequest", auth_request),
            )
        url = "https://parametermanager.googleapis.com/v1/" + version
        headers: dict[str, str] = {}
        self._credentials.before_request(auth_request, "GET", url, headers)  # type: ignore[no-untyped-call]
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise OperatorPolicyUnavailableError()
        with (
            httpx.Client(timeout=remaining, follow_redirects=False) as client,
            client.stream("GET", url, headers=headers, params={"view": "FULL"}) as response,
        ):
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > _MAX_RESPONSE_BYTES or monotonic() >= deadline:
                    raise OperatorPolicyUnavailableError()
        if monotonic() >= deadline:
            raise OperatorPolicyUnavailableError()
        document = _json(bytes(body))
        if (
            not isinstance(document, dict)
            or document.get("name") != version
            or document.get("disabled", False) is not False
            or not isinstance(document.get("payload"), dict)
        ):
            raise OperatorPolicyUnavailableError()
        encoded = document["payload"].get("data")
        if not isinstance(encoded, str) or len(encoded) > _MAX_RESPONSE_BYTES:
            raise OperatorPolicyUnavailableError()
        try:
            return base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise OperatorPolicyUnavailableError() from None

    async def _refresh(self) -> OperatorPolicy:
        started = self._clock()
        try:
            data = await asyncio.to_thread(self._fetch, self.version)
            elapsed = self._clock() - started
            if elapsed > _FETCH_SECONDS or elapsed >= self._cache_seconds:
                raise OperatorPolicyUnavailableError()
            policy = parse_operator_policy(data)
        except Exception:
            self._cached = None
            self._retry_after = self._clock() + 1.0
            raise OperatorPolicyUnavailableError() from None
        self._cached = policy
        # Bound freshness from the start of the read, including cloud round-trip time.
        self._expires = started + self._cache_seconds
        return policy

    def _completed(self, task: asyncio.Task[OperatorPolicy]) -> None:
        # A disconnected caller must not cancel a shared refresh or leave an unobserved error.
        if not task.cancelled():
            task.exception()
        if self._inflight is task:
            self._inflight = None

    async def _policy(self) -> OperatorPolicy:
        if self._cached is not None and self._clock() < self._expires:
            return self._cached
        if self._clock() < self._retry_after:
            raise OperatorPolicyUnavailableError()
        if self._inflight is None:
            self._inflight = asyncio.create_task(self._refresh())
            self._inflight.add_done_callback(self._completed)
        try:
            return await asyncio.wait_for(asyncio.shield(self._inflight), timeout=_FETCH_SECONDS)
        except (TimeoutError, OperatorPolicyUnavailableError):
            raise OperatorPolicyUnavailableError() from None

    async def role_for(self, subject: str, email: object) -> OperatorRole | None:
        return (await self._policy()).role_for(subject, email)

    async def ready(self) -> bool:
        try:
            await self._policy()
        except OperatorPolicyUnavailableError:
            return False
        return True
