"""Time-sortable identifiers.

Relay uses ULID-style ids (Crockford base32, 48-bit millisecond timestamp + 80 random bits)
so that ids sort roughly by creation time in logs and tables. A short type prefix makes ids
self-describing in transcripts and Slack messages (``evt_``, ``usr_``, ``tok_`` ...).
"""

from __future__ import annotations

import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def ulid(ts_ms: int | None = None) -> str:
    """Return a 26-character ULID string."""
    ts = int(time.time() * 1000) if ts_ms is None else ts_ms
    rand = int.from_bytes(os.urandom(10), "big")
    return _encode(ts, 10) + _encode(rand, 16)


def new_id(prefix: str) -> str:
    """Return a prefixed ULID such as ``evt_01J...``."""
    return f"{prefix}_{ulid()}"


def is_valid_id(value: str, prefix: str | None = None) -> bool:
    if "_" not in value:
        return False
    p, body = value.rsplit("_", 1)
    if prefix is not None and p != prefix:
        return False
    return len(body) == 26 and all(c in _ALPHABET for c in body)
