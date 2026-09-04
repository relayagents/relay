"""HTTP surface of the broker.

* ``GET  /a2a/agents``                      list registered AgentCards
* ``POST /a2a/agents``                      register/update my agent's card (+ optional push_url)
* ``GET  /a2a/agents/{id}/.well-known/agent-card.json``  the card
* ``POST /a2a/agents/{id}``                 JSON-RPC ``message/send`` / ``tasks/get`` addressed to that agent
* ``GET  /a2a/inbox``                       long-poll my agent's inbox (``?wait=25``)
* ``POST /a2a/tasks/{id}``                  my agent reports state/messages/artifacts on a task
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from relayagents.api.a2a_broker import broker
from relayagents.api.a2a_broker.types import AgentCard, Message, SendMessageParams, Task, TaskUpdate
from relayagents.api.auth import current_principal, get_services
from relayagents.tools.context import Principal, Services

router = APIRouter(prefix="/a2a", tags=["a2a"])


class RegisterIn(BaseModel):
    agent_id: str | None = None
    harness: str = "custom"
    card: AgentCard
    push_url: str | None = None


@router.post("/agents", status_code=201)
async def register(
    body: RegisterIn,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(get_services)],
) -> dict[str, Any]:
    agent_id = body.agent_id or (
        principal.actor.id
        if principal.actor.kind == "agent"
        else f"{principal.user_id}.{body.harness}"
    )
    if not agent_id.startswith(principal.user_id + "."):
        raise HTTPException(403, f"agent id must start with '{principal.user_id}.'")
    async with services.db.session() as session:
        row = await broker.register_agent(
            session,
            agent_id=agent_id,
            user_id=principal.user_id,
            harness=body.harness,
            card=body.card,
            push_url=body.push_url,
        )
        await session.commit()
        return {"agent_id": row.id, "card": row.card}


@router.get("/agents")
async def list_agents(
    _: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(get_services)],
) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from relayagents.core.models import AgentRow

    async with services.db.session() as session:
        rows = (await session.scalars(select(AgentRow).order_by(AgentRow.id))).all()
    return [
        {
            "agent_id": r.id,
            "user_id": r.user_id,
            "harness": r.harness,
            "card": r.card,
            "push": bool(r.push_url),
            "last_seen_at": r.last_seen_at,
        }
        for r in rows
    ]


@router.get("/agents/{agent_id}/.well-known/agent-card.json")
async def agent_card(
    agent_id: str, services: Annotated[Services, Depends(get_services)]
) -> dict[str, Any]:
    async with services.db.session() as session:
        row = await broker.get_agent(session, agent_id)
    if row is None:
        raise HTTPException(404, "unknown agent")
    return row.card


class JsonRpcRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: int | str | None = None
    method: str
    params: dict[str, Any] = {}


@router.post("/agents/{agent_id}")
async def jsonrpc(
    agent_id: str,
    body: JsonRpcRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(get_services)],
) -> dict[str, Any]:
    """A2A JSON-RPC binding. Any A2A client can `message/send` to a Relay agent this way."""
    try:
        if body.method == "message/send":
            params = SendMessageParams.model_validate(body.params)
            async with services.db.session() as session:
                task = await broker.send_message(
                    session,
                    from_actor=principal.actor,
                    to_agent=agent_id,
                    message=params.message,
                    metadata=params.metadata,
                )
                await session.commit()
            result: Any = task.model_dump(mode="json", by_alias=True)
        elif body.method == "tasks/get":
            async with services.db.session() as session:
                task_opt = await broker.get_task(session, body.params.get("id", ""))
            if task_opt is None:
                return {
                    "jsonrpc": "2.0",
                    "id": body.id,
                    "error": {"code": -32001, "message": "task not found"},
                }
            result = task_opt.model_dump(mode="json", by_alias=True)
        else:
            return {
                "jsonrpc": "2.0",
                "id": body.id,
                "error": {
                    "code": -32601,
                    "message": f"method {body.method} not supported by the Relay broker",
                },
            }
    except broker.BrokerError as exc:
        return {"jsonrpc": "2.0", "id": body.id, "error": {"code": -32000, "message": str(exc)}}
    return {"jsonrpc": "2.0", "id": body.id, "result": result}


@router.get("/inbox")
async def inbox(
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(get_services)],
    wait: int = Query(default=0, ge=0, le=60, description="Long-poll seconds."),
    states: Annotated[list[str] | None, Query()] = None,
) -> list[Task]:
    if principal.actor.kind != "agent":
        raise HTTPException(403, "inbox is for agent tokens")
    wanted = tuple(states or ["submitted"])
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    while True:
        async with services.db.session() as session:
            tasks = await broker.inbox(session, principal.actor.id, states=wanted)
            await session.commit()
        if tasks or loop.time() >= deadline:
            return tasks
        await asyncio.sleep(1.0)


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    _: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(get_services)],
) -> Task:
    async with services.db.session() as session:
        task = await broker.get_task(session, task_id)
    if task is None:
        raise HTTPException(404, "no such task")
    return task


@router.post("/tasks/{task_id}")
async def update_task(
    task_id: str,
    body: TaskUpdate,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(get_services)],
) -> Task:
    if principal.actor.kind != "agent":
        raise HTTPException(403, "only agents update tasks")
    async with services.db.session() as session:
        try:
            task = await broker.update_task(
                session,
                agent_actor=principal.actor,
                task_id=task_id,
                state=body.state,
                message=body.message,
                artifacts=body.artifacts,
            )
        except broker.BrokerError as exc:
            raise HTTPException(400, str(exc)) from exc
        await session.commit()
    # Surface state changes to the human who started the conversation (principle 7).
    chat = services.chat
    if (
        chat is not None
        and body.state in ("completed", "failed", "input_required")
        and body.message is not None
    ):
        origin_user = task.metadata.get("from", "").split(".", 1)[0]
        if origin_user:
            with contextlib.suppress(Exception):
                await chat.dm(
                    origin_user,
                    f":robot_face: `{principal.actor.id}` → thread `{task.context_id}` [{body.state}]:\n> {body.message.text[:1500]}",
                )
    return task


__all__ = ["Message", "router"]
