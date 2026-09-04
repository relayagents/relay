"""Standup submission (slice 2). The agent drafts, Relay applies the user's posting mode."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException

from relayagents.api.auth import current_principal, get_services
from relayagents.tools.context import Principal, Services
from relayagents.workers.standup import StandupDraft, gather, submit

router = APIRouter(prefix="/v1/standups", tags=["standups"])


@router.get("/draft")
async def draft(
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(get_services)],
    hours: int = 24,
) -> StandupDraft:
    async with services.db.session() as session:
        return await gather(session, principal.user_id, hours=hours)


@router.post("")
async def post_standup(
    body: StandupDraft,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(get_services)],
) -> dict[str, Any]:
    if body.user_id != principal.user_id:
        raise HTTPException(403, "you can only submit your own standup")
    return await submit(services, body, actor=principal.actor)
