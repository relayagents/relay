"""Mask secrets before anything is stored or shown. Leaf module: depends on nothing in Relay."""

from __future__ import annotations

import re
from typing import Any

_SENSITIVE_KEYS = (
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "private_key",
)
# Token *shapes* only: a bearer credential, Relay/OpenAI/Slack/GitHub token prefixes. Plain words
# such as "the bearer of bad news" must survive, since redaction is applied to stored text.
_SENSITIVE_VALUE = re.compile(
    r"(?i)(?:\bbearer\s+[A-Za-z0-9._~+/=-]{16,}|\brly_[A-Za-z0-9_-]{20,}|\bsk-[A-Za-z0-9_-]{16,}|\bxox[abp]-[A-Za-z0-9-]{10,}|\bgh[pous]_[A-Za-z0-9]{20,})"
)


def redact(value: Any) -> Any:
    """Recursively mask secret-looking keys and token-shaped strings."""
    if isinstance(value, dict):
        return {
            k: ("***" if any(s in str(k).lower() for s in _SENSITIVE_KEYS) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _SENSITIVE_VALUE.sub("***", value)
    return value
