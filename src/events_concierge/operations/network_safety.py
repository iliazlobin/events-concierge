"""Pure host classification shared by credential-bearing operations checks."""

from __future__ import annotations

import ipaddress

_MANAGED_PRIVATE_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)


def is_loopback_host(host: str | None) -> bool:
    """Return whether a host is unambiguously constrained to the local machine."""
    normalized = _normalized_host(host)
    if normalized is None:
        return False
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    address = _ip_address(normalized)
    if address is None:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return address.is_loopback or (mapped is not None and mapped.is_loopback)


def is_non_remote_host(host: str | None) -> bool:
    """Reject local/special hosts while retaining private managed-network addresses."""
    normalized = _normalized_host(host)
    if normalized is None:
        return True
    if (
        normalized in {"localhost", "postgres", "redis", "temporal"}
        or normalized.endswith(".localhost")
        or normalized.endswith(".local")
    ):
        return True
    address = _ip_address(normalized)
    if address is None:
        # Numeric legacy forms such as 2130706433 or 0177.0.0.1 may be interpreted as IPv4 by
        # resolvers even though ipaddress intentionally rejects them.
        compact = normalized.replace(".", "")
        return compact.isdigit() or (
            normalized.startswith("0x")
            and all(character in "0123456789abcdef" for character in normalized[2:])
        )
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    if any(address in network for network in _MANAGED_PRIVATE_NETWORKS):
        return False
    return bool(
        address.is_loopback
        or address.is_unspecified
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or not address.is_global
    )


def _normalized_host(host: str | None) -> str | None:
    if host is None:
        return None
    normalized = host.rstrip(".").casefold()
    return normalized or None


def _ip_address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None
