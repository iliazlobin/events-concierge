"""Credential-bearing operations reject local and special-network destinations."""

from __future__ import annotations

import pytest

from events_concierge.operations.network_safety import (
    is_loopback_host,
    is_non_remote_host,
)


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "app.localhost",
        "0.0.0.0",
        "::",
        "127.0.0.1",
        "::1",
        "::ffff:127.0.0.1",
        "169.254.10.20",
        "fe80::1",
        "224.0.0.1",
        "ff02::1",
        "192.0.2.1",
        "2130706433",
        "0177.0.0.1",
    ],
)
def test_special_or_local_hosts_are_not_remote(host: str) -> None:
    assert is_non_remote_host(host) is True


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "app.localhost",
        "127.0.0.1",
        "::1",
        "::ffff:127.0.0.1",
    ],
)
def test_loopback_hosts_are_accepted_for_local_canaries(host: str) -> None:
    assert is_loopback_host(host) is True


@pytest.mark.parametrize(
    "host",
    [
        "database.internal.example",
        "10.20.30.40",
        "172.20.30.40",
        "192.168.20.30",
        "fd00::1234",
    ],
)
def test_private_managed_network_hosts_remain_remote_eligible(host: str) -> None:
    assert is_non_remote_host(host) is False
