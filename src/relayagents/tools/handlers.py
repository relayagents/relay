"""Handler implementations. Each opens its own DB session and emits events for anything that
changes state. Handlers are transport-agnostic: the same function serves MCP, CLI, and REST."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime

from relayagents.api.a2a_broker import broker
from relayagents.api.a2a_broker.types import Message
from relayagents.core import approvals
from relayagents.core.events import (
    ActionItemClosed,
    Actor,
    Event,
    Provenance,
    QuestionOpened,
    ReportPosted,
    ToolCalled,
    ToolResult,
)
from relayagents.core.ids import new_id
from relayagents.core.permissions import is_forbidden, policy_for
from relayagents.core.projections import apply as project
from relayagents.core.projections import list_decisions, list_items
from relayagents.core.protocols import MemoryHit
from relayagents.core.store import EventStore, parse_since
from relayagents.tools.context import ToolContext
from relayagents.tools.schemas import (
    ActionItem,
    AskInput,
    AskOutput,
    Decision,
    DecisionsInput,
    DecisionsOutput,
    EventsInput,
    EventsOutput,
    ItemsInput,
    ItemsOutput,
    MyItemsInput,
    PostInput,
    PostOutput,
    RecallInput,
    RecallOutput,
    ReportInput,
    ReportOutput,
    RequestApprovalInput,
    RequestApprovalOutput,
)


class ToolError(Exception):
    """Raised for user-facing failures; transports map it to a clean error."""


# ---- recall ---------------------------------------------------------------------------------


async def recall(ctx: ToolContext, inp: RecallInput) -> RecallOutput:
    kinds = set(inp.kinds or ["graph", "vector", "event"])
    hits: list[MemoryHit] = []
    async with ctx.db.session() as session:
        store = EventStore(session)
        if "event" in kinds:
            for event, score in await store.keyword_search(inp.query, limit=inp.limit, types=None):
                hits.append(
                    MemoryHit(
                        text=_event_summary(event),
                        score=round(score, 3),
                        kind="event",
                        event_ids=[event.id],
                        valid_from=event.ts,
                        ref=event.id,
                    )
                )
        if "vector" in kinds and ctx.services.embedder is not None:
            try:
                emb = await ctx.services.embedder(inp.query)
                for event, score in await store.vector_search(emb, limit=inp.limit):
                    hits.append(
                        MemoryHit(
                            text=_event_summary(event),
                            score=round(score, 3),
                            kind="vector",
                            event_ids=[event.id],
                            valid_from=event.ts,
                            ref=event.id,
                        )
                    )
            except Exception as exc:
                hits.append(
                    MemoryHit(text=f"[vector search unavailable: {exc}]", score=0.0, kind="vector")
                )
    if "graph" in kinds and ctx.services.memory is not None:
        hits.extend(await ctx.services.memory.search(inp.query, limit=inp.limit))
    # Merge: dedupe by event id, prefer highest score, keep provenance.
    best: dict[str, MemoryHit] = {}
    for h in hits:
        key = h.ref or h.text
        if key not in best or h.score > best[key].score:
            best[key] = h
    ordered = sorted(best.values(), key=lambda h: h.score, reverse=True)[: inp.limit]
    return RecallOutput(hits=ordered)


def _event_summary(event: Event) -> str:
    p = event.payload.model_dump()
    for key in ("statement", "text", "title", "action", "answer"):
        if p.get(key):
            return f"[{event.type}] {p[key]}"
    return f"[{event.type}]"


# ---- items ----------------------------------------------------------------------------------


def _item(row) -> ActionItem:  # type: ignore[no-untyped-def]
    return ActionItem(
        id=row.id,
        title=row.title,
        assignee=row.assignee,
        status=row.status,
        due=row.due,
        details=row.details,
        meeting_id=row.meeting_id,
        source_event_id=row.source_event_id,
        updated_at=row.updated_at,
    )


async def my_items(ctx: ToolContext, inp: MyItemsInput) -> ItemsOutput:
    async with ctx.db.session() as session:
        rows = await list_items(session, assignee=ctx.user_id, status=inp.status, limit=inp.limit)
    return ItemsOutput(items=[_item(r) for r in rows])


async def items(ctx: ToolContext, inp: ItemsInput) -> ItemsOutput:
    async with ctx.db.session() as session:
        rows = await list_items(
            session, assignee=ctx.resolve_user(inp.assignee), status=inp.status, limit=inp.limit
        )
    return ItemsOutput(items=[_item(r) for r in rows])


# ---- events ---------------------------------------------------------------------------------


async def events(ctx: ToolContext, inp: EventsInput) -> EventsOutput:
    actor = ctx.resolve_user(inp.actor)
    types = list(inp.type) if inp.type else None
    async with ctx.db.session() as session:
        found = await EventStore(session).query(
            since=parse_since(inp.since),
            types=types,
            thread_id=inp.thread,
            user_id=actor if actor == ctx.user_id else None,
            actor_id=actor if actor and actor != ctx.user_id else None,
            text=inp.text,
            limit=inp.limit,
            descending=True,
        )
    if not inp.include_tool_calls and not (types and any(t.startswith("tool.") for t in types)):
        found = [e for e in found if not e.type.startswith("tool.")]
    return EventsOutput(events=list(reversed(found)))


# ---- report ---------------------------------------------------------------------------------


async def report(ctx: ToolContext, inp: ReportInput) -> ReportOutput:
    links = [inp.link] if inp.link else []
    async with ctx.db.session() as session:
        store = EventStore(session)
        thread = inp.item_id
        ev = Event.new(
            ReportPosted(text=inp.text, item_id=inp.item_id, links=links),
            actor=ctx.actor,
            source=_source_for(ctx),
            thread_id=thread,
        )
        await store.append(ev)
        await project(session, ev)
        closed = None
        if inp.close_item:
            if not inp.item_id:
                raise ToolError("close_item requires item_id")
            close = Event.new(
                ActionItemClosed(
                    item_id=inp.item_id, resolution="done", note=inp.text, links=links
                ),
                actor=ctx.actor,
                source=_source_for(ctx),
                thread_id=thread,
                provenance=Provenance(parent_event_ids=[ev.id]),
            )
            await store.append(close)
            await project(session, close)
            closed = inp.item_id
        await session.commit()
    return ReportOutput(event_id=ev.id, closed_item_id=closed)


def _source_for(ctx: ToolContext):  # type: ignore[no-untyped-def]
    return {"mcp": "api", "rest": "api", "cli": "cli", "internal": "api"}[ctx.transport]


# ---- ask ------------------------------------------------------------------------------------


async def ask(ctx: ToolContext, inp: AskInput) -> AskOutput:
    target_user = ctx.resolve_user(inp.user)
    assert target_user is not None
    async with ctx.db.session() as session:
        agent = await broker.agent_for_user(session, target_user)
        if agent is None:
            raise ToolError(
                f"no agent registered for user {target_user!r}; they need to run `relay add-user` / `relay setup-agent`"
            )
        question_id = new_id("q")
        thread_id = inp.thread_id or new_id("thr")
        msg = Message.from_text(
            inp.question,
            context_id=thread_id,
            metadata={"question_id": question_id, "from_user": ctx.user_id},
        )
        task = await broker.send_message(
            session,
            from_actor=ctx.actor,
            to_agent=agent.id,
            message=msg,
            metadata={"question_id": question_id},
            surfaced_to=[target_user],
        )
        q = Event.new(
            QuestionOpened(question_id=question_id, text=inp.question, asked_of=target_user),
            actor=ctx.actor,
            source="a2a",
            thread_id=thread_id,
            provenance=Provenance(parent_event_ids=[]),
        )
        await EventStore(session).append(q)
        await session.commit()
    if ctx.services.chat is not None:
        with contextlib.suppress(Exception):
            await ctx.services.chat.dm(
                target_user,
                f":speech_balloon: `{ctx.actor.id}` asked your agent (thread `{thread_id}`):\n> {inp.question}",
            )
    answer = None
    state = task.status.state
    if inp.wait_s:
        done = await broker.wait_for_terminal(ctx.db.sessions, task.id, timeout_s=inp.wait_s)
        if done is not None:
            state = done.status.state
            if done.history and done.history[-1].role == "agent":
                answer = done.history[-1].text
    return AskOutput(
        task_id=task.id,
        thread_id=thread_id,
        question_id=question_id,
        to_agent=agent.id,
        state=state,
        answer=answer,
    )


# ---- request_approval -----------------------------------------------------------------------


async def request_approval(ctx: ToolContext, inp: RequestApprovalInput) -> RequestApprovalOutput:
    if is_forbidden(inp.action_type):
        raise ToolError(
            f"action type {inp.action_type!r} is forbidden by policy; see docs/permissions.md"
        )
    async with ctx.db.session() as session:
        row = await approvals.request(
            session,
            requester=ctx.actor,
            requested_of=ctx.user_id,
            action=inp.action,
            action_type=inp.action_type,
            details=inp.details,
            ttl_s=inp.timeout_s,
            chat=ctx.services.chat,
        )
        await session.commit()
        approval_id = row.id
    if inp.wait:
        row = await approvals.wait(ctx.db.sessions, approval_id, timeout_s=inp.timeout_s)
    return RequestApprovalOutput(
        approval_id=approval_id,
        status=row.status,
        edited_action=row.edited_action,
        requested_of=row.requested_of,
    )  # type: ignore[arg-type]


# ---- decisions ------------------------------------------------------------------------------


async def decisions(ctx: ToolContext, inp: DecisionsInput) -> DecisionsOutput:
    async with ctx.db.session() as session:
        rows = await list_decisions(
            session, topic=inp.topic, since=parse_since(inp.since), limit=inp.limit
        )
    return DecisionsOutput(
        decisions=[
            Decision(
                id=r.id,
                statement=r.statement,
                topic=r.topic,
                rationale=r.rationale,
                decided_by=list(r.decided_by),
                decided_at=r.decided_at,
                supersedes=r.supersedes,
                superseded_by=r.superseded_by,
                source_event_id=r.source_event_id,
            )
            for r in rows
        ]
    )


# ---- post -----------------------------------------------------------------------------------


async def post(ctx: ToolContext, inp: PostInput) -> PostOutput:
    if not inp.as_agent:
        raise ToolError(
            "posting as a human is forbidden (policy slack.post.as_user=forbid); use as_agent=true"
        )
    action_type = "slack.post.as_agent"
    if policy_for(action_type) == "approve":
        result = await request_approval(
            ctx,
            RequestApprovalInput(
                action=f"post to Slack: {inp.text[:200]}", action_type=action_type
            ),
        )
        if result.status != "approved":
            raise ToolError(f"post not approved ({result.status})")
    chat = ctx.services.chat
    if chat is None:
        raise ToolError("Slack is not configured on this Relay node")
    channel = inp.channel or ctx.settings.slack_team_channel
    if not channel:
        raise ToolError("no channel given and RELAY_SLACK_TEAM_CHANNEL is unset")
    attribution = (
        f"posted by {ctx.principal.display_name or ctx.user_id}'s agent"
        if ctx.actor.kind == "agent"
        else f"posted by {ctx.principal.display_name or ctx.user_id} via relay"
    )
    call_id = new_id("call")
    async with ctx.db.session() as session:
        store = EventStore(session)
        called = Event.new(
            ToolCalled(
                call_id=call_id,
                tool="post",
                transport=ctx.transport,
                arguments={"channel": channel, "text": inp.text},
                target=f"slack:{channel}",
            ),
            actor=ctx.actor,
            source="slack",
        )
        await store.append(called)
        started = datetime.now(UTC)
        try:
            ref = await chat.post(channel, inp.text, attribution=attribution)
        except Exception as exc:
            await store.append(
                Event.new(
                    ToolResult(call_id=call_id, tool="post", ok=False, error=str(exc)),
                    actor=ctx.actor,
                    source="slack",
                    provenance=Provenance(parent_event_ids=[called.id]),
                )
            )
            await session.commit()
            raise ToolError(f"Slack post failed: {exc}") from exc
        ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
        result_ev = Event.new(
            ToolResult(
                call_id=call_id,
                tool="post",
                ok=True,
                summary=f"{ref.channel}/{ref.ts}",
                duration_ms=ms,
            ),
            actor=ctx.actor,
            source="slack",
            provenance=Provenance(parent_event_ids=[called.id], tool_call_ids=[call_id]),
        )
        await store.append(result_ev)
        await session.commit()
    return PostOutput(channel=ref.channel, ts=ref.ts, event_id=result_ev.id)


__all__ = [
    "Actor",
    "ask",
    "decisions",
    "events",
    "items",
    "my_items",
    "post",
    "recall",
    "report",
    "request_approval",
]
