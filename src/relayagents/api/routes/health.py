from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy import text

from relayagents import __version__

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    services = request.app.state.services
    db_ok = False
    try:
        async with services.db.session() as session:
            await session.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        pass
    redis_ok: bool | None = None
    redis = getattr(request.app.state, "redis", None)
    if redis is not None:
        try:
            redis_ok = bool(await redis.ping())
        except Exception:
            redis_ok = False
    status = "ok" if db_ok and redis_ok is not False else "degraded"
    return {
        "status": status,
        "version": __version__,
        # db/redis are live probes (a query, a PING); the fields below only reflect
        # whether the connector was constructed from settings, never whether it is
        # reachable. Naming them *_configured keeps that distinction honest, and an
        # unreachable optional connector deliberately does not affect `status`.
        "db": db_ok,
        "redis": redis_ok,
        "slack_configured": services.chat is not None,
        "workspace_mcp_configured": services.office is not None,
        "semantic_recall_configured": services.semantic is not None,
    }
