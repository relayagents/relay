"""Raw event log access. Agents publish to Relay by appending events here."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from relayagents.api.auth import current_principal, get_services
from relayagents.core.events import EVENT_TYPES, Event
from relayagents.core.projections import apply as project
from relayagents.core.store import EventStore, parse_since
from relayagents.tools.context import Principal, Services

router = APIRouter(prefix="/v1/events", tags=["events"])


class AppendIn(BaseModel):
    """An event minus the fields Relay sets: id, ts, actor (from the token)."""

    type: str | None = None
    payload: dict[str, Any]
    source: str = "api"
    visibility: str = "team"
    thread_id: str | None = None
    provenance: dict[str, Any] | None = None


@router.post("", status_code=201)
async def append_event(
    body: AppendIn,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(get_services)],
) -> Event:
    if body.type and body.type not in EVENT_TYPES:
        raise HTTPException(400, f"unknown event type {body.type!r}")
    try:
        event = Event.model_validate(
            {
                "type": body.type,
                "payload": body.payload,
                "actor": principal.actor.model_dump(),
                "source": body.source,
                "visibility": body.visibility,
                "thread_id": body.thread_id,
                "provenance": body.provenance or {},
            }
        )
    except Exception as exc:
        raise HTTPException(422, str(exc)) from exc
    async with services.db.session() as session:
        await EventStore(session).append(event)
        await project(session, event)
        await session.commit()
    return event


@router.get("")
async def list_events(
    _: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(get_services)],
    since: str | None = None,
    type: Annotated[list[str] | None, Query()] = None,
    thread: str | None = None,
    actor: str | None = None,
    after_seq: int | None = None,
    limit: int = Query(default=100, le=1000),
) -> list[Event]:
    async with services.db.session() as session:
        return await EventStore(session).query(
            since=parse_since(since),
            types=type,
            thread_id=thread,
            actor_id=actor,
            after_seq=after_seq,
            limit=limit,
        )


@router.get("/types")
async def event_types() -> list[str]:
    return list(EVENT_TYPES)


@router.get("/{event_id}")
async def get_event(
    event_id: str,
    _: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(get_services)],
) -> Event:
    async with services.db.session() as session:
        ev = await EventStore(session).get(event_id)
    if ev is None:
        raise HTTPException(404, "no such event")
    return ev
