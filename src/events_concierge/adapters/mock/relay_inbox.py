"""Fixture-only deterministic RelayInbox ingress (FR-2.6, FR-5.7/5.8, ADR-011).

This adapter is deliberately local and provider-free.  It parses bounded fixture MIME with the
standard library, strips hostile HTML without exposing attributes, allows only configured fixture
sender/link domains, and publishes an opaque capability reference.  It has no relay-domain,
network, workflow-signal, browser, LLM, notification, or plaintext-secret surface.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from enum import StrEnum
from html.parser import HTMLParser
from urllib.parse import urlsplit
from uuid import UUID

from ...domain.enums import Source
from ...ports.email import (
    RelaySecretReferencePublisherPort,
    RelaySecretReferenceStorePort,
    RelaySecretReservation,
)

_MAX_RAW_MIME_BYTES = 64 * 1024
_MAX_VISIBLE_TEXT_BYTES = 16 * 1024
_MIN_OTP_LENGTH = 4
_MAX_OTP_LENGTH = 12
_MAX_HOST_LENGTH = 253
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_LOCAL_PART = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+$")
_HTTPS_URL = re.compile(r"https://[^\s<>\"']+", re.IGNORECASE)
_URL_TRAILING_PUNCTUATION = ".,;:!?)]}"


class RelayIngressDisposition(StrEnum):
    """Safe, secret-free outcome of one local fixture delivery."""

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"


class RelayArtifactKind(StrEnum):
    """The class of login artifact found locally, never its value."""

    OTP = "otp"
    MAGIC_LINK = "magic_link"


@dataclass(frozen=True, slots=True)
class RelayFixtureEnvelope:
    """Trusted transport metadata accepted only by the fixture ingress adapter (ADR-011)."""

    delivery_id: UUID
    recipient: str
    envelope_sender: str
    received_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.delivery_id, UUID):
            raise ValueError("relay fixture delivery_id must be a UUID")
        if not isinstance(self.recipient, str) or not self.recipient.strip():
            raise ValueError("relay fixture recipient must not be empty")
        if not isinstance(self.envelope_sender, str) or not self.envelope_sender.strip():
            raise ValueError("relay fixture envelope_sender must not be empty")
        if not _is_aware(self.received_at):
            raise ValueError("relay fixture received_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class RelayExtractorRule:
    """One deterministic fixture-only sender, OTP, and magic-link extraction rule (ADR-011)."""

    source: Source
    allowed_sender_domains: tuple[str, ...]
    otp_length: int
    magic_link_hosts: tuple[str, ...]
    ttl: timedelta

    def __post_init__(self) -> None:
        if not isinstance(self.source, Source):
            raise ValueError("relay extractor rule source must be a Source")
        if (
            isinstance(self.otp_length, bool)
            or not isinstance(self.otp_length, int)
            or not _MIN_OTP_LENGTH <= self.otp_length <= _MAX_OTP_LENGTH
        ):
            raise ValueError("relay extractor rule otp_length must be between 4 and 12")
        if not isinstance(self.ttl, timedelta) or self.ttl <= timedelta():
            raise ValueError("relay extractor rule ttl must be positive")

        sender_domains = _normalise_rule_hosts(self.allowed_sender_domains)
        if not sender_domains:
            raise ValueError("relay extractor rule needs an allowed sender domain")
        magic_hosts = _normalise_rule_hosts(self.magic_link_hosts)
        object.__setattr__(self, "allowed_sender_domains", sender_domains)
        object.__setattr__(self, "magic_link_hosts", magic_hosts)


@dataclass(frozen=True, slots=True)
class RelayIngressResult:
    """Secret-free ingress result; no raw body, code, link, address, error, or reference crosses it."""

    disposition: RelayIngressDisposition
    source: Source | None
    artifact_kind: RelayArtifactKind | None

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, RelayIngressDisposition):
            raise ValueError("relay ingress result disposition must be a RelayIngressDisposition")
        if self.disposition is RelayIngressDisposition.REJECTED:
            if self.source is not None or self.artifact_kind is not None:
                raise ValueError("a rejected relay ingress result carries no source or artifact")
            return
        if not isinstance(self.source, Source) or not isinstance(
            self.artifact_kind, RelayArtifactKind
        ):
            raise ValueError("an accepted relay ingress result needs source and artifact metadata")


@dataclass(frozen=True, slots=True)
class _FixtureIngressContext:
    """Validated, secret-free metadata needed for a fixture capability reservation."""

    tenant_id: UUID
    sender_domain: str
    rule: RelayExtractorRule
    expires_at: datetime
    artifact_kind: RelayArtifactKind


class FixtureRelayInboxIngress:
    """Deterministic local ingress for sanitized fixtures, not a production RelayInbox (ADR-011).

    The implementation fails closed before publishing on unknown recipients, spoofed senders,
    malformed MIME, expired deliveries, non-visible HTML, unsupported links, and ambiguous
    artifacts.  It retains only the artifact class locally long enough to choose a safe result.
    """

    def __init__(
        self,
        *,
        recipient_bindings: Mapping[str, UUID],
        rules: Iterable[RelayExtractorRule],
        secret_store: RelaySecretReferenceStorePort,
        publisher: RelaySecretReferencePublisherPort,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._recipient_bindings = _normalise_recipient_bindings(recipient_bindings)
        self._rules = tuple(rules)
        if not self._rules:
            raise ValueError("fixture RelayInbox ingress needs at least one extractor rule")
        if any(not isinstance(rule, RelayExtractorRule) for rule in self._rules):
            raise ValueError("fixture RelayInbox ingress rules must be RelayExtractorRule values")
        if len({rule.source for rule in self._rules}) != len(self._rules):
            raise ValueError("fixture RelayInbox ingress has duplicate source rules")
        self._secret_store = secret_store
        self._publisher = publisher
        self._now = now or _utc_now

    async def ingest(self, envelope: RelayFixtureEnvelope, raw_mime: bytes) -> RelayIngressResult:
        """Classify and hand off one fixture message as an opaque reference, or reject it safely."""
        context = self._prepare_ingress(envelope, raw_mime)
        if context is None:
            return _rejected()
        reservation = self._reserve(envelope.delivery_id, context)
        if reservation is None:
            return _rejected()
        published = await self._publish(reservation)
        if not published:
            if not reservation.replayed:
                self._secret_store.discard_secret_reference(reservation.reference.secret_ref)
            return _rejected()

        return RelayIngressResult(
            disposition=(
                RelayIngressDisposition.DUPLICATE
                if reservation.replayed
                else RelayIngressDisposition.ACCEPTED
            ),
            source=context.rule.source,
            artifact_kind=context.artifact_kind,
        )

    def _prepare_ingress(
        self, envelope: RelayFixtureEnvelope, raw_mime: bytes
    ) -> _FixtureIngressContext | None:
        """Validate only safe transport metadata and choose exactly one local artifact class."""
        if (
            not isinstance(envelope, RelayFixtureEnvelope)
            or not isinstance(raw_mime, bytes)
            or not raw_mime
            or len(raw_mime) > _MAX_RAW_MIME_BYTES
        ):
            return None
        now = self._now()
        recipient = _normalise_mailbox(envelope.recipient)
        sender_domain = _mailbox_domain(envelope.envelope_sender)
        if not _is_aware(now) or recipient is None or sender_domain is None:
            return None
        tenant_id = self._recipient_bindings.get(recipient)
        rule = _matching_rule(sender_domain, self._rules)
        if tenant_id is None or rule is None:
            return None
        expires_at = envelope.received_at + rule.ttl
        if expires_at <= now:
            return None
        artifact_kind = _extract_artifact_kind(raw_mime, rule)
        if artifact_kind is None:
            return None
        return _FixtureIngressContext(
            tenant_id=tenant_id,
            sender_domain=sender_domain,
            rule=rule,
            expires_at=expires_at,
            artifact_kind=artifact_kind,
        )

    def _reserve(
        self, delivery_id: UUID, context: _FixtureIngressContext
    ) -> RelaySecretReservation | None:
        """Contain storage failures inside the fixture ingress without exposing attacker input."""
        try:
            return self._secret_store.reserve_secret_reference(
                delivery_id,
                context.tenant_id,
                context.rule.source,
                context.sender_domain,
                context.expires_at,
            )
        except Exception:
            return None

    async def _publish(self, reservation: RelaySecretReservation) -> bool:
        """Contain opaque-channel failures and leave the public result secret-free."""
        try:
            return await self._publisher.publish_secret_reference(reservation.reference)
        except Exception:
            return False


class _VisibleTextExtractor(HTMLParser):
    """Minimal HTML-to-visible-text adapter that discards script/style contents and all attributes."""

    _HIDDEN_TAGS = frozenset({"script", "style", "template", "noscript", "svg"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._hidden_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.casefold() in self._HIDDEN_TAGS:
            self._hidden_depth += 1
        elif self._hidden_depth == 0:
            self._chunks.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del tag, attrs
        if self._hidden_depth == 0:
            self._chunks.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in self._HIDDEN_TAGS and self._hidden_depth > 0:
            self._hidden_depth -= 1
        elif self._hidden_depth == 0:
            self._chunks.append(" ")

    def handle_data(self, data: str) -> None:
        if self._hidden_depth == 0:
            self._chunks.append(data)

    def visible_text(self) -> str:
        """Return bounded text only; attributes and hidden content never enter the candidate scan."""
        text = "".join(self._chunks)
        if len(text.encode("utf-8", errors="ignore")) > _MAX_VISIBLE_TEXT_BYTES:
            return ""
        return text


def _extract_artifact_kind(raw_mime: bytes, rule: RelayExtractorRule) -> RelayArtifactKind | None:
    """Return exactly one locally observed artifact class, never the artifact value (ADR-011)."""
    text_parts = _visible_text_parts(raw_mime)
    if text_parts is None:
        return None

    candidates: set[tuple[RelayArtifactKind, str]] = set()
    for text in text_parts:
        candidates.update(_otp_candidates(text, rule.otp_length))
        candidates.update(_magic_link_candidates(text, rule.magic_link_hosts))
    if len(candidates) != 1:
        candidates.clear()
        return None
    artifact_kind = next(iter(candidates))[0]
    candidates.clear()
    return artifact_kind


def _visible_text_parts(raw_mime: bytes) -> tuple[str, ...] | None:
    """Parse only bounded visible text leaves; raw MIME and attachments remain inside this adapter."""
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw_mime)
    except (UnicodeError, ValueError):
        return None
    if message.get("Content-Type") is None:
        return None

    text_parts: list[str] = []
    total_bytes = 0
    try:
        for part in message.walk():
            if part.is_multipart() or part.get_content_disposition() == "attachment":
                continue
            content_type = part.get_content_type().casefold()
            if content_type not in {"text/plain", "text/html"}:
                continue
            content = part.get_content()
            if not isinstance(content, str):
                continue
            if content_type == "text/html":
                extractor = _VisibleTextExtractor()
                extractor.feed(content)
                extractor.close()
                content = extractor.visible_text()
            size = len(content.encode("utf-8", errors="ignore"))
            total_bytes += size
            if not content or total_bytes > _MAX_VISIBLE_TEXT_BYTES:
                return None
            text_parts.append(content)
    except (LookupError, UnicodeError, ValueError):
        return None
    return tuple(text_parts) if text_parts else None


def _otp_candidates(text: str, otp_length: int) -> set[tuple[RelayArtifactKind, str]]:
    """Find context-bound numeric OTPs without treating arbitrary numbers as credentials."""
    expression = re.compile(
        rf"\b(?:one[- ]?time\s+)?(?:login\s+)?(?:code|otp|verification(?:\s+code)?)\b"
        rf"(?:\s*(?:is|:|-))?\s*([0-9]{{{otp_length}}})\b",
        re.IGNORECASE,
    )
    return {(RelayArtifactKind.OTP, match.group(1)) for match in expression.finditer(text)}


def _magic_link_candidates(
    text: str, allowed_hosts: tuple[str, ...]
) -> set[tuple[RelayArtifactKind, str]]:
    """Find only explicit visible HTTPS URLs on an exact or subdomain allowlist (ADR-011)."""
    candidates: set[tuple[RelayArtifactKind, str]] = set()
    for match in _HTTPS_URL.finditer(text):
        value = match.group(0).rstrip(_URL_TRAILING_PUNCTUATION)
        try:
            parsed = urlsplit(value)
            host = parsed.hostname
            port = parsed.port
        except ValueError:
            continue
        if (
            parsed.scheme.casefold() != "https"
            or host is None
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
        ):
            continue
        try:
            normalised_host = _normalise_host(host)
        except ValueError:
            continue
        if any(_matches_domain(normalised_host, allowed_host) for allowed_host in allowed_hosts):
            candidates.add((RelayArtifactKind.MAGIC_LINK, value))
    return candidates


def _matching_rule(
    sender_domain: str, rules: tuple[RelayExtractorRule, ...]
) -> RelayExtractorRule | None:
    """Resolve exactly one source rule; domain suffix lookalikes are never matches."""
    matches = [
        rule
        for rule in rules
        if any(
            _matches_domain(sender_domain, allowed_domain)
            for allowed_domain in rule.allowed_sender_domains
        )
    ]
    return matches[0] if len(matches) == 1 else None


def _normalise_recipient_bindings(bindings: Mapping[str, UUID]) -> dict[str, UUID]:
    """Validate fixed fixture recipient-to-tenant bindings at construction time."""
    normalised: dict[str, UUID] = {}
    for recipient, tenant_id in bindings.items():
        mailbox = _normalise_mailbox(recipient)
        if mailbox is None or not isinstance(tenant_id, UUID) or mailbox in normalised:
            raise ValueError(
                "fixture RelayInbox bindings must be unique valid recipient UUID pairs"
            )
        normalised[mailbox] = tenant_id
    return normalised


def _normalise_rule_hosts(hosts: tuple[str, ...]) -> tuple[str, ...]:
    """Canonicalize a fixture domain allowlist and reject ambiguous repeated entries."""
    normalised = tuple(_normalise_host(host) for host in hosts)
    if len(set(normalised)) != len(normalised):
        raise ValueError("relay extractor rule host allowlist must not contain duplicates")
    return normalised


def _normalise_mailbox(value: str) -> str | None:
    """Parse a bare SMTP envelope mailbox without accepting display-name or address-list syntax."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    _display_name, address = parseaddr(stripped)
    if not address or address != stripped:
        return None
    local, separator, domain = address.rpartition("@")
    if not separator or not local or not _LOCAL_PART.fullmatch(local):
        return None
    try:
        return f"{local.casefold()}@{_normalise_host(domain)}"
    except ValueError:
        return None


def _mailbox_domain(value: str) -> str | None:
    """Extract a normalized sender domain only from a strict envelope mailbox."""
    mailbox = _normalise_mailbox(value)
    if mailbox is None:
        return None
    return mailbox.rsplit("@", maxsplit=1)[1]


def _normalise_host(value: str) -> str:
    """Normalize a DNS hostname for fixture allowlisting, rejecting wildcard and suffix tricks."""
    if not isinstance(value, str):
        raise ValueError("relay host must be text")
    host = value.strip().casefold().rstrip(".")
    labels = host.split(".")
    if (
        not host
        or len(host) > _MAX_HOST_LENGTH
        or any(not _HOST_LABEL.fullmatch(label) for label in labels)
    ):
        raise ValueError("relay host must be a concrete DNS hostname")
    return host


def _matches_domain(actual: str, allowed: str) -> bool:
    """Accept an exact domain or a true subdomain, never a string-suffix lookalike."""
    return actual == allowed or actual.endswith(f".{allowed}")


def _is_aware(value: datetime) -> bool:
    """Keep fixture TTL comparisons fail-closed on a malformed clock or timestamp."""
    return value.tzinfo is not None and value.utcoffset() is not None


def _utc_now() -> datetime:
    """Supply an aware UTC clock when a deterministic fixture clock is not injected."""
    return datetime.now(UTC)


def _rejected() -> RelayIngressResult:
    """Return the sole externally visible failure shape, with no attacker-controlled detail."""
    return RelayIngressResult(
        disposition=RelayIngressDisposition.REJECTED,
        source=None,
        artifact_kind=None,
    )
