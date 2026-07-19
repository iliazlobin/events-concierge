"""Minimal ULID (Crockford base32, lexicographically sortable by time). Used for handoff task ids.

Kept out of the domain (which is pure) -- ULID generation reads the clock and randomness."""

from __future__ import annotations

import os
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid() -> str:
    ms = int(time.time() * 1000)
    rand = os.urandom(10)
    value = (ms << 80) | int.from_bytes(rand, "big")
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))
