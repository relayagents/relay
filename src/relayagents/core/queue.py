"""Job-queue plumbing shared by relay-api (enqueue) and the workers (consume).

arq pickles jobs by default, which turns "can write to Redis" into "can run code in the workers".
Relay jobs only carry JSON-able arguments, so we serialize as JSON on both sides.
"""

from __future__ import annotations

import json
from typing import Any

from arq.connections import ArqRedis, RedisSettings, create_pool


def job_serializer(data: Any) -> bytes:
    return json.dumps(data, default=str).encode()


def job_deserializer(raw: bytes) -> Any:
    return json.loads(raw)


async def connect(redis_url: str) -> ArqRedis:
    return await create_pool(
        RedisSettings.from_dsn(redis_url),
        job_serializer=job_serializer,
        job_deserializer=job_deserializer,
    )
