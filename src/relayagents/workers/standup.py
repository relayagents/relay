"""Per-user standup support (vertical slice 2).

The *drafting* happens in the user's own agent (a Hermes cron calls `relay standup draft`, adds
its judgement, then `relay standup submit`). Relay's part is deterministic: gather the sourced
facts, render the Block Kit draft with Approve/Edit buttons, and post only on a click (mode=draft)
or immediately with attribution (mode=auto). Nothing posts when mode=off.

In draft mode the click is a real approval: ``submit`` opens an ``ApprovalRow`` (+
``approval.requested``) and the button handlers resolve it (``approval.resolved``) before posting.
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from relayagents.core import approvals
from relayagents.core.events import (
    ActionItemClosed,
    ActionItemUpdated,
    Actor,
    Event,
    Provenance,
    ReportPosted,
    StandupPosted,
)
from relayagents.core.indexing import index_later
from relayagents.core.models import UserRow
from relayagents.core.projections import list_items
from relayagents.core.store import EventStore
from relayagents.tools.context import Services

DRAFT_TTL_S = 12 * 3600


class StandupDraft(BaseModel):
    user_id: str
    done: list[str] = Field(default_factory=list)
    doing: list[str] = Field(default_factory=list)
    blocked: list[str] = Field(default_factory=list)
    questions: list[str] = Field(
        default_factory=list,
        description="Anything without an event becomes a question, never an assertion.",
    )
    cited_event_ids: list[str] = Field(default_factory=list)


async def gather(
    session: AsyncSession, user_id: str, *, hours: int = 24, now: datetime | None = None
) -> StandupDraft:
    """The sourced skeleton: every line cites an event id. The agent may reword but not add facts."""
    now = now or datetime.now(UTC)
    events = await EventStore(session).query(
        since=now - timedelta(hours=hours), user_id=user_id, limit=1000
    )
    draft = StandupDraft(user_id=user_id)
    for e in events:
        p = e.payload
        if isinstance(p, ActionItemClosed):
            draft.done.append(f"{p.note or 'closed ' + p.item_id} [{e.id}]")
        elif isinstance(p, ReportPosted):
            draft.doing.append(f"{p.text} [{e.id}]")
        elif isinstance(p, ActionItemUpdated) and p.status == "blocked":
            draft.blocked.append(f"{p.note or p.item_id} [{e.id}]")
        else:
            continue
        draft.cited_event_ids.append(e.id)
    open_items = await list_items(session, assignee=user_id, status="open")
    reported = {
        e.payload.item_id  # type: ignore[attr-defined]
        for e in events
        if isinstance(e.payload, ReportPosted) and e.payload.item_id
    }
    for item in open_items:
        if item.id not in reported:
            draft.questions.append(
                f"Still working on '{item.title}' ({item.id})? No report in the last {hours}h."
            )
    return draft


def draft_blocks(draft: StandupDraft, *, approval_id: str | None = None) -> list[dict[str, Any]]:
    def section(title: str, xs: list[str]) -> str:
        return f"*{title}*\n" + ("\n".join(f"• {x}" for x in xs) if xs else "• _nothing_")

    text = "\n".join(
        [
            section("Done", draft.done),
            section("Doing", draft.doing),
            section("Blocked", draft.blocked),
        ]
    )
    if draft.questions:
        text += "\n*Open questions from your agent*\n" + "\n".join(
            f"• {q}" for q in draft.questions
        )
    payload = json.dumps({**draft.model_dump(), "approval_id": approval_id})
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":sunrise: *Standup draft for {draft.user_id}* (nothing posts until you click)\n{text}",
            },
        },
        {
            "type": "actions",
            "block_id": "standup",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Approve & post"},
                    "action_id": "standup_approve",
                    "value": payload,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Edit"},
                    "action_id": "standup_edit",
                    "value": payload,
                },
            ],
        },
    ]


def render_standup(draft: StandupDraft, display_name: str) -> str:
    def block(title: str, xs: list[str]) -> str:
        return f"*{title}:* " + ("; ".join(xs) if xs else "—")

    return f"*{display_name}*\n" + "\n".join(
        [block("Done", draft.done), block("Doing", draft.doing), block("Blocked", draft.blocked)]
    )


async def submit(services: Services, draft: StandupDraft, *, actor: Actor) -> dict[str, Any]:
    """Apply the user's posting mode. Emits approval.* and standup.posted as appropriate."""
    async with services.db.session() as session:
        user = await session.get(UserRow, draft.user_id)
        if user is None:
            raise KeyError(draft.user_id)
        mode = user.standup_mode
        if mode == "off":
            return {"mode": "off", "posted": False}
        if mode == "auto":
            ev = await _post(services, session, draft, user, mode="auto", actor=actor)
            await session.commit()
            return {"mode": "auto", "posted": True, "event_id": ev.id}
        # draft mode: open a real approval (row + event) and DM the draft with buttons.
        row = await approvals.request(
            session,
            requester=actor,
            requested_of=draft.user_id,
            action=f"post standup for {draft.user_id}",
            action_type="standup.post.draft",
            details={"draft": draft.model_dump()},
            ttl_s=DRAFT_TTL_S,
            chat=None,
        )
        if services.chat is not None:
            if services.chat.supports_actions:
                ref = await services.chat.dm(
                    draft.user_id,
                    "Your standup draft is ready.",
                    blocks=draft_blocks(draft, approval_id=row.id),
                )
            else:
                ref = await services.chat.dm(
                    draft.user_id,
                    f"Your standup draft is ready ({row.id}). Post it with `relay approvals approve {row.id}` or edit it with `relay standup submit`.",
                    blocks=draft_blocks(draft, approval_id=row.id)[:1],
                )
            row.chat_channel, row.chat_ts = ref.channel, ref.ts
        await session.commit()
        return {"mode": "draft", "posted": False, "approval_id": row.id, "event_id": row.event_id}


async def _post(
    services: Services,
    session: AsyncSession,
    draft: StandupDraft,
    user: UserRow,
    *,
    mode: str,
    actor: Actor,
) -> Event:
    channel = services.settings.slack_team_channel
    ref = None
    if services.chat is not None and channel:
        ref = await services.chat.post(
            channel,
            render_standup(draft, user.display_name),
            attribution=f"posted by {user.display_name}'s agent"
            if mode == "auto"
            else f"approved by {user.display_name}",
        )
    ev = Event.new(
        StandupPosted(
            user_id=user.id,
            mode=mode,  # type: ignore[arg-type]
            done=draft.done,
            doing=draft.doing,
            blocked=draft.blocked,
            questions=draft.questions,
            cited_event_ids=draft.cited_event_ids,
            channel=ref.channel if ref else None,
            message_ref=ref.ts if ref else None,
        ),
        actor=actor,
        source="slack",
        thread_id=f"standup:{user.id}:{datetime.now(UTC):%Y-%m-%d}",
        provenance=Provenance(parent_event_ids=list(draft.cited_event_ids)),
    )
    await EventStore(session).append(ev)
    await index_later(services, [ev])
    return ev


async def post_approved_draft(
    services: Services, session: AsyncSession, approval: Any
) -> Event | None:
    """Called when a `standup.post.draft` approval is approved outside Slack (CLI/REST):
    post the draft stored in the approval's details."""
    if approval.action_type != "standup.post.draft" or approval.status != "approved":
        return None
    draft = StandupDraft.model_validate(
        approval.details.get("draft") or {"user_id": approval.requested_of}
    )
    user = await session.get(UserRow, draft.user_id)
    if user is None:
        return None
    return await _post(services, session, draft, user, mode="draft", actor=Actor.human(user.id))


# ---- Slack button handlers (called from api.slack.app) ----------------------------------------


def _unpack(value: str) -> tuple[StandupDraft, str | None]:
    raw = json.loads(value)
    approval_id = raw.pop("approval_id", None)
    return StandupDraft.model_validate(raw), approval_id


async def _approve_and_post(
    services: Services,
    session: AsyncSession,
    draft: StandupDraft,
    user: UserRow,
    approval_id: str | None,
) -> Event:
    if approval_id:
        with contextlib.suppress(KeyError):  # the row may have expired; the click still counts
            await approvals.resolve(
                session, approval_id=approval_id, decision="approved", resolved_by=user.id
            )
    return await _post(services, session, draft, user, mode="draft", actor=Actor.human(user.id))


async def post_standup_from_draft(services: Services, body: dict[str, Any], client: Any) -> None:
    draft, approval_id = _unpack(body["actions"][0]["value"])
    async with services.db.session() as session:
        user = await session.get(UserRow, draft.user_id)
        if user is None or user.slack_user_id != body["user"]["id"]:
            await client.chat_postEphemeral(
                channel=body["channel"]["id"],
                user=body["user"]["id"],
                text="Only the owner can post their standup.",
            )
            return
        ev = await _approve_and_post(services, session, draft, user, approval_id)
        await session.commit()
    await client.chat_update(
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text=f"Standup posted ({ev.id}).",
        blocks=[
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f":white_check_mark: Standup posted. `{ev.id}`"},
            }
        ],
    )


async def open_standup_edit_modal(services: Services, body: dict[str, Any], client: Any) -> None:
    draft, approval_id = _unpack(body["actions"][0]["value"])

    def field(block_id: str, label: str, xs: list[str]) -> dict[str, Any]:
        return {
            "type": "input",
            "block_id": block_id,
            "label": {"type": "plain_text", "text": label},
            "optional": True,
            "element": {
                "type": "plain_text_input",
                "multiline": True,
                "action_id": "v",
                "initial_value": "\n".join(xs),
            },
        }

    await client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "standup_edit_submit",
            "private_metadata": json.dumps(
                {
                    "user_id": draft.user_id,
                    "cited_event_ids": draft.cited_event_ids,
                    "approval_id": approval_id,
                    "channel": body["channel"]["id"],
                    "ts": body["message"]["ts"],
                }
            ),
            "title": {"type": "plain_text", "text": "Edit standup"},
            "submit": {"type": "plain_text", "text": "Post"},
            "blocks": [
                field("done", "Done", draft.done),
                field("doing", "Doing", draft.doing),
                field("blocked", "Blocked", draft.blocked),
            ],
        },
    )


async def post_standup_from_modal(services: Services, body: dict[str, Any], client: Any) -> None:
    meta = json.loads(body["view"]["private_metadata"])
    values = body["view"]["state"]["values"]

    def lines(block_id: str) -> list[str]:
        raw = (values.get(block_id, {}).get("v", {}).get("value") or "").strip()
        return [ln.strip("• ").strip() for ln in raw.splitlines() if ln.strip()]

    draft = StandupDraft(
        user_id=meta["user_id"],
        done=lines("done"),
        doing=lines("doing"),
        blocked=lines("blocked"),
        cited_event_ids=meta["cited_event_ids"],
    )
    async with services.db.session() as session:
        user = await session.get(UserRow, draft.user_id)
        if user is None or user.slack_user_id != body["user"]["id"]:
            return
        ev = await _approve_and_post(services, session, draft, user, meta.get("approval_id"))
        await session.commit()
    with contextlib.suppress(Exception):
        await client.chat_update(
            channel=meta["channel"],
            ts=meta["ts"],
            text=f"Standup posted ({ev.id}).",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":white_check_mark: Standup posted (edited). `{ev.id}`",
                    },
                }
            ],
        )
