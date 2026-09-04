"""The PM job: after extraction, tell the team and hand each assigned item to its owner's agent.

Relay's PM has no superuser credentials. It only (a) posts a summary through the team Slack app
and (b) sends A2A tasks to the assignees' own agents, which act under their humans' tokens.
"""

from __future__ import annotations

import contextlib
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from relayagents.api.a2a_broker import broker
from relayagents.api.a2a_broker.types import Message
from relayagents.core.events import (
    ActionItemCreated,
    Actor,
    DecisionMade,
    Event,
    Provenance,
    QuestionOpened,
)
from relayagents.core.models import MeetingRow
from relayagents.tools.context import Services

PM_ACTOR = Actor.system("relay.pm")


def summary_text(meeting: MeetingRow, events: list[Event]) -> str:
    decisions = [e for e in events if isinstance(e.payload, DecisionMade)]
    items = [e for e in events if isinstance(e.payload, ActionItemCreated)]
    questions = [e for e in events if isinstance(e.payload, QuestionOpened)]
    lines = [f"*Meeting: {meeting.title}* (`{meeting.id}`)"]
    if decisions:
        lines.append("*Decisions*")
        lines += [f"• {e.payload.statement}  `{e.id}`" for e in decisions]  # type: ignore[attr-defined]
    if items:
        lines.append("*Action items*")
        lines += [
            f"• {e.payload.title}"
            + (f" → `{e.payload.assignee}`" if e.payload.assignee else " (unassigned)")
            + f"  `{e.payload.item_id}`"
            for e in items
        ]  # type: ignore[attr-defined]
    if questions:
        lines.append("*Open questions*")
        lines += [
            f"• {e.payload.text}" + (f" (for `{e.payload.asked_of}`)" if e.payload.asked_of else "")
            for e in questions
        ]  # type: ignore[attr-defined]
    if len(lines) == 1:
        lines.append("_No decisions, action items, or questions were extracted._")
    return "\n".join(lines)


def task_text(meeting: MeetingRow, ev: Event) -> str:
    p = ev.payload
    assert isinstance(p, ActionItemCreated)
    return (
        f"You have a new action item from meeting '{meeting.title}' ({meeting.id}).\n\n"
        f"Item {p.item_id}: {p.title}\n"
        + (f"Details: {p.details}\n" if p.details else "")
        + (f"Due: {p.due.date().isoformat()}\n" if p.due else "")
        + f"Source event: {ev.id} (segments {', '.join(ev.provenance.segment_ids)}).\n\n"
        "Plan it with your human. If it needs an external write (GitHub issue, doc, coding agent run), call "
        "`request_approval` first. When done, call `report` with item_id and close_item=true."
    )


async def dispatch(
    session: AsyncSession, services: Services, meeting: MeetingRow, events: list[Event]
) -> dict[str, Any]:
    """Post the summary and deliver one A2A task per assigned item. Returns what happened."""
    out: dict[str, Any] = {"summary_posted": False, "tasks": [], "unrouted": []}
    chat = services.chat
    channel = services.settings.slack_team_channel
    if chat is not None and channel:
        try:
            ref = await chat.post(
                channel,
                summary_text(meeting, events),
                attribution="posted by Relay (meeting summary)",
            )
            out["summary_posted"] = True
            out["summary_ref"] = ref.model_dump()
        except Exception as exc:
            out["summary_error"] = str(exc)
    for ev in events:
        p = ev.payload
        if not isinstance(p, ActionItemCreated) or not p.assignee:
            continue
        agent = await broker.agent_for_user(session, p.assignee)
        if agent is None:
            out["unrouted"].append({"item_id": p.item_id, "assignee": p.assignee})
            continue
        msg = Message.from_text(
            task_text(meeting, ev),
            context_id=p.item_id,
            metadata={"item_id": p.item_id, "meeting_id": meeting.id, "source_event_id": ev.id},
        )
        task = await broker.send_message(
            session,
            from_actor=PM_ACTOR,
            to_agent=agent.id,
            message=msg,
            metadata={"item_id": p.item_id, "kind": "action_item"},
            surfaced_to=[p.assignee],
            provenance=Provenance(parent_event_ids=[ev.id]),
        )
        out["tasks"].append({"task_id": task.id, "to": agent.id, "item_id": p.item_id})
        if chat is not None:
            with contextlib.suppress(Exception):
                await chat.dm(
                    p.assignee,
                    f":clipboard: Relay handed your agent an action item from *{meeting.title}*:\n> {p.title}\n`{p.item_id}` · task `{task.id}`",
                )
    return out
