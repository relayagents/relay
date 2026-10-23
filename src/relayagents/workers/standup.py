"""Per-user standup support (vertical slice 2).

Relay builds the draft from events and applies the user's posting mode. The workers' per-minute
cron (`run_due_standups`) does this at each user's `standup_time` (ADR-0011); `relay standup
draft` / `submit` let a person or their agent do the same by hand. Nothing adds facts to a draft:
every line cites an event id. Relay renders the Block Kit draft with Approve/Edit buttons and
posts only on a click (mode=draft) or immediately with attribution (mode=auto). Nothing posts
when mode=off.

In draft mode the click is a real approval: ``submit`` opens an ``ApprovalRow`` (+
``approval.requested``) and the button handlers resolve it (``approval.resolved``) before posting.
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from relayagents.core import approvals
from relayagents.core.events import (
    ActionItemClosed,
    ActionItemUpdated,
    Actor,
    ApprovalRequested,
    Event,
    Provenance,
    ReportPosted,
    StandupPosted,
)
from relayagents.core.indexing import index_later
from relayagents.core.models import ApprovalRow, UserRow
from relayagents.core.projections import list_items
from relayagents.core.store import EventStore
from relayagents.tools.context import Services

DRAFT_TTL_S = 12 * 3600

log = structlog.get_logger()
SCHEDULER = Actor.system("relay.standup")
CATCH_UP = timedelta(hours=2)  # a standup missed by a busy or restarting worker still goes out


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
        text += "\n*Open questions*\n" + "\n".join(f"• {q}" for q in draft.questions)
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


async def submit(
    services: Services, draft: StandupDraft, *, actor: Actor, now: datetime | None = None
) -> dict[str, Any]:
    """Apply the user's posting mode. Emits approval.* and standup.posted as appropriate, stamped
    with ``now`` (the scheduler's tick) when given."""
    async with services.db.session() as session:
        user = await session.get(UserRow, draft.user_id)
        if user is None:
            raise KeyError(draft.user_id)
        mode = user.standup_mode
        if mode == "off":
            return {"mode": "off", "posted": False}
        if mode == "auto":
            ev = await _post(services, session, draft, user, mode="auto", actor=actor, now=now)
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
            now=now,
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


def local_now(user: UserRow, now: datetime) -> datetime:
    try:
        tz = ZoneInfo(user.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("standup.bad_timezone", user=user.id, timezone=user.timezone)
        tz = UTC  # type: ignore[assignment]
    return now.astimezone(tz)


def due_since(user: UserRow, local: datetime) -> timedelta | None:
    """How long ago today's standup time passed, if it did within the catch-up window. A late or
    restarted cron still sends it, and a time skipped by a DST jump runs once the clock is past it."""
    if user.standup_mode == "off":
        return None
    try:
        hour, minute = (int(x) for x in user.standup_time.split(":"))
        at = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError:
        return None
    late = local - at  # same tzinfo: wall-clock difference
    return late if timedelta(0) <= late < CATCH_UP else None


async def scheduled_today(session: AsyncSession, user_id: str, local: datetime) -> bool:
    """True once the scheduler has drafted or posted this user's standup during their local day.
    Only the scheduler's own records count: a draft approved after midnight, or a standup
    submitted by hand, does not stand in for the next day's."""
    start = datetime.combine(local.date(), time(0), tzinfo=local.tzinfo)
    end = datetime.combine(local.date() + timedelta(days=1), time(0), tzinfo=local.tzinfo)
    events = await EventStore(session).query(
        types=["standup.posted", "approval.requested"],
        actor_id=SCHEDULER.id,
        since=start.astimezone(UTC),
        until=end.astimezone(UTC),
        limit=10_000,  # at most one per user per day
    )
    return any(
        (isinstance(e.payload, StandupPosted) and e.payload.user_id == user_id)
        or (isinstance(e.payload, ApprovalRequested) and e.payload.requested_of == user_id)
        for e in events
    )


async def run_due_standups(services: Services, *, now: datetime | None = None) -> list[str]:
    """Called every minute by the workers: draft and submit for each user whose time has come.
    Returns the users whose standup went out; one user's failure does not stop the others."""
    now = now or datetime.now(UTC)
    async with services.db.session() as session:
        users = (await session.scalars(select(UserRow).where(UserRow.standup_mode != "off"))).all()
    done: list[str] = []
    for user in users:
        try:
            local = local_now(user, now)
            late = due_since(user, local)
            if late is None:
                continue
            if user.standup_mode == "draft" and (services.chat is None or not user.slack_user_id):
                if late < timedelta(minutes=1):  # say so once, not every minute of the window
                    log.info("standup.no_dm_target", user=user.id)
                continue
            async with services.db.session() as session:
                if await scheduled_today(session, user.id, local):
                    continue
                draft = await gather(session, user.id, now=now)
            await submit(services, draft, actor=SCHEDULER, now=now)
        except Exception:
            log.exception("standup.scheduled_failed", user=user.id)
            continue
        done.append(user.id)
    return done


async def _post(
    services: Services,
    session: AsyncSession,
    draft: StandupDraft,
    user: UserRow,
    *,
    mode: str,
    actor: Actor,
    now: datetime | None = None,
) -> Event:
    now = now or datetime.now(UTC)
    channel = services.settings.slack_team_channel
    ref = None
    if services.chat is not None and channel:
        ref = await services.chat.post(
            channel,
            render_standup(draft, user.display_name),
            attribution=(
                f"posted by Relay for {user.display_name}"
                if mode == "auto" and actor.kind == "system"
                else f"posted by {user.display_name}'s agent"
                if mode == "auto"
                else f"approved by {user.display_name}"
            ),
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
        thread_id=f"standup:{user.id}:{local_now(user, now):%Y-%m-%d}",  # the user's own day
        ts=now,
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
) -> Event | None:
    """Post the draft on the owner's click. None when this draft was already approved and posted:
    a double click, or Slack retrying the action, must not post twice."""
    if approval_id:
        row = await session.get(ApprovalRow, approval_id)
        if row is not None and row.status == "approved":
            return None
        with contextlib.suppress(KeyError):  # the row may have expired; the click still counts
            await approvals.resolve(
                session, approval_id=approval_id, decision="approved", resolved_by=user.id
            )
    return await _post(services, session, draft, user, mode="draft", actor=Actor.human(user.id))


def _posted_blocks(ev: Event | None, *, edited: bool = False) -> tuple[str, list[dict[str, Any]]]:
    if ev is None:
        text = "This standup was already posted."
        return text, [
            {"type": "section", "text": {"type": "mrkdwn", "text": f":white_check_mark: {text}"}}
        ]
    label = "Standup posted (edited)." if edited else "Standup posted."
    return f"Standup posted ({ev.id}).", [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f":white_check_mark: {label} `{ev.id}`"},
        }
    ]


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
    text, blocks = _posted_blocks(ev)
    await client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"], text=text, blocks=blocks
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
    text, blocks = _posted_blocks(ev, edited=True)
    with contextlib.suppress(Exception):
        await client.chat_update(channel=meta["channel"], ts=meta["ts"], text=text, blocks=blocks)
