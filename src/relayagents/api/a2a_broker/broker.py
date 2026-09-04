"""Store-and-forward broker.

Every message becomes an ``agent.message`` event (the truth) and a row in ``a2a_tasks`` (delivery
bookkeeping). Delivery: an agent long-polls its inbox, or, if it registered a ``push_url``,
a worker POSTs the task there. Agents never talk to each other directly.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from relayagents.api.a2a_broker.types import (
    AgentCard,
    Artifact,
    Message,
    Task,
    TaskState,
    TaskStatus,
)
from relayagents.core.events import Actor, AgentMessage, AgentRegistered, Event, Provenance
from relayagents.core.ids import new_id
from relayagents.core.models import A2ATaskRow, AgentRow
from relayagents.core.projections import apply as project
from relayagents.core.store import EventStore


class BrokerError(Exception):
    pass


async def register_agent(
    session: AsyncSession,
    *,
    agent_id: str,
    user_id: str,
    harness: str,
    card: AgentCard,
    push_url: str | None = None,
    by: Actor | None = None,
) -> AgentRow:
    row = await session.get(AgentRow, agent_id)
    now = datetime.now(UTC)
    await EventStore(session).append(
        Event.new(
            AgentRegistered(agent_id=agent_id, user_id=user_id, harness=harness, push_url=push_url),
            actor=by or Actor.human(user_id),
            source="a2a",
            thread_id=f"agent:{agent_id}",
        )
    )
    if row is None:
        row = AgentRow(
            id=agent_id,
            user_id=user_id,
            name=card.name,
            harness=harness,
            card=card.model_dump(mode="json", by_alias=True),
            push_url=push_url,
            created_at=now,
            last_seen_at=now,
        )
        session.add(row)
    else:
        row.card = card.model_dump(mode="json", by_alias=True)
        row.push_url = push_url
        row.name = card.name
        row.last_seen_at = now
    await session.flush()
    return row


async def get_agent(session: AsyncSession, agent_id: str) -> AgentRow | None:
    return await session.get(AgentRow, agent_id)


async def agent_for_user(session: AsyncSession, user_id: str) -> AgentRow | None:
    rows = list(
        (
            await session.scalars(
                select(AgentRow).where(AgentRow.user_id == user_id).order_by(AgentRow.created_at)
            )
        ).all()
    )
    if not rows:
        return None
    for r in rows:
        if r.harness == "hermes":
            return r
    return rows[0]


def _row_to_task(row: A2ATaskRow) -> Task:
    history = [Message.model_validate(m) for m in row.history]
    last = history[-1] if history else None
    return Task(
        id=row.id,
        context_id=row.context_id,
        status=TaskStatus(state=row.state, message=last, timestamp=row.updated_at),  # type: ignore[arg-type]
        artifacts=[Artifact.model_validate(a) for a in row.artifacts],
        history=history,
        metadata={**row.metadata_, "from": row.from_agent, "to": row.to_agent},
    )


async def send_message(
    session: AsyncSession,
    *,
    from_actor: Actor,
    to_agent: str,
    message: Message,
    metadata: dict[str, Any] | None = None,
    surfaced_to: list[str] | None = None,
    provenance: Provenance | None = None,
) -> Task:
    """Create (or continue) a task for ``to_agent`` and log the message as an event."""
    target = await session.get(AgentRow, to_agent)
    if target is None:
        raise BrokerError(f"unknown agent {to_agent!r}")
    now = datetime.now(UTC)
    task_id = message.task_id or new_id("task")
    context_id = message.context_id or new_id("thr")
    message.task_id = task_id
    message.context_id = context_id
    message.role = "user"  # whoever sends *to* an agent is the "user" side of that task
    row = await session.get(A2ATaskRow, task_id)
    if row is not None:
        # Continuing an existing task: only its original sender (or that sender's human/agents)
        # may append, and only through the agent it was addressed to.
        origin_user = Actor.agent(row.from_agent).user_id
        if row.to_agent != to_agent or (
            from_actor.id != row.from_agent and from_actor.user_id != origin_user
        ):
            raise BrokerError(f"task {task_id!r} belongs to another conversation")
        context_id = row.context_id
        message.context_id = context_id
    if row is None:
        row = A2ATaskRow(
            id=task_id,
            context_id=context_id,
            from_agent=from_actor.id,
            to_agent=to_agent,
            state="submitted",
            history=[],
            artifacts=[],
            metadata_=metadata or {},
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    row.history = [*row.history, message.model_dump(mode="json", by_alias=True)]
    row.updated_at = now
    if row.state in ("completed", "failed", "canceled", "rejected"):
        row.state = "submitted"
    event = Event.new(
        AgentMessage(
            task_id=task_id,
            from_agent=from_actor.id,
            to_agent=to_agent,
            role=message.role,
            text=message.text,
            state="submitted",
            surfaced_to=surfaced_to or [target.user_id],
        ),
        actor=from_actor,
        source="a2a",
        thread_id=context_id,
        provenance=provenance,
    )
    await EventStore(session).append(event)
    await project(session, event)
    await session.flush()
    return _row_to_task(row)


async def get_task(session: AsyncSession, task_id: str) -> Task | None:
    row = await session.get(A2ATaskRow, task_id)
    return _row_to_task(row) if row else None


async def inbox(
    session: AsyncSession,
    agent_id: str,
    *,
    states: tuple[str, ...] = ("submitted",),
    limit: int = 20,
) -> list[Task]:
    rows = (
        await session.scalars(
            select(A2ATaskRow)
            .where(A2ATaskRow.to_agent == agent_id, A2ATaskRow.state.in_(states))
            .order_by(A2ATaskRow.created_at)
            .limit(limit)
        )
    ).all()
    agent = await session.get(AgentRow, agent_id)
    if agent is not None:
        agent.last_seen_at = datetime.now(UTC)
    return [_row_to_task(r) for r in rows]


async def update_task(
    session: AsyncSession,
    *,
    agent_actor: Actor,
    task_id: str,
    state: TaskState,
    message: Message | None,
    artifacts: list[Artifact] | None = None,
) -> Task:
    row = await session.get(A2ATaskRow, task_id)
    if row is None:
        raise BrokerError(f"unknown task {task_id!r}")
    if row.to_agent != agent_actor.id and row.from_agent != agent_actor.id:
        raise BrokerError("task does not belong to this agent")
    now = datetime.now(UTC)
    row.state = state
    row.updated_at = now
    if state == "working" and row.delivered_at is None:
        row.delivered_at = now
    if artifacts:
        row.artifacts = [
            *row.artifacts,
            *[a.model_dump(mode="json", by_alias=True) for a in artifacts],
        ]
    text = ""
    if message is not None:
        message.task_id = task_id
        message.context_id = row.context_id
        message.role = "agent"
        row.history = [*row.history, message.model_dump(mode="json", by_alias=True)]
        text = message.text
    origin = await session.get(AgentRow, row.from_agent)
    surfaced = [origin.user_id] if origin else [row.from_agent.split(".", 1)[0]]
    event = Event.new(
        AgentMessage(
            task_id=task_id,
            from_agent=agent_actor.id,
            to_agent=row.from_agent,
            role="agent",
            text=text,
            state=state,
            surfaced_to=surfaced,
        ),
        actor=agent_actor,
        source="a2a",
        thread_id=row.context_id,
    )
    await EventStore(session).append(event)
    await session.flush()
    return _row_to_task(row)


async def wait_for_terminal(
    session_factory: Any, task_id: str, *, timeout_s: float, poll_s: float = 1.0
) -> Task | None:
    """Poll until the task reaches a terminal state or the timeout elapses."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while True:
        async with session_factory() as session:
            task = await get_task(session, task_id)
        if task is None:
            return None
        if task.status.state in ("completed", "failed", "canceled", "rejected", "input_required"):
            return task
        if loop.time() >= deadline:
            return task
        await asyncio.sleep(poll_s)
