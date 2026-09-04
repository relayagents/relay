"""Approvals: an agent asks, a human resolves (in Slack), the caller blocks or polls.

State lives in ``approvals`` (operational) and in ``approval.requested`` / ``approval.resolved``
events (truth). The Slack handlers call :func:`resolve`; the tool handler calls :func:`request`
then :func:`wait`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from relayagents.core.events import Actor, ApprovalRequested, ApprovalResolved, Event, Provenance
from relayagents.core.ids import new_id
from relayagents.core.models import ApprovalRow
from relayagents.core.protocols import ChatApp
from relayagents.core.redact import redact
from relayagents.core.store import EventStore


def approval_blocks(
    approval_id: str, requester: str, action: str, action_type: str, *, interactive: bool = True
) -> list[dict[str, Any]]:
    """Slack Block Kit for an approval request. Buttons carry the approval id; without Socket Mode
    the message explains how to resolve from the CLI instead."""
    header = {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f":lock: *Approval requested* by `{requester}`\n> {action}\n_policy: `{action_type}`_ · `{approval_id}`",
        },
    }
    if not interactive:
        return [
            header,
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Resolve from your terminal: `relay approvals approve {approval_id}` or `relay approvals deny {approval_id}`",
                    }
                ],
            },
        ]
    return [
        header,
        {
            "type": "actions",
            "block_id": f"approval:{approval_id}",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "action_id": "approval_approve",
                    "value": approval_id,
                },
                {
                    "type": "button",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "Deny"},
                    "action_id": "approval_deny",
                    "value": approval_id,
                },
            ],
        },
    ]


async def request(
    session: AsyncSession,
    *,
    requester: Actor,
    requested_of: str,
    action: str,
    action_type: str,
    details: dict[str, Any] | None = None,
    ttl_s: int = 3600,
    thread_id: str | None = None,
    chat: ChatApp | None = None,
    provenance: Provenance | None = None,
) -> ApprovalRow:
    details = redact(details or {})
    action = redact(action)
    now = datetime.now(UTC)
    approval_id = new_id("apr")
    expires = now + timedelta(seconds=ttl_s)
    event = Event.new(
        ApprovalRequested(
            approval_id=approval_id,
            action=action,
            action_type=action_type,
            requested_of=requested_of,
            details=details,
            expires_at=expires,
        ),
        actor=requester,
        source="api",
        thread_id=thread_id or approval_id,
        provenance=provenance,
    )
    await EventStore(session).append(event)
    row = ApprovalRow(
        id=approval_id,
        event_id=event.id,
        requester_actor_kind=requester.kind,
        requester_actor_id=requester.id,
        requested_of=requested_of,
        action=action,
        action_type=action_type,
        details=details,
        status="pending",
        created_at=now,
        expires_at=expires,
    )
    session.add(row)
    await session.flush()
    if chat is not None:
        try:
            ref = await chat.dm(
                requested_of,
                f"Approval requested by {requester.id}: {action}",
                blocks=approval_blocks(
                    approval_id,
                    requester.id,
                    action,
                    action_type,
                    interactive=chat.supports_actions,
                ),
            )
            row.chat_channel, row.chat_ts = ref.channel, ref.ts
        except Exception:
            pass
    return row


async def resolve(
    session: AsyncSession,
    *,
    approval_id: str,
    decision: str,
    resolved_by: str | None,
    edited_action: str | None = None,
    note: str | None = None,
) -> ApprovalRow:
    row = await session.get(ApprovalRow, approval_id)
    if row is None:
        raise KeyError(approval_id)
    if row.status != "pending":
        return row
    if resolved_by is not None and resolved_by != row.requested_of and decision != "expired":
        raise PermissionError(
            f"{resolved_by} cannot resolve an approval requested of {row.requested_of}"
        )
    edited_action = redact(edited_action) if edited_action else edited_action
    note = redact(note) if note else note
    now = datetime.now(UTC)
    row.status = decision
    row.resolved_by = resolved_by
    row.edited_action = edited_action
    row.resolved_at = now
    event = Event.new(
        ApprovalResolved(
            approval_id=approval_id,
            decision=decision,
            resolved_by=resolved_by,
            edited_action=edited_action,
            note=note,
        ),  # type: ignore[arg-type]
        actor=Actor.human(resolved_by) if resolved_by else Actor.system("relay.approvals"),
        source="slack" if resolved_by else "api",
        thread_id=approval_id,
        provenance=Provenance(parent_event_ids=[row.event_id]),
    )
    await EventStore(session).append(event)
    await session.flush()
    return row


async def wait(
    session_factory: Any, approval_id: str, *, timeout_s: float, poll_s: float = 1.0
) -> ApprovalRow:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while True:
        async with session_factory() as session:
            row = await session.get(ApprovalRow, approval_id)
            if row is None:
                raise KeyError(approval_id)
            if row.status != "pending":
                return row
            if row.expires_at and datetime.now(UTC) >= row.expires_at:
                row = await resolve(
                    session, approval_id=approval_id, decision="expired", resolved_by=None
                )
                await session.commit()
                return row
        if loop.time() >= deadline:
            return row
        await asyncio.sleep(poll_s)
